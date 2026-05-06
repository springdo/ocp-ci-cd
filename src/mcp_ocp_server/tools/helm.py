"""helm_install tool implementation."""

import logging
import time

from ..runner import confined_path, run
from ..target_ns import ensure_namespace_exists, target_namespace

logger = logging.getLogger(__name__)


async def helm_install(
    release_name: str,
    chart_path: str = ".",
    values_files: list[str] | None = None,
) -> str:
    """Install or upgrade a Helm chart in the target namespace.

    Uses `helm upgrade --install` (idempotent) so the tool is safe to call
    on both first installs and subsequent updates.

    Namespace resolution matches ``oc`` tools (see ``target_namespace`` in
    ``target_ns``): ``OCP_TARGET_NAMESPACE``, then ``POD_NAMESPACE``, then
    ``prototypes``. Cross-namespace installs beyond that are out of scope for v1.

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
    ns = target_namespace()
    logger.info(
        "helm_install called  release=%r  chart_path=%r  values_files=%r  namespace=%s",
        release_name, chart_path, values_files, ns,
    )

    await ensure_namespace_exists(ns)

    chart_dir = confined_path(chart_path)
    logger.debug("Resolved chart dir: %s  (exists=%s)", chart_dir, chart_dir.exists())

    argv = [
        "helm", "upgrade", "--install",
        release_name, str(chart_dir),
        "-n", ns,
        "--wait",
    ]
    if values_files:
        for vf in values_files:
            vf_path = confined_path(vf)
            logger.debug("Values file: %s", vf_path)
            argv += ["-f", str(vf_path)]

    logger.debug("helm argv: %s", argv)
    start = time.monotonic()
    result = await run(argv)
    elapsed = round(time.monotonic() - start, 1)

    if result.exit_code != 0:
        logger.error(
            "helm_install FAILED  release=%r  exit=%d  elapsed=%.1fs\nstdout: %s\nstderr: %s",
            release_name, result.exit_code, elapsed, result.stdout, result.stderr,
        )
        raise RuntimeError(
            f"helm upgrade --install failed (exit {result.exit_code}):\n{result.stderr}"
        )

    logger.info("helm_install OK  release=%r  namespace=%s  elapsed=%.1fs", release_name, ns, elapsed)
    return result.stdout
