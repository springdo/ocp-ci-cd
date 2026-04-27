"""helm_install tool implementation."""

import logging
import os

from ..runner import confined_path, run

logger = logging.getLogger(__name__)

# Fixed to the pod's namespace via Downward API; see Helm chart deployment.yaml.
NAMESPACE = os.environ.get("POD_NAMESPACE", "default")


async def helm_install(
    release_name: str,
    chart_path: str = ".",
    values_files: list[str] | None = None,
) -> str:
    """Install or upgrade a Helm chart in the pod namespace.

    Uses `helm upgrade --install` (idempotent) so the tool is safe to call
    on both first installs and subsequent updates.

    The namespace is always the pod's own namespace (injected via the Downward
    API as POD_NAMESPACE) — cross-namespace installs are out of scope for v1.

    Args:
        release_name: Helm release name.
        chart_path:   Path to the chart directory, relative to WORKSPACE_ROOT
                      (default ``"."`` — useful when the chart is at the root
                      of a freshly cloned repository).
        values_files: Optional list of values file paths, each relative to
                      WORKSPACE_ROOT, passed as ``-f <path>`` arguments.

    Returns:
        stdout from helm on success.

    Raises:
        RuntimeError: If helm exits non-zero after exhausting retries.
        ValueError:   If any path escapes WORKSPACE_ROOT.
    """
    chart_dir = confined_path(chart_path)
    argv = [
        "helm", "upgrade", "--install",
        release_name, str(chart_dir),
        "-n", NAMESPACE,
        "--wait",
    ]
    if values_files:
        for vf in values_files:
            vf_path = confined_path(vf)
            argv += ["-f", str(vf_path)]

    result = await run(argv)
    if result.exit_code != 0:
        raise RuntimeError(
            f"helm upgrade --install failed (exit {result.exit_code}):\n{result.stderr}"
        )
    return result.stdout
