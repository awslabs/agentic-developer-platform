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

# AWS CLI's official 2.37.4 bundle embeds vulnerable Python 3.14.6.
# Build the maintained portable-exe source distribution with fixed Python.
# This executable is ADP-built; the AWS release ZIP signature does not cover it.
FROM public.ecr.aws/docker/library/python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS tools
ARG TARGETARCH=amd64
RUN test "$TARGETARCH" = amd64 && \
    python -c 'import sys; assert sys.version_info[:3] == (3, 14, 7)' && \
    apt-get update && apt-get install -y --no-install-recommends build-essential binutils && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src/awscli
ADD --checksum=sha256:c4d840d5975cea7f3bb51441c6f602312e9ec9dae9afaf725561a250eee73a90 https://codeload.github.com/aws/aws-cli/tar.gz/d22f2115447d212da4af5fc35e440ec7f960aad4 /tmp/awscli-source.tar.gz
RUN tar -xzf /tmp/awscli-source.tar.gz --strip-components=1 && rm /tmp/awscli-source.tar.gz
# Upstream's download-deps backend enforces its bundled dependency hash locks.
RUN ./configure --with-install-type=portable-exe --with-download-deps --prefix=/opt/awscli-adp && \
    make -j2 && make install && \
    cp LICENSE.txt /opt/awscli-adp/LICENSE.txt && \
    cp -r build/exe/aws/THIRD_PARTY_LICENSES /opt/awscli-adp/THIRD_PARTY_LICENSES
RUN --network=none /opt/awscli-adp/bin/aws --version | grep -F 'aws-cli/2.37.4 Python/3.14.7'
COPY modules/domain-apps/superplane/executor/awscli-build/source-build.json /opt/awscli-adp/source-build.json

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
COPY --from=tools /opt/awscli-adp /opt/awscli-adp
RUN ln -s /opt/awscli-adp/bin/aws /usr/local/bin/aws && \
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
