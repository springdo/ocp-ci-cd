"""git_clone implementation (MCP tool: debug_git_clone, also used by deploy_from_git)."""

import logging
import os
import pathlib
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


def _clone_destination_exists_message(stderr: str) -> bool:
    """True if git clone failed because the target path already exists and is non-empty."""
    s = (stderr or "").lower()
    return "already exists" in s and "not an empty directory" in s


async def _git_head_info(dest: pathlib.Path) -> tuple[str, str]:
    """Return (commit_hash, commit_message) for HEAD at *dest*."""
    hash_r = await run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"], cwd=WORKSPACE_ROOT
    )
    if hash_r.exit_code != 0:
        raise RuntimeError(
            f"git rev-parse HEAD failed (exit {hash_r.exit_code}):\n{hash_r.stderr}"
        )
    log_r = await run(
        ["git", "-C", str(dest), "log", "-1", "--pretty=format:%s%n%b"],
        cwd=WORKSPACE_ROOT,
    )
    if log_r.exit_code != 0:
        raise RuntimeError(
            f"git log failed (exit {log_r.exit_code}):\n{log_r.stderr}"
        )
    return hash_r.stdout.strip(), log_r.stdout.strip()


async def _git_is_worktree(dest: pathlib.Path) -> bool:
    r = await run(
        ["git", "-C", str(dest), "rev-parse", "--is-inside-work-tree"],
        cwd=WORKSPACE_ROOT,
    )
    return r.exit_code == 0 and r.stdout.strip() == "true"


async def _scrub_origin_if_pat(dest: pathlib.Path, repo_url: str, auth_kind: str) -> None:
    if auth_kind != "https_pat":
        return
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


def resolve_repo_dest(repo_path: str) -> pathlib.Path:
    """Resolve *repo_path* to an absolute path confined under WORKSPACE_ROOT.

    Accepts relative paths (resolved under WORKSPACE_ROOT) or absolute paths
    that already reside under WORKSPACE_ROOT.  Raises ValueError for traversal
    attempts or absolute paths outside WORKSPACE_ROOT.
    """
    p = pathlib.Path(repo_path)
    if p.is_absolute():
        root = WORKSPACE_ROOT.resolve()
        resolved = p.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValueError(
                f"Path {repo_path!r} is outside WORKSPACE_ROOT ({root})"
            )
        return resolved
    return confined_path(repo_path)


async def git_pull(repo_path: str) -> dict:
    """Pull the latest changes in an existing git work tree.

    Args:
        repo_path: Directory of the repository.  May be a relative path under
                   WORKSPACE_ROOT (e.g. ``my-app``) or an absolute path that
                   resolves inside WORKSPACE_ROOT.

    Returns:
        A dict with:
        - ``commit_hash``:   Full SHA-1 of HEAD after the pull.
        - ``commit_message``: Subject + body of the latest commit (trimmed).
        - ``pull_output``:  Raw stdout from ``git pull`` (e.g. "Already up to date.").

    Raises:
        ValueError:   If the path escapes WORKSPACE_ROOT.
        RuntimeError: If the path is not a Git work tree, or if ``git pull`` /
                      subsequent git commands exit non-zero.
    """
    dest = resolve_repo_dest(repo_path)
    logger.info("git_pull called  dest=%s", dest)

    if not await _git_is_worktree(dest):
        raise RuntimeError(
            f"git_pull: {dest} is not a Git work tree; use debug_git_clone to clone first"
        )

    start = time.monotonic()
    pull = await run(["git", "-C", str(dest), "pull"], cwd=WORKSPACE_ROOT)
    if pull.exit_code != 0:
        logger.error(
            "git_pull FAILED  dest=%s  exit=%d\n%s",
            dest,
            pull.exit_code,
            pull.stderr,
        )
        raise RuntimeError(f"git pull failed (exit {pull.exit_code}):\n{pull.stderr}")

    commit_hash, commit_message = await _git_head_info(dest)

    elapsed = round(time.monotonic() - start, 1)
    logger.info(
        "git_pull OK  dest=%s  hash=%s  elapsed=%.1fs", dest, commit_hash[:12], elapsed
    )
    return {
        "commit_hash": commit_hash,
        "commit_message": commit_message,
        "pull_output": pull.stdout.strip(),
    }


async def git_clone(
    repo_url: str,
    local_path: str | None = None,
    branch: str | None = None,
    github_token: str | None = None,
) -> dict:
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
        A dict with ``application_name``, ``commit_hash``, ``commit_message``, and
        ``clone_output`` (raw git stdout).  When the destination already exists as a
        Git work tree, ``git pull`` is run and ``clone_output`` reflects that.

    If ``git clone`` fails because the destination already exists and is non-empty,
    and that path is already a Git work tree, runs ``git pull`` there instead of
    raising.

    Raises:
        RuntimeError: If `git clone` / `git pull` exits non-zero.
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
        if result.exit_code == 128 and _clone_destination_exists_message(result.stderr):
            if await _git_is_worktree(dest):
                logger.info(
                    "git_clone: destination exists and is a git repo, running git pull  dest=%s",
                    dest,
                )
                pull = await run(["git", "-C", str(dest), "pull"], cwd=WORKSPACE_ROOT)
                elapsed = round(time.monotonic() - start, 1)
                if pull.exit_code != 0:
                    logger.error(
                        "git_clone FAILED (git pull)  dest=%s  exit=%d\n%s",
                        dest,
                        pull.exit_code,
                        pull.stderr,
                    )
                    raise RuntimeError(
                        f"git pull failed (exit {pull.exit_code}):\n{pull.stderr}"
                    )
                await _scrub_origin_if_pat(dest, repo_url, auth_kind)
                commit_hash, commit_message = await _git_head_info(dest)
                logger.info(
                    "git_clone OK (pulled)  dest=%s  hash=%s  elapsed=%.1fs",
                    dest, commit_hash[:12], elapsed,
                )
                return {
                    "application_name": effective_path,
                    "commit_hash": commit_hash,
                    "commit_message": commit_message,
                    "clone_output": pull.stdout.strip(),
                }
            logger.warning(
                "git_clone: clone failed (destination exists) but %s is not a git work tree; not running pull",
                dest,
            )

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

    await _scrub_origin_if_pat(dest, repo_url, auth_kind)
    commit_hash, commit_message = await _git_head_info(dest)

    logger.info("git_clone OK  dest=%s  hash=%s  elapsed=%.1fs", dest, commit_hash[:12], elapsed)
    return {
        "application_name": effective_path,
        "commit_hash": commit_hash,
        "commit_message": commit_message,
        "clone_output": result.stdout.strip(),
    }
