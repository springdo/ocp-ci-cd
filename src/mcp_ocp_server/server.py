"""MCP server entry point — Streamable HTTP transport.

Start with:
    mcp-ocp-server

Environment variables:
    BIND_HOST           Bind address (default: 0.0.0.0)
    PORT                HTTP port (default: 8000)
    WORKSPACE_ROOT      Base directory for git clones / Helm chart paths
    POD_NAMESPACE       Used as target namespace for oc/helm when OCP_TARGET_NAMESPACE is unset (Downward API in-cluster)
    OCP_TARGET_NAMESPACE  Optional override for oc/helm namespace (otherwise POD_NAMESPACE, else prototypes)
    KUBECONFIG          Optional; auto-generated from SA token when running in-cluster
    MCP_API_KEY         When set, all requests must carry "X-API-Key: <value>"
    GITHUB_TOKEN        Optional; GitHub PAT for private HTTPS clones when git_clone omits github_token
    LOG_LEVEL           Python logging level (default: INFO; set DEBUG for full traces)
"""

import logging
import os
import time

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .kubeconfig import bootstrap_kubeconfig
from .runner import WORKSPACE_ROOT
from .target_ns import target_namespace
from .tools.git import git_clone as _git_clone
from .tools.helm import helm_install as _helm_install
from .tools.openshift import oc_new_build as _oc_new_build
from .tools.openshift import oc_start_build as _oc_start_build
from .tools.openshift import wait_for_build as _wait_for_build

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

# When BIND_HOST is 0.0.0.0 (the default for containers), FastMCP does NOT
# auto-enable its localhost-only DNS rebinding protection, so requests from
# an OpenShift Route hostname are accepted.
_BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")

mcp = FastMCP(
    "ocp-ci-cd",
    host=_BIND_HOST,
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
async def git_clone(
    repo_url: str,
    local_path: str,
    branch: str | None = None,
    github_token: str | None = None,
) -> str:
    """Clone a git repository into WORKSPACE_ROOT/<local_path>.

    Args:
        repo_url:      The remote URL to clone from (https or ssh).
        local_path:    Destination directory name, relative to WORKSPACE_ROOT.
                       Must not contain '..' or resolve outside WORKSPACE_ROOT.
        branch:        Optional branch, tag, or commit ref to check out.
        github_token:  Optional GitHub PAT for private HTTPS repos. When omitted,
                       ``GITHUB_TOKEN`` from the environment is used if set.
                       SSH URLs are unchanged (use deploy keys or an SSH agent).
    """
    has_token = bool(
        (github_token and github_token.strip())
        or (os.environ.get("GITHUB_TOKEN") or "").strip()
    )
    logger.info(
        "TOOL git_clone  repo=%s  local_path=%r  branch=%r  github_token=%s",
        repo_url,
        local_path,
        branch,
        "set" if has_token else "unset",
    )
    start = time.monotonic()
    try:
        result = await _git_clone(repo_url, local_path, branch, github_token)
        logger.info("TOOL git_clone OK  elapsed=%.1fs", time.monotonic() - start)
        return result
    except Exception as exc:
        logger.error("TOOL git_clone ERROR  elapsed=%.1fs  error=%s", time.monotonic() - start, exc)
        raise


@mcp.tool()
async def oc_new_build(
    name: str,
    strategy: str = "docker",
    image_stream: str | None = None,
    binary: bool = False,
    context_path: str = ".",
    extra_flags: list[str] | None = None,
) -> str:
    """Create a new BuildConfig on OpenShift via `oc new-build`.

    Args:
        name:          Name for the BuildConfig.
        strategy:      Build strategy — 'docker' (default) or 'source'.
        image_stream:  Optional builder image or image-stream tag,
                       e.g. 'nodejs:18' or 'myapp:latest'.
        binary:        If true, adds --binary for a binary-source build (no directory
                       on new-build; use ``oc start-build --from-dir`` with the same
                       ``context_path``).
        context_path:  Directory under WORKSPACE_ROOT with the Dockerfile or source
                       (default ``'.'``). After ``git_clone`` into ``myapp``, set this
                       to ``myapp`` (or ``.`` if the clone target is the workspace root).
        extra_flags:   Additional flags from a fixed allowlist:
                       --context-dir, --to, --source-secret, --push-secret,
                       --labels, --env.  Unrecognised flags are silently dropped.
    """
    logger.info(
        "TOOL oc_new_build  name=%r  strategy=%r  image_stream=%r  binary=%s  context_path=%r",
        name, strategy, image_stream, binary, context_path,
    )
    start = time.monotonic()
    try:
        result = await _oc_new_build(name, strategy, image_stream, binary, context_path, extra_flags)
        logger.info("TOOL oc_new_build OK  name=%r  elapsed=%.1fs", name, time.monotonic() - start)
        return result
    except Exception as exc:
        logger.error("TOOL oc_new_build ERROR  name=%r  elapsed=%.1fs  error=%s", name, time.monotonic() - start, exc)
        raise


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
    logger.info("TOOL oc_start_build  buildconfig=%r  commit=%r", buildconfig, commit)
    start = time.monotonic()
    try:
        result = await _oc_start_build(buildconfig, commit)
        logger.info(
            "TOOL oc_start_build OK  buildconfig=%r  build_name=%r  elapsed=%.1fs",
            buildconfig, result.get("build_name"), time.monotonic() - start,
        )
        return result
    except Exception as exc:
        logger.error("TOOL oc_start_build ERROR  buildconfig=%r  elapsed=%.1fs  error=%s", buildconfig, time.monotonic() - start, exc)
        raise


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
    logger.info("TOOL wait_for_build  build=%r  timeout=%ds  interval=%ds", build_name, timeout_seconds, poll_interval_seconds)
    start = time.monotonic()
    try:
        result = await _wait_for_build(build_name, timeout_seconds, poll_interval_seconds)
        logger.info(
            "TOOL wait_for_build DONE  build=%r  phase=%r  success=%s  elapsed=%.1fs",
            build_name, result.get("phase"), result.get("success"), time.monotonic() - start,
        )
        return result
    except Exception as exc:
        logger.error("TOOL wait_for_build ERROR  build=%r  elapsed=%.1fs  error=%s", build_name, time.monotonic() - start, exc)
        raise


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
    logger.info("TOOL helm_install  release=%r  chart_path=%r  values_files=%r", release_name, chart_path, values_files)
    start = time.monotonic()
    try:
        result = await _helm_install(release_name, chart_path, values_files)
        logger.info("TOOL helm_install OK  release=%r  elapsed=%.1fs", release_name, time.monotonic() - start)
        return result
    except Exception as exc:
        logger.error("TOOL helm_install ERROR  release=%r  elapsed=%.1fs  error=%s", release_name, time.monotonic() - start, exc)
        raise


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class _RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every inbound HTTP request and its response status + timing."""

    # Headers whose values should not appear in logs.
    _REDACT = {"x-api-key", "authorization", "cookie"}

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        client = request.client.host if request.client else "unknown"

        # Log safe subset of headers at DEBUG level.
        safe_headers = {
            k: ("***" if k.lower() in self._REDACT else v)
            for k, v in request.headers.items()
        }
        logger.debug(
            "→ %s %s  client=%s  headers=%s",
            request.method, request.url.path, client, safe_headers,
        )
        logger.info("→ %s %s  client=%s", request.method, request.url.path, client)

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed = round((time.monotonic() - start) * 1000)
            logger.error(
                "← %s %s  EXCEPTION  elapsed=%dms  error=%s",
                request.method, request.url.path, elapsed, exc,
            )
            raise

        elapsed = round((time.monotonic() - start) * 1000)
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        logger.log(
            level,
            "← %s %s  status=%d  elapsed=%dms",
            request.method, request.url.path, response.status_code, elapsed,
        )
        return response


class _ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests that do not carry the expected X-API-Key header value."""

    def __init__(self, app, api_key: str) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("X-API-Key") != self._api_key:
            logger.warning(
                "Unauthorized request  method=%s  path=%s  client=%s",
                request.method, request.url.path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# ---------------------------------------------------------------------------
# App factory and entry point
# ---------------------------------------------------------------------------

def create_app():
    """Build and return the ASGI application.

    Middleware stack (outermost → innermost):
      _RequestLoggingMiddleware  — logs every request/response
      _ApiKeyMiddleware          — enforces X-API-Key when MCP_API_KEY is set
      FastMCP streamable_http_app
    """
    bootstrap_kubeconfig()
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    app = mcp.streamable_http_app()

    api_key = os.environ.get("MCP_API_KEY")
    if api_key:
        app = _ApiKeyMiddleware(app, api_key)
        logger.info("X-API-Key authentication enabled")
    else:
        logger.warning(
            "MCP_API_KEY is not set — the MCP endpoint is unauthenticated. "
            "This is only acceptable when the server is not reachable externally."
        )

    # Request logger wraps everything so all requests (including 401s) are logged.
    app = _RequestLoggingMiddleware(app)

    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    port = int(os.environ.get("PORT", "8000"))

    logger.info(
        "Starting MCP OCP server  bind=%s:%d  workspace=%s  target_namespace=%s",
        _BIND_HOST, port, WORKSPACE_ROOT, target_namespace(),
    )

    uvicorn.run(create_app(), host=_BIND_HOST, port=port)


if __name__ == "__main__":
    main()
