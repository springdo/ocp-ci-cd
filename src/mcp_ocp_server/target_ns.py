"""Target namespace resolution and lazy creation for oc/helm tools."""

import logging
import os
import subprocess

from .runner import run

logger = logging.getLogger(__name__)

_ensured_namespaces: set[str] = set()


def _namespace_from_oc_kubeconfig() -> str:
    """Current namespace from ``oc``'s active context (local dev / demo)."""
    try:
        proc = subprocess.run(
            ["oc", "config", "view", "--minify", "-o", "jsonpath={..namespace}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("Could not read namespace from oc kubeconfig: %s", exc)
    return "default"


def target_namespace() -> str:
    """Namespace for ``oc`` and ``helm`` tool calls.

    Resolution order:

    1. ``OCP_TARGET_NAMESPACE`` — explicit override.
    2. ``POD_NAMESPACE`` — the MCP pod's namespace (Downward API in-cluster).
    3. Active ``oc`` context namespace from kubeconfig, then ``default`` if unknown.
    """
    if os.environ.get("OCP_TARGET_NAMESPACE"):
        return os.environ["OCP_TARGET_NAMESPACE"]
    if os.environ.get("POD_NAMESPACE"):
        return os.environ["POD_NAMESPACE"]
    return _namespace_from_oc_kubeconfig()


async def ensure_namespace_exists(ns: str) -> None:
    """Ensure namespace ``ns`` exists; create it if ``oc get`` says it does not.

    When ``ns`` equals ``POD_NAMESPACE`` (the MCP pod's own namespace), this is a
    no-op: the namespace already exists and typical namespaced ServiceAccounts
    cannot ``get`` cluster-scoped ``Namespace`` objects anyway.

    Otherwise tries ``oc create namespace`` first (minimal kubeconfig churn), then
    ``oc new-project`` for OpenShift environments where the former is not allowed.

    Idempotent per process: skips work if we already ensured ``ns`` in this
    interpreter. Concurrent creates may race; a follow-up ``oc get`` confirms.
    """
    if ns in _ensured_namespaces:
        return

    pod_ns = os.environ.get("POD_NAMESPACE")
    if pod_ns and ns == pod_ns:
        logger.debug("ensure_namespace_exists: %r is the pod namespace — skipping check/create", ns)
        _ensured_namespaces.add(ns)
        return

    check = await run(["oc", "get", "namespace", ns, "-o", "name"])
    if check.exit_code == 0:
        _ensured_namespaces.add(ns)
        return

    logger.info("Namespace %r not found — attempting to create it", ns)

    for label, cmd in (
        ("oc create namespace", ["oc", "create", "namespace", ns]),
        ("oc new-project", ["oc", "new-project", ns]),
    ):
        created = await run(cmd)
        if created.exit_code == 0:
            logger.info("Created namespace %r via %s", ns, label)
            _ensured_namespaces.add(ns)
            return

        err = (created.stderr or "").lower()
        if "already exists" in err or "alreadyexist" in err:
            recheck = await run(["oc", "get", "namespace", ns, "-o", "name"])
            if recheck.exit_code == 0:
                logger.info("Namespace %r already present (race or existing resource)", ns)
                _ensured_namespaces.add(ns)
                return

        logger.debug("%s failed for %r: %s", label, ns, (created.stderr or "").strip())

    raise RuntimeError(
        f"Could not create namespace {ns!r}. "
        "Check that you can create namespaces/projects on this cluster, "
        "or create the namespace manually."
    )
