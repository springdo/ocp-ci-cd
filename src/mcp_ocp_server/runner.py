"""Safe subprocess runner with path confinement.

All subprocesses are launched with an explicit argument vector — never via
shell=True or string interpolation — to prevent command injection.

Outputs are capped at MAX_OUTPUT_BYTES to keep MCP responses manageable.
"""

import asyncio
import os
import pathlib
from typing import NamedTuple

WORKSPACE_ROOT = pathlib.Path(os.environ.get("WORKSPACE_ROOT", "/tmp/workspace"))

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
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or WORKSPACE_ROOT,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return RunResult(
        exit_code=proc.returncode,  # type: ignore[arg-type]
        stdout=_truncate(stdout_bytes.decode("utf-8", errors="replace")),
        stderr=_truncate(stderr_bytes.decode("utf-8", errors="replace")),
    )


def _truncate(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        return encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace") + "\n… (output truncated)"
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
    return resolved
