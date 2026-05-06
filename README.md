# MCP OCP Server

An MCP server that exposes OpenShift build and Helm deployment operations over the [Streamable HTTP](https://modelcontextprotocol.io/specification/latest/basic/transports) transport. Connect any MCP client (e.g. Cursor) to the server's HTTP endpoint and run `git clone` → `openshift_build` → `wait_for_build` → `helm_deploy` as natural-language-driven tool calls.

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
| `git_clone` | `git clone --depth 1 <url>` into `WORKSPACE_ROOT/<application_name>`; `local_path` is optional (defaults to the repo name from the URL). Optional GitHub PAT for private **HTTPS** repos |
| `openshift_build` | Runs `oc new-build --binary --name=… --strategy=docker` then `oc start-build … --from-dir=…`. `git_workspace` defaults to `name` (same folder as clone when names match). Returns `build` / `build_name`, `namespace`, optional `console_url` (set `OPENSHIFT_CONSOLE_BASE_URL`). Reuses existing BuildConfig if present. |
| `wait_for_build` | Poll `oc get build/<name>` until Complete / Failed / Cancelled / Error or timeout |
| `helm_deploy` | `helm upgrade -i <app_name> <chart>` (no `--wait`) with `fullnameOverride` and internal-registry `image.repository`; chart at `<app_name>/chart` or `chart/`; returns `route_url` when `oc get route` finds the app |

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

MCP_API_KEY=my-secret-token \
mcp-ocp-server
```

Default `WORKSPACE_ROOT` is `/workspace`; override if needed (for example `WORKSPACE_ROOT=/tmp/mcp-workspace`).

The server binds to `0.0.0.0:8000` by default. For local access, use:

```
http://127.0.0.1:8000/mcp
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BIND_HOST` | `0.0.0.0` | Bind address (`127.0.0.1` is useful for localhost-only local dev) |
| `PORT` | `8000` | HTTP port |
| `WORKSPACE_ROOT` | `/workspace` | Base directory for clones and Helm charts |
| `OCP_TARGET_NAMESPACE` | *(unset)* | If set, `oc` / `helm` use this namespace. Otherwise see `POD_NAMESPACE`. |
| `POD_NAMESPACE` | *(unset locally)* | In-cluster, injected by the Downward API — **the MCP pod's namespace**. With no `OCP_TARGET_NAMESPACE`, tools use this in-cluster; locally they use your **current `oc` context** namespace (then `default`). |
| `KUBECONFIG` | *(auto in-cluster)* | Path to kubeconfig; auto-generated from SA token when running in a pod |
| `MCP_API_KEY` | *(unset = no auth)* | When set, all requests must carry `X-API-Key: <value>` |
| `GITHUB_TOKEN` | *(unset)* | GitHub personal access token for private **HTTPS** clones when the `git_clone` tool does not pass `github_token` |
| `OPENSHIFT_CONSOLE_BASE_URL` | *(unset)* | Web console origin with no trailing slash (e.g. `https://console-openshift-console.apps.<cluster>`). Enables `console_url` on `openshift_build`. Discover with `oc get route console -n openshift-console -o jsonpath='{.spec.host}'` and prefix `https://`. |
| `OPENSHIFT_INTERNAL_REGISTRY` | `image-registry.openshift-image-registry.svc:5000` | Registry host for `helm_deploy` `image.repository` (`<registry>/<namespace>/<app_name>`). |
| `HELM_DEPLOY_IMAGE_TAG` | `latest` | Image tag passed to `helm_deploy` (`--set image.tag`). |
| `LOG_LEVEL` | `INFO` | Python logging level |

### Private GitHub repositories

Use an **HTTPS** URL (for example `https://github.com/org/private-repo.git`). Either:

- set `GITHUB_TOKEN` in the environment (recommended for OpenShift — inject from a Secret), or
- pass `github_token` on the `git_clone` tool call (avoid logging it in untrusted clients).

SSH URLs are not altered; use SSH keys or an agent if you clone via `git@github.com:...`.

The container image sets default `git config` `user.name` / `user.email` to **OCP_BOT** / **OCP_BOT@orange-bank.ie** so git does not fail identity checks after clone.

### OpenShift target namespace and `openshift_build`

- **`oc` / `helm` tools** resolve the namespace as: `OCP_TARGET_NAMESPACE` → **`POD_NAMESPACE`** (the MCP pod's namespace when running in-cluster) → **active `oc` context** namespace when developing locally → `default` if that cannot be read.
- Before `openshift_build`, `wait_for_build`, and `helm_deploy`, the server may **ensure the target namespace exists**. If the target is **the same as the MCP pod’s namespace** (`POD_NAMESPACE`), this step is skipped — no cluster-scoped `Namespace` GET is required. For **other** targets (`OCP_TARGET_NAMESPACE`), your identity needs permission to read or create namespaces/projects.
- If **`oc new-build` fails because the BuildConfig already exists**, **`openshift_build`** still succeeds and runs **`oc start-build`** (after verifying the BuildConfig with `oc get buildconfig`).
- Binary uploads (`oc start-build --from-dir`) need **`create`** on **`buildconfigs/instantiatebinary`** — the Helm chart `Role` includes this; re-apply the chart if you hit Forbidden on upload.

### `helm_deploy` (template app)

- **Single argument `app_name`:** use the same string as `openshift_build` `name` and as the clone directory (`git_clone` `local_path`, or the URL-derived name when `local_path` is omitted) so the image `image-registry.openshift-image-registry.svc:5000/<namespace>/<app_name>` matches the build output.
- **Chart:** first path under `WORKSPACE_ROOT` that contains `Chart.yaml`: `<app_name>/chart`, then `<app_name>` (chart at clone root — common for template repos), then `chart`.
- **Helm:** `helm upgrade -i <app_name> … --set-string fullnameOverride=<app_name> --set image.repository=… --set image.tag=…` (no `--wait`; idempotent).
- **Route URL:** after success, `oc get route` looks for `app.kubernetes.io/instance=<app_name>` or a Route named `app_name`. Template charts should follow one of those conventions.

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
git_clone              Clone into WORKSPACE_ROOT/<application_name> (optional local_path; else URL-derived)
openshift_build        name (+ optional git_workspace, default name); returns build name, namespace, optional console URL
wait_for_build         Block until build_name reaches Complete (or fail fast)
helm_deploy            helm upgrade -i (no --wait), fullnameOverride + image; route_url when found
```
