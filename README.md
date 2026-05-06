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
| `git_clone` | `git clone --depth 1 <url>` into `WORKSPACE_ROOT/<local_path>`; optional GitHub PAT for private **HTTPS** repos |
| `oc_new_build` | Create a BuildConfig via `oc new-build` (non-binary builds need a `context_path` under `WORKSPACE_ROOT`, default `.`) |
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
MCP_API_KEY=my-secret-token \
mcp-ocp-server
```

The server binds to `0.0.0.0:8000` by default. For local access, use:

```
http://127.0.0.1:8000/mcp
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BIND_HOST` | `0.0.0.0` | Bind address (`127.0.0.1` is useful for localhost-only local dev) |
| `PORT` | `8000` | HTTP port |
| `WORKSPACE_ROOT` | `/tmp/workspace` | Base directory for clones and Helm charts |
| `OCP_TARGET_NAMESPACE` | *(unset)* | If set, `oc` / `helm` use this namespace. Otherwise see `POD_NAMESPACE`. |
| `POD_NAMESPACE` | `prototypes` (local) | In-cluster, injected by the Downward API. With no `OCP_TARGET_NAMESPACE`, this is the target namespace for tools; when unset locally, tools default to **`prototypes`**. |
| `KUBECONFIG` | *(auto in-cluster)* | Path to kubeconfig; auto-generated from SA token when running in a pod |
| `MCP_API_KEY` | *(unset = no auth)* | When set, all requests must carry `X-API-Key: <value>` |
| `GITHUB_TOKEN` | *(unset)* | GitHub personal access token for private **HTTPS** clones when the `git_clone` tool does not pass `github_token` |
| `LOG_LEVEL` | `INFO` | Python logging level |

### Private GitHub repositories

Use an **HTTPS** URL (for example `https://github.com/org/private-repo.git`). Either:

- set `GITHUB_TOKEN` in the environment (recommended for OpenShift — inject from a Secret), or
- pass `github_token` on the `git_clone` tool call (avoid logging it in untrusted clients).

SSH URLs are not altered; use SSH keys or an agent if you clone via `git@github.com:...`.

The container image sets default `git config` `user.name` / `user.email` to **OCP_BOT** / **OCP_BOT@orange-bank.ie** so git does not fail identity checks after clone.

### OpenShift target namespace and `oc new-build`

- **`oc` / `helm` tools** resolve the namespace as: `OCP_TARGET_NAMESPACE` → `POD_NAMESPACE` → **`prototypes`** (so local runs without env vars use `prototypes`).
- Before `oc new-build`, `oc start-build`, `wait_for_build`, and `helm_install`, the server tries to **create the namespace** if it does not exist (`oc create namespace`, then `oc new-project` as a fallback). Your kube user or in-cluster ServiceAccount needs permission to create namespaces/projects.
- If **`oc new-build` fails because the BuildConfig already exists**, the tool **returns success** with a short message that the existing BuildConfig is reused (after verifying it with `oc get buildconfig`).

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

### 1. Choose an API key value

```bash
export MCP_API_KEY="daffy-duck"
```

### 2. Install the Helm chart

```bash
helm upgrade --install mcp-server ./chart \
  -n my-namespace \
  --set image.repository=quay.io/myorg/mcp-ocp-server \
  --set image.tag=latest \
  --set env.MCP_API_KEY="${MCP_API_KEY}"
```

For private GitHub HTTPS clones, set a PAT on the deployment, for example:

```bash
export GITHUB_TOKEN="ghp_..."   # fine-grained or classic PAT with repo scope
helm upgrade --install mcp-server ./chart \
  -n my-namespace \
  --set image.repository=quay.io/myorg/mcp-ocp-server \
  --set image.tag=latest \
  --set env.MCP_API_KEY="${MCP_API_KEY}" \
  --set env.GITHUB_TOKEN="${GITHUB_TOKEN}"
```

Or add `env.GITHUB_TOKEN` in a values file (prefer a Secret reference in production).

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
helm upgrade mcp-server ./chart \
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
helm upgrade mcp-server ./chart -n my-namespace -f my-values.yaml
```

## Connecting Cursor

Add the following to your Cursor MCP settings (`~/.cursor/mcp.json` or workspace `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "ocp-ci-cd": {
      "url": "https://<route-host>/mcp",
      "headers": {
        "X-API-Key": "<your-token>"
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
- `MCP_API_KEY` is the application-layer auth, checked via the `X-API-Key` header.
- The pod's ServiceAccount has a **namespaced `Role`** only — no `ClusterRole`. All `oc` and `helm` operations target the pod's own namespace.
- When `MCP_API_KEY` is not set, the server logs a warning and the endpoint is unauthenticated. This is acceptable only when the server is not reachable externally.
- In-cluster kubeconfig is written from the projected service-account token (`tokenFile` pointer, not embedded), so it stays valid after token rotation.

## Typical end-to-end flow

```
git_clone         Clone the application repo into WORKSPACE_ROOT
oc_new_build      Create a BuildConfig (set context_path to the clone directory, e.g. myrepo, unless the Dockerfile is at WORKSPACE_ROOT)
oc_start_build    Trigger the build → returns build_name
wait_for_build    Block until build_name reaches Complete (or fail fast)
helm_install      Deploy the Helm chart at the repo root into the pod namespace
```
