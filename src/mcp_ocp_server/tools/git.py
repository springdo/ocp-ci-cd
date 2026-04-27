"""git_clone tool implementation."""

import logging
import time

from ..runner import WORKSPACE_ROOT, confined_path, run

logger = logging.getLogger(__name__)


async def git_clone(repo_url: str, local_path: str, branch: str | None = None) -> str:
    """Clone a git repository into WORKSPACE_ROOT/<local_path>.

    Args:
        repo_url:   The remote URL to clone from.
        local_path: Destination directory relative to WORKSPACE_ROOT.
        branch:     Optional branch, tag, or commit ref to check out.

    Returns:
        A short summary string on success.

    Raises:
        RuntimeError: If `git clone` exits non-zero.
        ValueError:   If local_path would escape WORKSPACE_ROOT.
    """
    logger.info(
        "git_clone called  repo=%s  local_path=%r  branch=%r",
        repo_url, local_path, branch,
    )

    dest = confined_path(local_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.debug("Clone destination: %s  (exists=%s)", dest, dest.exists())

    argv = ["git", "clone", "--depth", "1"]
    if branch:
        argv += ["--branch", branch]
    argv += [repo_url, str(dest)]

    start = time.monotonic()
    result = await run(argv, cwd=WORKSPACE_ROOT)
    elapsed = round(time.monotonic() - start, 1)

    if result.exit_code != 0:
        logger.error(
            "git_clone FAILED  repo=%s  dest=%s  exit=%d\n%s",
            repo_url, dest, result.exit_code, result.stderr,
        )
        raise RuntimeError(
            f"git clone failed (exit {result.exit_code}):\n{result.stderr}"
        )

    logger.info("git_clone OK  dest=%s  elapsed=%.1fs", dest, elapsed)
    return f"Cloned {repo_url} → {dest}\n{result.stdout}".strip()
