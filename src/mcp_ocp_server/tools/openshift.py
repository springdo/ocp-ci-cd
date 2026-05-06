"""OpenShift tool implementations: oc_new_build, oc_start_build, wait_for_build."""

import asyncio
import logging
import re
import time

from ..runner import confined_path, run
from ..target_ns import ensure_namespace_exists, target_namespace

logger = logging.getLogger(__name__)

_TERMINAL_PHASES = {"Complete", "Failed", "Cancelled", "Error"}
_RUNNING_PHASES = {"New", "Pending", "Running"}

# Allowlist of flags that may be forwarded to oc new-build.
def _new_build_bc_already_exists(stderr: str, stdout: str) -> bool:
    """True if oc new-build output indicates the BuildConfig already exists."""
    combined = f"{stderr}\n{stdout}".lower()
    if "already exists" not in combined and "alreadyexist" not in combined:
        return False
    return "buildconfig" in combined


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
    context_path: str = ".",
    extra_flags: list[str] | None = None,
) -> str:
    """Create a new BuildConfig via `oc new-build`.

    Args:
        name:          Name for the BuildConfig.
        strategy:      Build strategy — 'docker' or 'source'.
        image_stream:  Optional builder image or image-stream tag, e.g. 'nodejs:18'.
        binary:        If true, use ``--binary`` (no directory source on new-build;
                       follow with ``oc start-build --from-dir`` using the same context).
        context_path:  Directory under WORKSPACE_ROOT containing the Dockerfile or
                       source (default ``'.'``). Required for non-binary builds so
                       ``oc`` receives a source location. Ignored when ``binary`` is true.
        extra_flags:   Optional list of additional flags from a fixed allowlist
                       (--context-dir, --to, --source-secret, --push-secret,
                       --labels, --env).  Any flag not on the allowlist is silently
                       dropped to prevent injection.

    Returns:
        stdout from `oc new-build`.

    Raises:
        RuntimeError: If the command exits non-zero.
    """
    ns = target_namespace()
    logger.info(
        "oc_new_build called  name=%r  strategy=%r  image_stream=%r  binary=%s  context_path=%r  namespace=%s",
        name, strategy, image_stream, binary, context_path, ns,
    )

    await ensure_namespace_exists(ns)

    if binary:
        argv = ["oc", "new-build", f"--name={name}", f"--strategy={strategy}", "-n", ns]
        if image_stream:
            argv.append(image_stream)
        argv.append("--binary")
    else:
        ctx = str(confined_path(context_path))
        argv = ["oc", "new-build"]
        if image_stream:
            argv.append(image_stream)
        argv.append(ctx)
        argv.extend([f"--name={name}", f"--strategy={strategy}", "-n", ns])
    if extra_flags:
        for flag in extra_flags:
            if any(flag.startswith(prefix) for prefix in _NEW_BUILD_FLAG_PREFIXES):
                argv.append(flag)
            else:
                logger.warning("Dropping disallowed flag from oc new-build: %r", flag)

    logger.debug("oc new-build argv: %s", argv)
    result = await run(argv)

    if result.exit_code != 0:
        if _new_build_bc_already_exists(result.stderr, result.stdout):
            verify = await run(["oc", "get", "buildconfig", name, "-n", ns, "-o", "name"])
            if verify.exit_code == 0:
                msg = (
                    f'BuildConfig "{name}" already exists in namespace "{ns}". '
                    "Reusing it — no new BuildConfig was created. "
                    "Use oc_start_build to trigger a build when ready."
                )
                logger.info("oc_new_build: %s", msg)
                return msg
        logger.error(
            "oc_new_build FAILED  name=%r  exit=%d\nstdout: %s\nstderr: %s",
            name, result.exit_code, result.stdout, result.stderr,
        )
        raise RuntimeError(
            f"oc new-build failed (exit {result.exit_code}):\n{result.stderr}"
        )

    logger.info("oc_new_build OK  name=%r", name)
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
    ns = target_namespace()
    logger.info(
        "oc_start_build called  buildconfig=%r  commit=%r  namespace=%s",
        buildconfig, commit, ns,
    )

    await ensure_namespace_exists(ns)

    argv = ["oc", "start-build", buildconfig, "-n", ns]
    if commit:
        argv += ["--commit", commit]

    logger.debug("oc start-build argv: %s", argv)
    result = await run(argv)

    if result.exit_code != 0:
        logger.error(
            "oc_start_build FAILED  buildconfig=%r  exit=%d\nstdout: %s\nstderr: %s",
            buildconfig, result.exit_code, result.stdout, result.stderr,
        )
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

    if build_name:
        logger.info("oc_start_build OK  buildconfig=%r  build_name=%r", buildconfig, build_name)
    else:
        logger.warning(
            "oc_start_build: could not parse build name from output: %r", result.stdout
        )

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
    poll_count = 0

    ns = target_namespace()
    logger.info(
        "wait_for_build called  build=%r  timeout=%ds  interval=%ds  namespace=%s",
        build_name, timeout_seconds, poll_interval_seconds, ns,
    )

    await ensure_namespace_exists(ns)

    while time.monotonic() < deadline:
        poll_count += 1
        result = await run([
            "oc", "get", f"build/{build_name}",
            "-n", ns,
            "-o", r"jsonpath={.status.phase}|{.status.message}",
        ])

        if result.exit_code == 0:
            parts = result.stdout.strip().split("|", 1)
            phase = parts[0] if parts[0] else "Unknown"
            message = parts[1] if len(parts) > 1 else ""

            logger.info(
                "wait_for_build poll #%d  build=%r  phase=%r  elapsed=%.0fs",
                poll_count, build_name, phase, time.monotonic() - start,
            )

            if phase in _TERMINAL_PHASES:
                elapsed = round(time.monotonic() - start)
                if phase == "Complete":
                    logger.info(
                        "wait_for_build COMPLETE  build=%r  elapsed=%ds",
                        build_name, elapsed,
                    )
                else:
                    logger.error(
                        "wait_for_build FAILED  build=%r  phase=%r  message=%r  elapsed=%ds",
                        build_name, phase, message, elapsed,
                    )
                return {
                    "phase": phase,
                    "success": phase == "Complete",
                    "message": message,
                    "elapsed_seconds": elapsed,
                }
        else:
            logger.warning(
                "wait_for_build poll #%d: oc get build/%s returned exit %d: %s",
                poll_count, build_name, result.exit_code, result.stderr.strip(),
            )

        await asyncio.sleep(poll_interval_seconds)

    elapsed = round(time.monotonic() - start)
    logger.error(
        "wait_for_build TIMEOUT  build=%r  last_phase=%r  polls=%d  elapsed=%ds",
        build_name, phase, poll_count, elapsed,
    )
    return {
        "phase": "Timeout",
        "success": False,
        "message": (
            f"Build did not reach a terminal phase within {timeout_seconds}s. "
            f"Last known phase: {phase}"
        ),
        "elapsed_seconds": elapsed,
    }
