# MCP OCP Server

An MCP server that exposes OpenShift build and Helm deployment operations over the [Streamable HTTP](https://modelcontextprotocol.io/specification/latest/basic/transports) transport. Connect any MCP client (e.g. Cursor) to the server's HTTP endpoint and run `git clone` → `oc new-build` → `oc start-build` → `wait_for_build` → `helm install` as natural-language-driven tool calls.

## Prerequisites

| Tool | Notes |
|------|-------|
| Python ≥ 3.11 | Runtime for the MCP server |
| `oc` | OpenShift CLI; must be logged in (`oc login`) for local dev |
| `helm` ≥ 3.x | Installed on `PATH` |
| `git` | Installed on `PATH` |
| Docker / Podman | Only needed to build the container image |

## Available tools

| Tool | What it does |
|------|-------------|
| `git_clone` | `git clone --depth 1 <url>` into `WORKSPACE_ROOT/<local_path>` |
| `oc_new_build` | Create a BuildConfig via `oc new-build` |
| `oc_start_build` | Trigger a build; returns the build name for use with `wait_for_build` |
| `wait_for_build` | Poll `oc get build/<name>` until Complete / Failed / Cancelled / Error or timeout |
| `helm_install` | `helm upgrade --install --wait` against a chart in `WORKSPACE_ROOT` |

## Running locally (development)

Install with [uv](https://docs.astral.sh/uv/) (recommended) or pip:

```bash
uv sync
# or
pip install -e .
```

Log in to OpenShift first, then start the server:

```bash
oc login --token=<token> --server=https://api.mycluster.example.com:6443

WORKSPACE_ROOT=/tmp/mcp-workspace \
MCP_BEARER_TOKEN=my-secret-token \
mcp-ocp-server
```

The server binds to `127.0.0.1:8000` by default. The MCP endpoint is at:

```
http://127.0.0.1:8000/mcp
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BIND_HOST` | `127.0.0.1` | Set to `0.0.0.0` in containers |
| `PORT` | `8000` | HTTP port |
| `WORKSPACE_ROOT` | `/tmp/workspace` | Base directory for clones and Helm charts |
| `POD_NAMESPACE` | `default` | Target OpenShift namespace for `oc` / `helm` |
| `KUBECONFIG` | *(auto in-cluster)* | Path to kubeconfig; auto-generated from SA token when running in a pod |
| `MCP_BEARER_TOKEN` | *(unset = no auth)* | When set, all requests must carry `Authorization: Bearer <value>` |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Building the container image

```bash
docker build \
  --build-arg OC_VERSION=4.16.0 \
  --build-arg HELM_VERSION=3.17.0 \
  -t quay.io/myorg/mcp-ocp-server:latest .

docker push quay.io/myorg/mcp-ocp-server:latest
```

The image is based on **Red Hat UBI 9 / Python 3.11**, runs as UID 1001 (non-root), and is compatible with OpenShift's `restricted-v2` SCC.

## Deploying to OpenShift with Helm

### 1. Create a bearer-token Secret

```bash
oc create secret generic mcp-bearer-token \
  --from-literal=token=$(openssl rand -hex 32) \
  -n my-namespace
```

### 2. Install the Helm chart

```bash
helm upgrade --install mcp-server ./charts/mcp-server \
  -n my-namespace \
  --set image.repository=quay.io/myorg/mcp-ocp-server \
  --set image.tag=latest \
  --set bearerTokenSecret.name=mcp-bearer-token
```

The chart creates:

| Resource | Purpose |
|----------|---------|
| `ServiceAccount` | Pod identity; token used for `oc` / `helm` API calls |
| `Role` + `RoleBinding` | Namespaced permissions (builds, imagestreams, common Helm targets) |
| `Deployment` | Runs the MCP server image |
| `Service` (ClusterIP) | Internal access to the MCP HTTP port |
| `Route` (edge TLS) | External HTTPS access via the OpenShift router |

### 3. Get the Route URL

```bash
oc get route mcp-server -n my-namespace -o jsonpath='{.spec.host}'
```

The MCP endpoint is `https://<route-host>/mcp`.

### Persistent workspace (optional)

By default the workspace uses an `emptyDir` (lost on pod restart). For a persistent workspace across restarts:

```bash
helm upgrade mcp-server ./charts/mcp-server \
  -n my-namespace \
  --set workspace.storageType=pvc \
  --set workspace.pvc.size=10Gi
```

### Extending RBAC for your application chart

The default `Role` covers common build and Helm targets. If your application chart creates additional resource types, append rules via values:

```yaml
# my-values.yaml
rbac:
  extraRules:
    - apiGroups: ["apps"]
      resources: ["statefulsets"]
      verbs: ["get", "list", "create", "update", "patch", "delete"]
    - apiGroups: ["networking.k8s.io"]
      resources: ["networkpolicies"]
      verbs: ["get", "list", "create", "update", "patch", "delete"]
```

```bash
helm upgrade mcp-server ./charts/mcp-server -n my-namespace -f my-values.yaml
```

## Connecting Cursor

Add the following to your Cursor MCP settings (`~/.cursor/mcp.json` or workspace `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "ocp-ci-cd": {
      "url": "https://<route-host>/mcp",
      "headers": {
        "Authorization": "Bearer <your-token>"
      }
    }
  }
}
```

For local development (no TLS, no auth):

```json
{
  "mcpServers": {
    "ocp-ci-cd": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

## Security notes

- The Route uses **edge TLS termination** — TLS is terminated at the OpenShift router; traffic inside the cluster is plain HTTP. For stricter requirements, switch to `tls.termination: reencrypt` and configure a pod certificate.
- `MCP_BEARER_TOKEN` is the application-layer auth. Supply it via a Kubernetes Secret (see above) rather than as a plain env var or values override.
- The pod's ServiceAccount has a **namespaced `Role`** only — no `ClusterRole`. All `oc` and `helm` operations target the pod's own namespace.
- When `MCP_BEARER_TOKEN` is not set, the server logs a warning and the endpoint is unauthenticated. This is acceptable only when the server is not reachable externally (e.g. local dev on `127.0.0.1`).
- In-cluster kubeconfig is written from the projected service-account token (`tokenFile` pointer, not embedded), so it stays valid after token rotation.

## Typical end-to-end flow

```
git_clone         Clone the application repo into WORKSPACE_ROOT
oc_new_build      Create a BuildConfig for the app image
oc_start_build    Trigger the build → returns build_name
wait_for_build    Block until build_name reaches Complete (or fail fast)
helm_install      Deploy the Helm chart at the repo root into the pod namespace
```
