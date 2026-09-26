# Build from repository root. Release tooling must supply a reviewed digest-pinned
# Python 3.12 base; this component must never share the Go worker's credentials.
ARG PYTHON_IMAGE
FROM public.ecr.aws/docker/library/golang:1.26.8-trixie@sha256:bdca99a00bc16590cb1a0bb4e698f5fc5d6a64e4d5eef13d9f18a0ee08e5fa65 AS kubectl-builder
WORKDIR /src
RUN curl -fsSL https://github.com/kubernetes/kubernetes/archive/refs/tags/v1.31.4.tar.gz -o /tmp/kubernetes.tar.gz && \
    echo 'ff3a2e9cae3b4734be4a6cdfeb933830c63219eaeda05cc4e59b7559fcde52b0  /tmp/kubernetes.tar.gz' | sha256sum -c - && \
    tar -xzf /tmp/kubernetes.tar.gz --strip-components=1 && rm /tmp/kubernetes.tar.gz
COPY modules/domain-apps/superplane/executor/kubectl-build /build-tools
RUN git apply /build-tools/dependencies.patch && \
    GOTOOLCHAIN=local GOWORK=off go list -mod=readonly -deps ./cmd/kubectl > /dev/null
RUN --network=none mkdir /out && GOTOOLCHAIN=local sh /build-tools/build.sh

FROM public.ecr.aws/docker/library/golang:1.26.8-trixie@sha256:bdca99a00bc16590cb1a0bb4e698f5fc5d6a64e4d5eef13d9f18a0ee08e5fa65 AS terraform-builder
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN git init . && git remote add origin https://github.com/hashicorp/terraform.git && \
    git fetch --depth=1 origin e044e569c5bc81f82e9a4d7891f37c6fbb0a8a10 && \
    git checkout --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = e044e569c5bc81f82e9a4d7891f37c6fbb0a8a10
COPY modules/domain-apps/superplane/executor/terraform-build /build-tools
RUN git apply --check /build-tools/security.patch && git apply /build-tools/security.patch && \
    go mod download && go mod verify
RUN --network=none mkdir /out && sh /build-tools/build.sh

FROM ${PYTHON_IMAGE} AS tools
# Tool installation happens only in this remote image build. Linux/amd64 is
# explicit; another architecture must add its reviewed artifacts first.
ARG TARGETARCH=amd64
RUN test "$TARGETARCH" = amd64 && apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg unzip && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tools
COPY modules/domain-apps/superplane/executor/awscli-build /awscli-build
# Pin the official renewed key and release bytes; reject expired/error statuses.
RUN curl --fail --silent --show-error --location \
      https://awscli.amazonaws.com/awscli-exe-linux-x86_64-2.37.4.zip -o awscliv2.zip && \
    curl --fail --silent --show-error --location \
      https://awscli.amazonaws.com/awscli-exe-linux-x86_64-2.37.4.zip.sig -o awscliv2.zip.sig && \
    python /awscli-build/verify.py awscliv2.zip awscliv2.zip.sig && \
    echo '0c59444563f4df735eeb5481f6165f95dae546c33761760d8be9855d5cfe2d12  awscliv2.zip' | sha256sum -c - && \
    unzip -q awscliv2.zip && ./aws/install --install-dir /opt/aws-cli --bin-dir /opt/aws-bin

FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY modules/harness/jobs /build/modules/harness/jobs
COPY modules/domain-apps/superplane /build/modules/domain-apps/superplane
# The maintained staging script copies policies, Terraform locks/scripts and
# bootstrap assets into their own distributions. No API package is installed.
RUN bash modules/domain-apps/superplane/src/superplane-api/scripts/stage-domain-auth.sh
RUN python -m venv /opt/executor && /opt/executor/bin/pip install --no-cache-dir \
    /build/modules/domain-apps/superplane/src/superplane-api/vendor/harness-jobs \
    /build/modules/domain-apps/superplane/src/superplane-api/vendor/superplane-contracts \
    /build/modules/domain-apps/superplane/src/superplane-api/vendor/account-factory \
    /build/modules/domain-apps/superplane/src/superplane-api/vendor/account-provisioning \
    /build/modules/domain-apps/superplane/src/superplane-api/vendor/superplane-bootstrap \
    '/build/modules/domain-apps/superplane/src/superplane-api/vendor/workspace-provisioning[worker]' \
    /build/modules/domain-apps/superplane/src/superplane-api/vendor/superplane-executor
RUN /opt/executor/bin/python -c \
    'from pathlib import Path; import workspace_provisioning; root=Path(workspace_provisioning.__file__).parent / "_data"; assert (root / "workspaces/.terraform.lock.hcl").is_file(); assert (root / "workspaces/scripts/apply_workspace_plan.py").is_file(); assert (root / "crds.yaml").is_file()'

FROM ${PYTHON_IMAGE} AS runtime
COPY --from=build /opt/executor /opt/executor
COPY --from=terraform-builder /out/terraform /usr/local/bin/terraform
COPY --from=terraform-builder /src/LICENSE /usr/share/licenses/superplane-terraform/LICENSE
COPY --from=terraform-builder /build-tools /usr/share/licenses/superplane-terraform/build
COPY --from=kubectl-builder /out/kubectl /usr/local/bin/kubectl
COPY --from=kubectl-builder /src/LICENSES /usr/share/licenses/superplane-kubectl/upstream
COPY --from=kubectl-builder /build-tools /usr/share/licenses/superplane-kubectl/build
COPY --from=tools /opt/aws-cli /opt/aws-cli
RUN ln -s /opt/aws-cli/v2/current/bin/aws /usr/local/bin/aws && \
    mkdir -p /var/lib/superplane-lifecycle && \
    chown 65531:65532 /var/lib/superplane-lifecycle && \
    chmod 0700 /var/lib/superplane-lifecycle
USER 65531:65532
ENV PATH="/opt/executor/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AWS_MAX_ATTEMPTS=1 AWS_RETRY_MODE=standard \
    SUPERPLANE_LIFECYCLE_STATE_DIR=/var/lib/superplane-lifecycle

FROM runtime AS controller-service
ENTRYPOINT ["superplane-execution-service"]

FROM runtime AS paid-worker
ENTRYPOINT ["superplane-paid-worker"]
