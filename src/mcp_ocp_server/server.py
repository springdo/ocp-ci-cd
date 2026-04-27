"""MCP server entry point — Streamable HTTP transport.

Start with:
    mcp-ocp-server

Environment variables:
    BIND_HOST           Bind address (default: 127.0.0.1; use 0.0.0.0 in-cluster)
    PORT                HTTP port (default: 8000)
    WORKSPACE_ROOT      Base directory for git clones / Helm chart paths
    POD_NAMESPACE       Target OpenShift namespace (injected by Downward API in-cluster)
    KUBECONFIG          Optional; auto-generated from SA token when running in-cluster
    MCP_BEARER_TOKEN    When set, all requests must carry "Authorization: Bearer <token>"
    LOG_LEVEL           Python logging level (default: INFO)
"""

import logging
import os

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .kubeconfig import bootstrap_kubeconfig
from .runner import WORKSPACE_ROOT
from .tools.git import git_clone as _git_clone
from .tools.helm import helm_install as _helm_install
from .tools.openshift import oc_new_build as _oc_new_build
from .tools.openshift import oc_start_build as _oc_start_build
from .tools.openshift import wait_for_build as _wait_for_build

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastMCP server — stateless HTTP (one request → one response JSON or SSE)
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "ocp-ci-cd",
    instructions=(
        "MCP server for OpenShift builds and Helm deployments. "
        "Typical flow: git_clone → oc_new_build → oc_start_build → "
        "wait_for_build → helm_install."
    ),
    stateless_http=True,
)


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------

@mcp.tool()
async def git_clone(repo_url: str, local_path: str, branch: str | None = None) -> str:
    """Clone a git repository into WORKSPACE_ROOT/<local_path>.

    Args:
        repo_url:   The remote URL to clone from (https or ssh).
        local_path: Destination directory name, relative to WORKSPACE_ROOT.
                    Must not contain '..' or resolve outside WORKSPACE_ROOT.
        branch:     Optional branch, tag, or commit ref to check out.
    """
    return await _git_clone(repo_url, local_path, branch)


@mcp.tool()
async def oc_new_build(
    name: str,
    strategy: str = "docker",
    image_stream: str | None = None,
    binary: bool = False,
    extra_flags: list[str] | None = None,
) -> str:
    """Create a new BuildConfig on OpenShift via `oc new-build`.

    Args:
        name:         Name for the BuildConfig.
        strategy:     Build strategy — 'docker' (default) or 'source'.
        image_stream: Optional image-stream tag to build from or to,
                      e.g. 'nodejs:18' or 'myapp:latest'.
        binary:       If true, adds --binary for a binary-source build.
        extra_flags:  Additional flags from a fixed allowlist:
                      --context-dir, --to, --source-secret, --push-secret,
                      --labels, --env.  Unrecognised flags are silently dropped.
    """
    return await _oc_new_build(name, strategy, image_stream, binary, extra_flags)


@mcp.tool()
async def oc_start_build(buildconfig: str, commit: str | None = None) -> dict:
    """Trigger a build from an existing BuildConfig via `oc start-build`.

    Returns a dict containing:
    - ``build_name`` (str | None): the new build name (e.g. 'my-app-3').
      Pass this directly to ``wait_for_build``.
    - ``output`` (str): raw stdout from `oc start-build`.

    Args:
        buildconfig: Name of the BuildConfig to start.
        commit:      Optional git commit ref to build from.
    """
    return await _oc_start_build(buildconfig, commit)


@mcp.tool()
async def wait_for_build(
    build_name: str,
    timeout_seconds: int = 3600,
    poll_interval_seconds: int = 10,
) -> dict:
    """Poll an OpenShift Build until it reaches a terminal phase or times out.

    Returns a dict containing:
    - ``phase`` (str): 'Complete', 'Failed', 'Cancelled', 'Error', or 'Timeout'.
    - ``success`` (bool): True only when phase is 'Complete'.
    - ``message`` (str): Status message from the Build object, if any.
    - ``elapsed_seconds`` (int): Wall-clock seconds spent waiting.

    Args:
        build_name:            Full build name from ``oc_start_build`` output,
                               e.g. 'my-app-3'.
        timeout_seconds:       Maximum seconds to wait (default 3600; max 7200).
        poll_interval_seconds: Seconds between polls (clamped 5–60; default 10).
    """
    return await _wait_for_build(build_name, timeout_seconds, poll_interval_seconds)


@mcp.tool()
async def helm_install(
    release_name: str,
    chart_path: str = ".",
    values_files: list[str] | None = None,
) -> str:
    """Install or upgrade a Helm chart in the pod namespace (idempotent).

    Uses `helm upgrade --install --wait` so the call blocks until all
    chart resources are ready or Helm reports a failure.

    The namespace is always the pod's own namespace — cross-namespace
    installs are not supported in v1.

    Args:
        release_name: Helm release name.
        chart_path:   Path to the chart directory, relative to WORKSPACE_ROOT
                      (default '.' — the root of a cloned repo when Chart.yaml
                      lives at the top level).
        values_files: Optional list of values file paths relative to
                      WORKSPACE_ROOT, applied in order as -f arguments.
    """
    return await _helm_install(release_name, chart_path, values_files)


# ---------------------------------------------------------------------------
# Bearer auth middleware (optional)
# ---------------------------------------------------------------------------

class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests that do not carry the expected bearer token."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != self._token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# ---------------------------------------------------------------------------
# App factory and entry point
# ---------------------------------------------------------------------------

def create_app():
    """Build and return the ASGI application.

    Wraps the FastMCP Streamable HTTP app with bearer auth middleware when
    MCP_BEARER_TOKEN is set.
    """
    bootstrap_kubeconfig()
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    app = mcp.streamable_http_app()

    bearer_token = os.environ.get("MCP_BEARER_TOKEN")
    if bearer_token:
        app = _BearerAuthMiddleware(app, bearer_token)
        logger.info("Bearer authentication enabled")
    else:
        logger.warning(
            "MCP_BEARER_TOKEN is not set — the MCP endpoint is unauthenticated. "
            "This is only acceptable when the server is not reachable externally."
        )

    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    host = os.environ.get("BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))

    logger.info(
        "Starting MCP OCP server on %s:%d  workspace=%s  namespace=%s",
        host, port, WORKSPACE_ROOT, os.environ.get("POD_NAMESPACE", "default"),
    )

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
