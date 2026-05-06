"""OpenShift tool implementations: openshift_build + wait_for_build."""

import asyncio
import logging
import os
import re
import time
from urllib.parse import quote

from ..runner import confined_path, run
from ..target_ns import ensure_namespace_exists, target_namespace

logger = logging.getLogger(__name__)

_TERMINAL_PHASES = {"Complete", "Failed", "Cancelled", "Error"}


def _new_build_bc_already_exists(stderr: str, stdout: str) -> bool:
    """True if oc new-build output indicates the BuildConfig already exists."""
    combined = f"{stderr}\n{stdout}".lower()
    if "already exists" not in combined and "alreadyexist" not in combined:
        return False
    return "buildconfig" in combined


def _parse_build_name_from_start_build(stdout: str) -> str | None:
    for line in stdout.splitlines():
        match = re.search(r"build\.build\.openshift\.io/(\S+)", line)
        if match:
            return match.group(1)
    return None


def openshift_build_console_url(namespace: str, build_name: str | None) -> str | None:
    """Deep link to a Build in the OpenShift console (OCP 4 dynamic console).

    Set ``OPENSHIFT_CONSOLE_BASE_URL`` (no trailing slash), e.g.
    ``https://console-openshift-console.apps.cluster.example.com``.
    """
    if not build_name:
        return None
    base = (os.environ.get("OPENSHIFT_CONSOLE_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return None
    ns_q = quote(namespace, safe="")
    bn_q = quote(build_name, safe="")
    return f"{base}/k8s/ns/{ns_q}/build.openshift.io~v1~Build/{bn_q}"


async def openshift_build(name: str, git_workspace: str | None = None) -> dict:
    """Create a binary Docker BuildConfig (if needed) and start a build from a local directory.

    Runs (fixed flags):

    1. ``oc new-build --binary --name=<name> --strategy=docker -n <ns>``
    2. ``oc start-build <name> --from-dir=<git_workspace> -n <ns>``

    ``git_workspace`` is resolved under ``WORKSPACE_ROOT``. If omitted, defaults to
    ``name`` (same folder as ``git_clone`` when using URL-derived ``application_name``).

    If step 1 fails because the BuildConfig already exists, that is treated as success
    and step 2 still runs.

    Returns:
        dict with ``build_name`` (same as ``build``, for ``wait_for_build``), ``namespace``,
        ``console_url`` (if ``OPENSHIFT_CONSOLE_BASE_URL`` is set), ``reused_buildconfig``,
        ``new_build``, ``start_build_output``.

    Raises:
        RuntimeError: If either step fails (except handled already-exists on new-build).
        ValueError: If ``git_workspace`` escapes ``WORKSPACE_ROOT``.
    """
    ws = (git_workspace or "").strip() or name
    ns = target_namespace()
    logger.info(
        "openshift_build  name=%r  git_workspace=%r  namespace=%s",
        name, ws, ns,
    )

    await ensure_namespace_exists(ns)

    argv_new = [
        "oc",
        "new-build",
        "--binary",
        f"--name={name}",
        "--strategy=docker",
        "-n",
        ns,
    ]
    logger.debug("oc new-build argv: %s", argv_new)
    result_nb = await run(argv_new)

    reused_bc = False
    if result_nb.exit_code != 0:
        if _new_build_bc_already_exists(result_nb.stderr, result_nb.stdout):
            verify = await run(["oc", "get", "buildconfig", name, "-n", ns, "-o", "name"])
            if verify.exit_code == 0:
                reused_bc = True
                nb_msg = (
                    f'BuildConfig "{name}" already exists in namespace "{ns}" — reusing it.'
                )
                logger.info("openshift_build new-build: %s", nb_msg)
            else:
                logger.error(
                    "openshift_build new-build FAILED (already exists?) but get bc failed  name=%r\nstderr: %s",
                    name,
                    result_nb.stderr,
                )
                raise RuntimeError(
                    f"oc new-build failed (exit {result_nb.exit_code}):\n{result_nb.stderr}"
                )
        else:
            logger.error(
                "openshift_build new-build FAILED  name=%r  exit=%d\nstdout: %s\nstderr: %s",
                name,
                result_nb.exit_code,
                result_nb.stdout,
                result_nb.stderr,
            )
            raise RuntimeError(
                f"oc new-build failed (exit {result_nb.exit_code}):\n{result_nb.stderr}"
            )
    else:
        nb_msg = result_nb.stdout.strip()

    from_dir = str(confined_path(ws))
    argv_start = ["oc", "start-build", name, "--from-dir", from_dir, "-n", ns]
    logger.debug("oc start-build argv: %s", argv_start)
    result_sb = await run(argv_start)

    if result_sb.exit_code != 0:
        logger.error(
            "openshift_build start-build FAILED  name=%r  exit=%d\nstdout: %s\nstderr: %s",
            name,
            result_sb.exit_code,
            result_sb.stdout,
            result_sb.stderr,
        )
        raise RuntimeError(
            f"oc start-build failed (exit {result_sb.exit_code}):\n{result_sb.stderr}"
        )

    build_name = _parse_build_name_from_start_build(result_sb.stdout)
    if build_name:
        logger.info(
            "openshift_build OK  name=%r  build_name=%r  reused_bc=%s",
            name,
            build_name,
            reused_bc,
        )
    else:
        logger.warning(
            "openshift_build: could not parse build name from: %r",
            result_sb.stdout,
        )

    console_url = openshift_build_console_url(ns, build_name)
    return {
        "build_name": build_name,
        "build": build_name,
        "namespace": ns,
        "console_url": console_url,
        "reused_buildconfig": reused_bc,
        "new_build": nb_msg,
        "start_build_output": result_sb.stdout,
    }


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
                               from ``openshift_build`` output ``build_name``.
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
