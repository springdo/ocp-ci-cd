"""MCP server entry point — Streamable HTTP transport.

Start with:
    mcp-ocp-server

Environment variables:
    BIND_HOST           Bind address (default: 0.0.0.0)
    PORT                HTTP port (default: 8000)
    WORKSPACE_ROOT      Base directory for git clones / Helm chart paths (default /workspace)
    POD_NAMESPACE       Used as target namespace for oc/helm when OCP_TARGET_NAMESPACE is unset (Downward API in-cluster)
    OCP_TARGET_NAMESPACE  Optional override for oc/helm namespace (otherwise POD_NAMESPACE, else active oc context)
    KUBECONFIG          Optional; auto-generated from SA token when running in-cluster
    MCP_API_KEY         When set, all requests must carry "X-API-Key: <value>"
    GITHUB_TOKEN        Optional; GitHub PAT for private HTTPS clones when debug_git_clone / deploy_from_git omits github_token
    OPENSHIFT_CONSOLE_BASE_URL  Optional; e.g. https://console-openshift-console.apps... (no trailing slash) for debug_openshift_build console_url
    OPENSHIFT_INTERNAL_REGISTRY  Optional; default image-registry.openshift-image-registry.svc:5000 (debug_helm_deploy image.repository prefix)
    HELM_DEPLOY_IMAGE_TAG  Optional; image tag for debug_helm_deploy / deploy_from_git (default latest)
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
from .tools.git import application_name_from_repo_url
from .tools.git import git_clone as _git_clone
from .tools.git import git_pull as _git_pull
from .tools.helm import helm_deploy as _helm_deploy
from .tools.openshift import openshift_build as _openshift_build
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
        "Prefer deploy_from_git(repo_url, …) for clone → build → wait → helm in one call. "
        "Low-level steps are available as debug_git_clone, debug_git_pull, "
        "debug_openshift_build, debug_wait_for_build, debug_helm_deploy."
    ),
    stateless_http=True,
)


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------

@mcp.tool()
async def debug_git_clone(
    repo_url: str,
    local_path: str | None = None,
    branch: str | None = None,
    github_token: str | None = None,
) -> dict:
    """Clone a git repository into WORKSPACE_ROOT/<application_name> (low-level).

    Default ``WORKSPACE_ROOT`` is ``/workspace`` (set in the container; override locally if needed).

    Args:
        repo_url:      The remote URL to clone from (https or ssh).
        local_path:    Optional subdirectory name; if omitted, derived from the repo URL
                       (last path segment). Use the same value as ``debug_openshift_build`` ``name``
                       and ``debug_helm_deploy`` ``app_name``.
        branch:        Optional branch, tag, or commit ref to check out.
        github_token:  Optional GitHub PAT for private HTTPS repos. When omitted,
                       ``GITHUB_TOKEN`` from the environment is used if set.
                       SSH URLs are unchanged (use deploy keys or an SSH agent).

    If the clone target already exists as a Git work tree, ``git pull`` is run there
    instead of failing with "destination path … already exists".

    Returns a dict with:
    - ``application_name``: Derived clone directory name (pass to ``debug_openshift_build`` / ``debug_helm_deploy``).
    - ``commit_hash``:      Full SHA-1 of HEAD after clone / pull.
    - ``commit_message``:   Subject + body of the latest commit (trimmed).
    - ``clone_output``:     Raw git stdout (clone summary or pull output).
    """
    has_token = bool(
        (github_token and github_token.strip())
        or (os.environ.get("GITHUB_TOKEN") or "").strip()
    )
    logger.info(
        "TOOL debug_git_clone  repo=%s  local_path=%r  branch=%r  github_token=%s",
        repo_url,
        local_path,
        branch,
        "set" if has_token else "unset",
    )
    start = time.monotonic()
    try:
        result = await _git_clone(repo_url, local_path, branch, github_token)
        logger.info("TOOL debug_git_clone OK  elapsed=%.1fs", time.monotonic() - start)
        return result
    except Exception as exc:
        logger.error("TOOL debug_git_clone ERROR  elapsed=%.1fs  error=%s", time.monotonic() - start, exc)
        raise


@mcp.tool()
async def debug_git_pull(repo_path: str) -> dict:
    """Pull the latest changes in an existing git work tree (low-level).

    The repository must already exist under ``WORKSPACE_ROOT`` (default ``/workspace``).
    Accepts the same relative path used as ``local_path`` in ``debug_git_clone``
    (e.g. ``my-app`` → ``WORKSPACE_ROOT/my-app``), or an absolute path that resolves
    inside ``WORKSPACE_ROOT``.

    Args:
        repo_path: Relative subdirectory under ``WORKSPACE_ROOT``, or absolute path
                   inside ``WORKSPACE_ROOT``.

    Returns a dict with:
    - ``commit_hash``:    Full SHA-1 of HEAD after the pull.
    - ``commit_message``: Subject + body of the latest commit (trimmed).
    - ``pull_output``:   Raw ``git pull`` stdout (e.g. "Already up to date.").
    """
    logger.info("TOOL debug_git_pull  repo_path=%r", repo_path)
    start = time.monotonic()
    try:
        result = await _git_pull(repo_path)
        logger.info(
            "TOOL debug_git_pull OK  hash=%s  elapsed=%.1fs",
            result.get("commit_hash", "")[:12],
            time.monotonic() - start,
        )
        return result
    except Exception as exc:
        logger.error(
            "TOOL debug_git_pull ERROR  repo_path=%r  elapsed=%.1fs  error=%s",
            repo_path,
            time.monotonic() - start,
            exc,
        )
        raise


@mcp.tool()
async def debug_openshift_build(name: str, git_workspace: str | None = None) -> dict:
    """OpenShift binary Docker build: ensure BuildConfig, upload source, start build (low-level).

    Runs ``oc new-build --binary --name=<name> --strategy=docker`` then
    ``oc start-build <name> --from-dir=<git_workspace>`` (paths under
    ``WORKSPACE_ROOT``). If the BuildConfig already exists, new-build is skipped
    and the start-build still runs.

    Returns a dict with:
    - ``build`` / ``build_name``: OpenShift Build resource name; pass ``build_name`` to ``debug_wait_for_build``.
    - ``namespace``: project/namespace the build runs in.
    - ``console_url``: link to the build in the web console if ``OPENSHIFT_CONSOLE_BASE_URL`` is set, else null.
    - ``reused_buildconfig``: True if the BuildConfig already existed.
    - ``new_build``: new-build stdout or reuse message.
    - ``start_build_output``: raw ``oc start-build`` stdout.

    Args:
        name:           BuildConfig name; use the same string as ``debug_git_clone`` ``application_name``
                        and ``debug_helm_deploy`` ``app_name``.
        git_workspace:  Directory under ``WORKSPACE_ROOT`` with the Dockerfile; defaults to ``name``.
    """
    logger.info("TOOL debug_openshift_build  name=%r  git_workspace=%r", name, git_workspace)
    start = time.monotonic()
    try:
        result = await _openshift_build(name, git_workspace)
        logger.info(
            "TOOL debug_openshift_build OK  name=%r  build_name=%r  elapsed=%.1fs",
            name,
            result.get("build_name"),
            time.monotonic() - start,
        )
        return result
    except Exception as exc:
        logger.error(
            "TOOL debug_openshift_build ERROR  name=%r  elapsed=%.1fs  error=%s",
            name,
            time.monotonic() - start,
            exc,
        )
        raise


@mcp.tool()
async def debug_wait_for_build(
    build_name: str,
    timeout_seconds: int = 3600,
    poll_interval_seconds: int = 10,
) -> dict:
    """Poll an OpenShift Build until it reaches a terminal phase or times out (low-level).

    Returns a dict containing:
    - ``phase`` (str): 'Complete', 'Failed', 'Cancelled', 'Error', or 'Timeout'.
    - ``success`` (bool): True only when phase is 'Complete'.
    - ``message`` (str): Status message from the Build object, if any.
    - ``elapsed_seconds`` (int): Wall-clock seconds spent waiting.

    Args:
        build_name:            Full build name from ``debug_openshift_build`` output,
                               e.g. 'my-app-3'.
        timeout_seconds:       Maximum seconds to wait (default 3600; max 7200).
        poll_interval_seconds: Seconds between polls (clamped 5–60; default 10).
    """
    logger.info(
        "TOOL debug_wait_for_build  build=%r  timeout=%ds  interval=%ds",
        build_name,
        timeout_seconds,
        poll_interval_seconds,
    )
    start = time.monotonic()
    try:
        result = await _wait_for_build(build_name, timeout_seconds, poll_interval_seconds)
        logger.info(
            "TOOL debug_wait_for_build DONE  build=%r  phase=%r  success=%s  elapsed=%.1fs",
            build_name,
            result.get("phase"),
            result.get("success"),
            time.monotonic() - start,
        )
        return result
    except Exception as exc:
        logger.error(
            "TOOL debug_wait_for_build ERROR  build=%r  elapsed=%.1fs  error=%s",
            build_name,
            time.monotonic() - start,
            exc,
        )
        raise


@mcp.tool()
async def debug_helm_deploy(app_name: str) -> dict:
    """Deploy the template app chart using the image built by ``debug_openshift_build`` (low-level).

    Runs ``helm upgrade -i <app_name> <chart> -n <ns>`` (no ``--wait``) with
    ``fullnameOverride`` and ``image.repository`` / ``image.tag`` set (internal
    registry ``OPENSHIFT_INTERNAL_REGISTRY`` / ``namespace`` / ``app_name``;
    tag from ``HELM_DEPLOY_IMAGE_TAG``, default ``latest``).

    Chart path: first of ``<app_name>/chart``, ``<app_name>`` (``Chart.yaml`` at clone
    root), or ``chart`` under ``WORKSPACE_ROOT``. Release name equals ``app_name``.

    On success, runs ``oc get route`` to return ``route_host`` / ``route_url`` when
    a matching Route exists (label ``app.kubernetes.io/instance=<app_name>`` or
    Route named ``app_name``).

    Args:
        app_name: Same as ``debug_openshift_build`` ``name`` and the clone directory (``debug_git_clone``
                  ``local_path`` or URL-derived ``application_name``).
    """
    logger.info("TOOL debug_helm_deploy  app_name=%r", app_name)
    start = time.monotonic()
    try:
        result = await _helm_deploy(app_name)
        logger.info(
            "TOOL debug_helm_deploy OK  app_name=%r  route_url=%r  elapsed=%.1fs",
            app_name,
            result.get("route_url"),
            time.monotonic() - start,
        )
        return result
    except Exception as exc:
        logger.error(
            "TOOL debug_helm_deploy ERROR  app_name=%r  elapsed=%.1fs  error=%s",
            app_name,
            time.monotonic() - start,
            exc,
        )
        raise


@mcp.tool()
async def deploy_from_git(
    repo_url: str,
    local_path: str | None = None,
    branch: str | None = None,
    github_token: str | None = None,
    git_workspace: str | None = None,
    build_wait_timeout_seconds: int = 3600,
    build_poll_interval_seconds: int = 10,
) -> dict:
    """Clone a repo, binary-build, wait for completion, then Helm deploy (single call).

    Resolves ``application_name`` as ``local_path`` or the repo URL basename (same rules
    as ``debug_git_clone``), then runs ``debug_openshift_build`` → ``debug_wait_for_build``
    → ``debug_helm_deploy`` with that name. Helm runs only if the build reaches phase
    ``Complete``.

    Args:
        repo_url:                     Git remote URL (https or ssh).
        local_path:                   Optional clone directory name under ``WORKSPACE_ROOT``;
                                      if omitted, derived from the URL.
        branch:                       Optional ref to check out after clone.
        github_token:                 Optional GitHub PAT for private HTTPS clones.
        git_workspace:                Source directory for ``oc start-build --from-dir``;
                                      defaults to ``application_name``.
        build_wait_timeout_seconds:   Passed to ``debug_wait_for_build`` (default 3600).
        build_poll_interval_seconds:  Poll interval for ``debug_wait_for_build`` (default 10).

    Returns:
        Dict with ``application_name``, ``clone`` (dict with ``application_name``,
        ``commit_hash``, ``commit_message``, ``clone_output``), ``build``, ``wait``,
        and ``helm`` (each tool's return shape).
    """
    application_name = (local_path or "").strip() or application_name_from_repo_url(repo_url)
    logger.info(
        "TOOL deploy_from_git  repo=%r  application_name=%r  branch=%r  git_workspace=%r",
        repo_url,
        application_name,
        branch,
        git_workspace,
    )
    start = time.monotonic()
    try:
        clone_text = await _git_clone(repo_url, local_path, branch, github_token)
        build_out = await _openshift_build(application_name, git_workspace)
        build_name = build_out.get("build_name") or build_out.get("build")
        if not build_name:
            raise RuntimeError(
                "deploy_from_git: no build_name in debug_openshift_build output: "
                f"{build_out!r}"
            )

        wait_out = await _wait_for_build(
            build_name,
            build_wait_timeout_seconds,
            build_poll_interval_seconds,
        )
        if not wait_out.get("success"):
            phase = wait_out.get("phase")
            msg = wait_out.get("message") or ""
            raise RuntimeError(
                f"deploy_from_git: build did not succeed (phase={phase!r} message={msg!r}); "
                f"wait result: {wait_out!r}"
            )

        helm_out = await _helm_deploy(application_name)

        logger.info(
            "TOOL deploy_from_git OK  application_name=%r  route_url=%r  elapsed=%.1fs",
            application_name,
            helm_out.get("route_url"),
            time.monotonic() - start,
        )
        return {
            "application_name": application_name,
            "clone": clone_text,
            "build": build_out,
            "wait": wait_out,
            "helm": helm_out,
        }
    except Exception as exc:
        logger.error(
            "TOOL deploy_from_git ERROR  application_name=%r  elapsed=%.1fs  error=%s",
            application_name,
            time.monotonic() - start,
            exc,
        )
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
