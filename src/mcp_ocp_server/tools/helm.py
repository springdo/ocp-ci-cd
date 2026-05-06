"""helm_deploy — constrained Helm install for template repos after openshift_build."""

import logging
import os
import time
from pathlib import Path

from ..runner import confined_path, run
from ..target_ns import ensure_namespace_exists, target_namespace

logger = logging.getLogger(__name__)

_DEFAULT_REGISTRY = "image-registry.openshift-image-registry.svc:5000"


def _is_helm_chart_dir(path: Path) -> bool:
    return path.is_dir() and (path / "Chart.yaml").is_file()


def _resolve_chart_dir(app_name: str) -> tuple[Path, str]:
    """Return (absolute chart path, relative label for logs)."""
    candidates: list[tuple[str, Path]] = [
        (f"{app_name}/chart", confined_path(f"{app_name}/chart")),
        (app_name, confined_path(app_name)),
        ("chart", confined_path("chart")),
    ]
    for rel, path in candidates:
        if _is_helm_chart_dir(path):
            return path, rel
    raise ValueError(
        f"No Helm chart found (need Chart.yaml): tried WORKSPACE_ROOT/{app_name}/chart, "
        f"WORKSPACE_ROOT/{app_name}, WORKSPACE_ROOT/chart"
    )


async def _discover_route_host(ns: str, app_name: str) -> str | None:
    """Return Route .spec.host for the app release, or None."""
    labeled = await run([
        "oc",
        "get",
        "route",
        "-n",
        ns,
        "-l",
        f"app.kubernetes.io/instance={app_name}",
        "-o",
        "jsonpath={.items[0].spec.host}",
    ])
    if labeled.exit_code == 0 and labeled.stdout.strip():
        return labeled.stdout.strip()

    by_name = await run([
        "oc",
        "get",
        "route",
        app_name,
        "-n",
        ns,
        "-o",
        "jsonpath={.spec.host}",
    ])
    if by_name.exit_code == 0 and by_name.stdout.strip():
        return by_name.stdout.strip()

    logger.warning(
        "helm_deploy: no Route host found for release=%r in namespace=%r "
        "(tried label app.kubernetes.io/instance and route name)",
        app_name,
        ns,
    )
    return None


async def helm_deploy(app_name: str) -> dict:
    """Run ``helm upgrade -i`` for ``{app_name}/chart`` using the internal registry image.

    Image: ``{OPENSHIFT_INTERNAL_REGISTRY}/{namespace}/{app_name}:{tag}`` (repository
    passed as ``--set image.repository=...`` / ``image.tag``).

    ``app_name`` should match ``openshift_build`` ``name`` and the clone directory
    (``git_clone`` ``local_path`` or URL-derived name).

    Chart directory (first path with ``Chart.yaml``): ``{app_name}/chart``, then
    ``{app_name}`` (chart at clone root), then ``chart`` under workspace root.

    Does not pass ``--wait`` (avoids long Helm timeouts); resources may still be
    rolling out when the command returns.

    After a successful install, discovers the application Route host via
    ``oc get route`` (label ``app.kubernetes.io/instance`` first, then route named
    ``app_name``). The route may not exist yet if the chart creates it asynchronously.

    Returns:
        Dict with ``helm_output``, ``namespace``, ``release``, ``image_repository``,
        ``image_tag``, ``route_host``, ``route_url`` (``https://`` + host when known).

    Raises:
        RuntimeError: If helm fails.
        ValueError: If paths escape ``WORKSPACE_ROOT``.
    """
    ns = target_namespace()
    registry = (os.environ.get("OPENSHIFT_INTERNAL_REGISTRY") or _DEFAULT_REGISTRY).strip().rstrip("/")
    tag = (os.environ.get("HELM_DEPLOY_IMAGE_TAG") or "latest").strip()

    image_repository = f"{registry}/{ns}/{app_name}"

    logger.info(
        "helm_deploy  app_name=%r  namespace=%s  image_repository=%s:%s",
        app_name,
        ns,
        image_repository,
        tag,
    )

    await ensure_namespace_exists(ns)

    chart_dir, chart_rel = _resolve_chart_dir(app_name)
    logger.debug("Resolved chart dir: %s  (rel=%s)", chart_dir, chart_rel)

    release = app_name

    argv = [
        "helm",
        "upgrade",
        "-i",
        release,
        str(chart_dir),
        "-n",
        ns,
        "--set-string",
        f"fullnameOverride={app_name}",
        "--set",
        f"image.repository={image_repository}",
        "--set",
        f"image.tag={tag}",
    ]

    logger.debug("helm argv: %s", argv)
    start = time.monotonic()
    result = await run(argv)
    elapsed = round(time.monotonic() - start, 1)

    if result.exit_code != 0:
        logger.error(
            "helm_deploy FAILED  release=%r  exit=%d  elapsed=%.1fs\nstdout: %s\nstderr: %s",
            release,
            result.exit_code,
            elapsed,
            result.stdout,
            result.stderr,
        )
        raise RuntimeError(
            f"helm upgrade -i failed (exit {result.exit_code}):\n{result.stderr}"
        )

    logger.info("helm_deploy OK  release=%r  namespace=%s  elapsed=%.1fs", release, ns, elapsed)

    route_host = await _discover_route_host(ns, release)
    route_url = f"https://{route_host}" if route_host else None

    return {
        "helm_output": result.stdout.strip(),
        "namespace": ns,
        "release": release,
        "image_repository": image_repository,
        "image_tag": tag,
        "route_host": route_host,
        "route_url": route_url,
    }
