"""git_clone tool implementation."""

from ..runner import WORKSPACE_ROOT, confined_path, run


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
    dest = confined_path(local_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    argv = ["git", "clone", "--depth", "1"]
    if branch:
        argv += ["--branch", branch]
    argv += [repo_url, str(dest)]

    result = await run(argv, cwd=WORKSPACE_ROOT)
    if result.exit_code != 0:
        raise RuntimeError(
            f"git clone failed (exit {result.exit_code}):\n{result.stderr}"
        )
    return f"Cloned {repo_url} → {dest}\n{result.stdout}".strip()
