# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------
# Stage 1 — download oc and helm binaries
#
# Uses alpine:3 rather than ubi-minimal so that curl/tar are always
# available from the public Alpine mirrors — no Red Hat CDN access or
# subscription is required for this stage.
# -----------------------------------------------------------------------
FROM alpine:3 AS tools

# Pin versions via build args so CI can override without touching this file.
ARG OC_VERSION=4.16.0
ARG HELM_VERSION=3.17.0

# $TARGETARCH is set automatically by BuildKit (amd64 / arm64).
ARG TARGETARCH=amd64

RUN apk add --no-cache curl tar

# Install the OpenShift CLI.
# The OCP mirror uses a plain filename for amd64 and an "-arm64" suffix for arm64.
# We extract the whole tarball to /tmp first (BusyBox tar does not reliably support
# extracting named members directly to a different -C directory), then install the
# binary.  No `oc version --client` here — cross-arch binaries cannot be executed
# inside a build stage on a different-architecture host.
RUN OC_SUFFIX=$([ "$TARGETARCH" = "arm64" ] && echo "-arm64" || echo "") && \
    curl -fsSL \
      "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OC_VERSION}/openshift-client-linux${OC_SUFFIX}.tar.gz" \
      -o /tmp/oc.tar.gz && \
    tar -xzf /tmp/oc.tar.gz -C /tmp && \
    install -m 0755 /tmp/oc /usr/local/bin/oc && \
    rm -f /tmp/oc.tar.gz /tmp/oc /tmp/kubectl /tmp/README.md

# Install Helm (get.helm.sh uses the conventional linux-amd64 / linux-arm64 layout).
RUN curl -fsSL \
      "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
      -o /tmp/helm.tar.gz && \
    tar -xzf /tmp/helm.tar.gz -C /tmp && \
    install -m 0755 /tmp/linux-${TARGETARCH}/helm /usr/local/bin/helm && \
    rm -rf /tmp/helm.tar.gz /tmp/linux-*

# -----------------------------------------------------------------------
# Stage 2 — Python runtime
# -----------------------------------------------------------------------
# ubi9/python-311 is a full UBI 9 S2I image — it uses `dnf` (not microdnf,
# which is only present on ubi9-minimal).  It already runs as UID 1001.
FROM registry.access.redhat.com/ubi9/python-311:latest

USER root
RUN dnf install -y --nodocs git && dnf clean all

# Copy CLI binaries from Stage 1.
COPY --from=tools /usr/local/bin/oc    /usr/local/bin/oc
COPY --from=tools /usr/local/bin/helm  /usr/local/bin/helm

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir .

# Workspace directory — group-writable so OpenShift's arbitrary-UID assignment works.
RUN mkdir -p /workspace && chown 1001:0 /workspace && chmod g+rwX /workspace

USER 1001

ENV WORKSPACE_ROOT=/workspace \
    PORT=8000 \
    BIND_HOST=0.0.0.0 \
    LOG_LEVEL=INFO

EXPOSE 8000

# Liveness / readiness: FastMCP does not expose /healthz by default.
# The Deployment uses a TCP socket probe against port 8000 instead.

CMD ["mcp-ocp-server"]
