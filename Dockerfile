# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------
# Stage 1 — download oc and helm binaries
# -----------------------------------------------------------------------
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest AS tools

# Pin versions via build args so CI can override without touching this file.
ARG OC_VERSION=4.16.0
ARG HELM_VERSION=3.17.0

# $TARGETARCH is set automatically by BuildKit (amd64 / arm64).
ARG TARGETARCH=amd64

RUN microdnf install -y tar gzip curl && microdnf clean all

# Install the OpenShift CLI (oc).  The tarball also contains kubectl.
RUN curl -fsSL \
      "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OC_VERSION}/openshift-client-linux.tar.gz" \
      -o /tmp/oc.tar.gz && \
    tar -xzf /tmp/oc.tar.gz -C /usr/local/bin oc kubectl && \
    rm /tmp/oc.tar.gz && \
    oc version --client

# Install Helm.
RUN HELM_OS=linux && \
    curl -fsSL \
      "https://get.helm.sh/helm-v${HELM_VERSION}-${HELM_OS}-${TARGETARCH}.tar.gz" \
      -o /tmp/helm.tar.gz && \
    tar -xzf /tmp/helm.tar.gz -C /tmp && \
    mv /tmp/${HELM_OS}-${TARGETARCH}/helm /usr/local/bin/helm && \
    rm -rf /tmp/helm* /tmp/${HELM_OS}-* && \
    helm version

# -----------------------------------------------------------------------
# Stage 2 — Python runtime
# -----------------------------------------------------------------------
# ubi9/python-311 runs as UID 1001 by default, which satisfies OpenShift's
# restricted-v2 SCC (no specific UID required, non-root mandatory).
FROM registry.access.redhat.com/ubi9/python-311:latest

USER root
RUN microdnf install -y git && microdnf clean all

# Copy CLI binaries from Stage 1.
COPY --from=tools /usr/local/bin/oc    /usr/local/bin/oc
COPY --from=tools /usr/local/bin/helm  /usr/local/bin/helm

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir .

# Workspace directory for git clones and Helm chart paths.
# Mount an emptyDir or PVC over /workspace in the Helm chart.
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
