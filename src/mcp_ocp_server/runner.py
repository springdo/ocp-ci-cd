"""Safe subprocess runner with path confinement.

All subprocesses are launched with an explicit argument vector — never via
shell=True or string interpolation — to prevent command injection.

Outputs are capped at MAX_OUTPUT_BYTES to keep MCP responses manageable.
"""

import asyncio
import logging
import os
import pathlib
import time
from typing import NamedTuple

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = pathlib.Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))

MAX_OUTPUT_BYTES = 50_000  # ~50 KB per stream


class RunResult(NamedTuple):
    exit_code: int
    stdout: str
    stderr: str


async def run(argv: list[str], cwd: pathlib.Path | None = None) -> RunResult:
    """Run a subprocess and return its exit code, stdout, and stderr.

    Args:
        argv: Argument vector. The first element must be the executable name or
              absolute path. Never passes user input through a shell.
        cwd:  Working directory; defaults to WORKSPACE_ROOT.
    """
    effective_cwd = cwd or WORKSPACE_ROOT
    cmd_str = " ".join(argv)

    logger.debug("$ %s  (cwd=%s)", cmd_str, effective_cwd)

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=effective_cwd,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    elapsed = round((time.monotonic() - start) * 1000)

    stdout = _truncate(stdout_bytes.decode("utf-8", errors="replace"))
    stderr = _truncate(stderr_bytes.decode("utf-8", errors="replace"))
    exit_code = proc.returncode  # type: ignore[assignment]

    if exit_code == 0:
        logger.info("✓ %s  exit=0  elapsed=%dms", argv[0], elapsed)
        if stdout.strip():
            logger.debug("stdout: %s", _preview(stdout))
        if stderr.strip():
            logger.debug("stderr: %s", _preview(stderr))
    else:
        logger.error(
            "✗ %s  exit=%d  elapsed=%dms\n  cmd : %s\n  stdout: %s\n  stderr: %s",
            argv[0], exit_code, elapsed,
            cmd_str,
            _preview(stdout) or "(empty)",
            _preview(stderr) or "(empty)",
        )

    return RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def _truncate(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        return encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace") + "\n… (output truncated)"
    return text


def _preview(text: str, max_chars: int = 500) -> str:
    """Return first max_chars of text for log lines."""
    text = text.strip()
    if len(text) > max_chars:
        return text[:max_chars] + " …"
    return text


def confined_path(relative: str) -> pathlib.Path:
    """Resolve *relative* under WORKSPACE_ROOT, raising ValueError on traversal.

    Args:
        relative: A path string relative to WORKSPACE_ROOT.

    Returns:
        Resolved absolute path guaranteed to be inside WORKSPACE_ROOT.

    Raises:
        ValueError: If the resolved path escapes WORKSPACE_ROOT.
    """
    root = WORKSPACE_ROOT.resolve()
    resolved = (WORKSPACE_ROOT / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Path {relative!r} resolves outside WORKSPACE_ROOT ({root})"
        )
    logger.debug("confined_path %r → %s", relative, resolved)
    return resolved
