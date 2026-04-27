"""OpenShift tool implementations: oc_new_build, oc_start_build, wait_for_build."""

import asyncio
import logging
import os
import re
import time

from ..runner import run

logger = logging.getLogger(__name__)

# Injected from the Downward API (fieldRef: metadata.namespace) in the Helm chart.
# Falls back to "default" for local development.
NAMESPACE = os.environ.get("POD_NAMESPACE", "default")

_TERMINAL_PHASES = {"Complete", "Failed", "Cancelled", "Error"}
_RUNNING_PHASES = {"New", "Pending", "Running"}

# Allowlist of flags that may be forwarded to oc new-build.
_NEW_BUILD_FLAG_PREFIXES = (
    "--context-dir=",
    "--to=",
    "--source-secret=",
    "--push-secret=",
    "--labels=",
    "--env=",
)


async def oc_new_build(
    name: str,
    strategy: str = "docker",
    image_stream: str | None = None,
    binary: bool = False,
    extra_flags: list[str] | None = None,
) -> str:
    """Create a new BuildConfig via `oc new-build`.

    Args:
        name:         Name for the BuildConfig.
        strategy:     Build strategy — 'docker' or 'source'.
        image_stream: Optional image-stream tag, e.g. 'nodejs:18' or 'myapp:latest'.
        binary:       If true, add --binary for a binary-source build.
        extra_flags:  Optional list of additional flags from a fixed allowlist
                      (--context-dir, --to, --source-secret, --push-secret,
                      --labels, --env).  Any flag not on the allowlist is silently
                      dropped to prevent injection.

    Returns:
        stdout from `oc new-build`.

    Raises:
        RuntimeError: If the command exits non-zero.
    """
    argv = ["oc", "new-build", f"--name={name}", f"--strategy={strategy}", "-n", NAMESPACE]
    if image_stream:
        argv.append(image_stream)
    if binary:
        argv.append("--binary")
    if extra_flags:
        for flag in extra_flags:
            if any(flag.startswith(prefix) for prefix in _NEW_BUILD_FLAG_PREFIXES):
                argv.append(flag)
            else:
                logger.warning("Dropping disallowed flag from oc new-build: %r", flag)

    result = await run(argv)
    if result.exit_code != 0:
        raise RuntimeError(
            f"oc new-build failed (exit {result.exit_code}):\n{result.stderr}"
        )
    return result.stdout


async def oc_start_build(
    buildconfig: str,
    commit: str | None = None,
) -> dict:
    """Trigger a build from a BuildConfig via `oc start-build`.

    Args:
        buildconfig: Name of the BuildConfig to start.
        commit:      Optional git commit ref to build from.

    Returns:
        A dict with:
        - ``build_name`` (str | None): The new build's name, parsed from `oc` output.
          Pass this directly to ``wait_for_build``.
        - ``output`` (str): Raw stdout from `oc start-build`.

    Raises:
        RuntimeError: If the command exits non-zero.
    """
    argv = ["oc", "start-build", buildconfig, "-n", NAMESPACE]
    if commit:
        argv += ["--commit", commit]

    result = await run(argv)
    if result.exit_code != 0:
        raise RuntimeError(
            f"oc start-build failed (exit {result.exit_code}):\n{result.stderr}"
        )

    # `oc start-build` prints something like:
    #   build.build.openshift.io/my-app-3 started
    build_name: str | None = None
    for line in result.stdout.splitlines():
        match = re.search(r"build\.build\.openshift\.io/(\S+)", line)
        if match:
            build_name = match.group(1)
            break

    return {"build_name": build_name, "output": result.stdout}


async def wait_for_build(
    build_name: str,
    timeout_seconds: int = 3600,
    poll_interval_seconds: int = 10,
) -> dict:
    """Poll an OpenShift Build until it reaches a terminal phase or times out.

    The tool queries `oc get build/<name>` on a regular interval and returns
    as soon as the build phase is one of: Complete, Failed, Cancelled, Error.

    Args:
        build_name:            Full build name (e.g. ``my-app-3``), typically
                               obtained from the ``build_name`` field returned
                               by ``oc_start_build``.
        timeout_seconds:       Maximum seconds to wait before giving up
                               (default 3600; max enforced at 7200).
        poll_interval_seconds: Seconds between polls (clamped 5–60; default 10).

    Returns:
        A dict with:
        - ``phase`` (str): Terminal phase or ``"Timeout"`` / ``"Unknown"``.
        - ``success`` (bool): True only when phase is ``"Complete"``.
        - ``message`` (str): Status message from the Build object, if any.
        - ``elapsed_seconds`` (int): Wall-clock seconds spent waiting.
    """
    poll_interval_seconds = max(5, min(poll_interval_seconds, 60))
    timeout_seconds = max(1, min(timeout_seconds, 7200))
    deadline = time.monotonic() + timeout_seconds
    start = time.monotonic()
    phase = "Unknown"
    message = ""

    while time.monotonic() < deadline:
        result = await run([
            "oc", "get", f"build/{build_name}",
            "-n", NAMESPACE,
            "-o", r"jsonpath={.status.phase}|{.status.message}",
        ])

        if result.exit_code == 0:
            parts = result.stdout.strip().split("|", 1)
            phase = parts[0] if parts[0] else "Unknown"
            message = parts[1] if len(parts) > 1 else ""

            if phase in _TERMINAL_PHASES:
                return {
                    "phase": phase,
                    "success": phase == "Complete",
                    "message": message,
                    "elapsed_seconds": round(time.monotonic() - start),
                }
        else:
            logger.warning(
                "oc get build/%s returned exit %d: %s",
                build_name, result.exit_code, result.stderr.strip(),
            )

        await asyncio.sleep(poll_interval_seconds)

    return {
        "phase": "Timeout",
        "success": False,
        "message": (
            f"Build did not reach a terminal phase within {timeout_seconds}s. "
            f"Last known phase: {phase}"
        ),
        "elapsed_seconds": round(time.monotonic() - start),
    }
