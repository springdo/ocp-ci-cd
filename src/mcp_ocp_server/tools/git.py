"""git_clone tool implementation."""

import logging
import os
import time
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

from ..runner import WORKSPACE_ROOT, confined_path, run

logger = logging.getLogger(__name__)


def application_name_from_repo_url(repo_url: str) -> str:
    """Last path segment of the repo (e.g. ``my-app`` from ``https://github.com/org/my-app.git``)."""
    u = repo_url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    if "://" in u:
        path = urlparse(u).path.strip("/")
        name = path.split("/")[-1] if path else "app"
    elif u.startswith("git@"):
        path = u.split(":", 1)[-1].strip("/")
        name = path.split("/")[-1] if path else "app"
    else:
        name = os.path.basename(u) or "app"
    if not name or name in (".", ".."):
        name = "app"
    return name


def _host_port_from_netloc(netloc: str) -> str:
    """Return host[:port] from netloc, stripping any userinfo prefix."""
    if "@" in netloc:
        return netloc.rsplit("@", 1)[-1]
    return netloc


def _clone_url_with_https_pat(repo_url: str, token: str) -> str:
    """Embed a GitHub-style PAT for HTTPS clone (x-access-token user)."""
    parts = urlsplit(repo_url)
    if parts.scheme.lower() != "https":
        return repo_url
    host_port = _host_port_from_netloc(parts.netloc)
    user = quote("x-access-token", safe="")
    pwd = quote(token, safe="")
    new_netloc = f"{user}:{pwd}@{host_port}"
    return urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))


async def git_clone(
    repo_url: str,
    local_path: str | None = None,
    branch: str | None = None,
    github_token: str | None = None,
) -> str:
    """Clone a git repository into WORKSPACE_ROOT/<local_path>.

    Args:
        repo_url:       The remote URL to clone from.
        local_path:     Destination directory under WORKSPACE_ROOT. If omitted, the
                        repository name is taken from the URL (use the same string as
                        ``name`` / ``app_name`` in later tools).
        branch:         Optional branch, tag, or commit ref to check out.
        github_token:   Optional GitHub PAT for private HTTPS repos. If omitted,
                        ``GITHUB_TOKEN`` from the environment is used when set.

    Returns:
        A short summary string on success (includes ``application_name``).

    Raises:
        RuntimeError: If `git clone` exits non-zero.
        ValueError:   If local_path would escape WORKSPACE_ROOT.
    """
    effective_path = (local_path or "").strip() or application_name_from_repo_url(repo_url)

    token = (github_token or os.environ.get("GITHUB_TOKEN") or "").strip() or None
    scheme = urlsplit(repo_url).scheme.lower()
    if token and scheme == "https":
        clone_url = _clone_url_with_https_pat(repo_url, token)
        auth_kind = "https_pat"
    else:
        clone_url = repo_url
        if token:
            auth_kind = "pat_ignored_non_https"
        else:
            auth_kind = "none"

    logger.info(
        "git_clone called  repo=%s  local_path=%r  application_name=%r  branch=%r  auth=%s",
        repo_url,
        local_path,
        effective_path,
        branch,
        auth_kind,
    )

    dest = confined_path(effective_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.debug("Clone destination: %s  (exists=%s)", dest, dest.exists())

    argv = ["git", "clone", "--depth", "1"]
    if branch:
        argv += ["--branch", branch]
    argv += [clone_url, str(dest)]

    start = time.monotonic()
    result = await run(argv, cwd=WORKSPACE_ROOT)
    elapsed = round(time.monotonic() - start, 1)

    if result.exit_code != 0:
        logger.error(
            "git_clone FAILED  repo=%s  dest=%s  exit=%d\n%s",
            repo_url,
            dest,
            result.exit_code,
            result.stderr,
        )
        raise RuntimeError(
            f"git clone failed (exit {result.exit_code}):\n{result.stderr}"
        )

    if auth_kind == "https_pat":
        origin_set = await run(
            ["git", "-C", str(dest), "remote", "set-url", "origin", repo_url],
            cwd=WORKSPACE_ROOT,
        )
        if origin_set.exit_code != 0:
            logger.warning(
                "git_clone: could not scrub origin URL (exit %d): %s",
                origin_set.exit_code,
                origin_set.stderr.strip(),
            )

    logger.info("git_clone OK  dest=%s  elapsed=%.1fs", dest, elapsed)
    return (
        f"Cloned {repo_url} → {dest}\n"
        f"application_name={effective_path}  (use for openshift_build name, helm_deploy app_name)\n"
        f"{result.stdout}"
    ).strip()
