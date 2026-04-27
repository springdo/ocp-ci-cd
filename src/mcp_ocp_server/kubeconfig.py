"""Bootstrap an in-cluster kubeconfig from the projected service-account token.

When running inside a Kubernetes/OpenShift pod and KUBECONFIG is not already set,
this writes a minimal kubeconfig to /tmp/kubeconfig using the pod's service-account
credentials so that `oc` and `helm` behave as authenticated in-cluster clients.

Using `tokenFile` (a path) rather than embedding the raw token means the kubeconfig
continues to work after token rotation (OCP 4.x bound service-account tokens).
"""

import logging
import os
import pathlib

logger = logging.getLogger(__name__)

_SA_DIR = pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount")
_KUBECONFIG_PATH = pathlib.Path("/tmp/kubeconfig")


def bootstrap_kubeconfig() -> None:
    """Write an in-cluster kubeconfig when no KUBECONFIG env-var is present.

    Safe to call even when running outside a cluster — it is a no-op when the
    service-account mount is absent or KUBECONFIG is already configured.
    """
    if os.environ.get("KUBECONFIG"):
        logger.debug("KUBECONFIG already set — skipping in-cluster bootstrap")
        return

    ca = _SA_DIR / "ca.crt"
    token_path = _SA_DIR / "token"

    if not ca.exists() or not token_path.exists():
        logger.debug("Service-account mount not found — assuming external kubeconfig")
        return

    api_host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    api_port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    api_server = f"https://{api_host}:{api_port}"

    kubeconfig = f"""\
apiVersion: v1
kind: Config
clusters:
- name: in-cluster
  cluster:
    server: {api_server}
    certificate-authority: {ca}
contexts:
- name: in-cluster
  context:
    cluster: in-cluster
    user: sa
current-context: in-cluster
users:
- name: sa
  user:
    tokenFile: {token_path}
"""

    _KUBECONFIG_PATH.write_text(kubeconfig)
    os.environ["KUBECONFIG"] = str(_KUBECONFIG_PATH)
    logger.info("In-cluster kubeconfig written to %s (api-server: %s)", _KUBECONFIG_PATH, api_server)
