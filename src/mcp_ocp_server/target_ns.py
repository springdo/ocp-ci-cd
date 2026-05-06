"""Target namespace resolution and lazy creation for oc/helm tools."""

import logging
import os

from .runner import run

logger = logging.getLogger(__name__)

_ensured_namespaces: set[str] = set()


def target_namespace() -> str:
    """Namespace for ``oc`` and ``helm`` tool calls.

    Resolution order:

    1. ``OCP_TARGET_NAMESPACE`` — explicit override (e.g. always use ``prototypes``).
    2. ``POD_NAMESPACE`` — Downward API when the server runs in-cluster.
    3. ``prototypes`` — local / unset defaults.
    """
    return os.environ.get(
        "OCP_TARGET_NAMESPACE",
        os.environ.get("POD_NAMESPACE", "prototypes"),
    )


async def ensure_namespace_exists(ns: str) -> None:
    """Ensure namespace ``ns`` exists; create it if ``oc get`` says it does not.

    Tries ``oc create namespace`` first (minimal kubeconfig churn), then
    ``oc new-project`` for OpenShift environments where the former is not allowed.

    Idempotent per process: skips work if we already ensured ``ns`` in this
    interpreter. Concurrent creates may race; a follow-up ``oc get`` confirms.
    """
    if ns in _ensured_namespaces:
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
