# Build from repository root. Release tooling must supply a reviewed digest-pinned
# Python 3.12 base; this component must never share the Go worker's credentials.
ARG PYTHON_IMAGE
FROM public.ecr.aws/docker/library/golang:1.26.9-trixie@sha256:cf7e928cb9edc2ed8b3cc6fe77ee1ee30b9fea28149564c8840097321a7600ea AS kubectl-builder
WORKDIR /src
RUN curl -fsSL https://github.com/kubernetes/kubernetes/archive/refs/tags/v1.31.4.tar.gz -o /tmp/kubernetes.tar.gz && \
    echo 'ff3a2e9cae3b4734be4a6cdfeb933830c63219eaeda05cc4e59b7559fcde52b0  /tmp/kubernetes.tar.gz' | sha256sum -c - && \
    tar -xzf /tmp/kubernetes.tar.gz --strip-components=1 && rm /tmp/kubernetes.tar.gz
COPY modules/domain-apps/superplane/executor/kubectl-build /build-tools
RUN git apply /build-tools/dependencies.patch && \
    GOTOOLCHAIN=local GOWORK=off go list -mod=readonly -deps ./cmd/kubectl > /dev/null
RUN --network=none mkdir /out && GOTOOLCHAIN=local sh /build-tools/build.sh

FROM public.ecr.aws/docker/library/golang:1.26.9-trixie@sha256:cf7e928cb9edc2ed8b3cc6fe77ee1ee30b9fea28149564c8840097321a7600ea AS terraform-builder
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
FROM public.ecr.aws/docker/library/python:3.14.8-slim-trixie@sha256:cfe2e24a75302a15934d37c2d86412893c0aa934dc3a97cbb439d04c01890ca9 AS tools
ARG TARGETARCH=amd64
RUN test "$TARGETARCH" = amd64 && \
    python -c 'import sys; assert sys.version_info[:3] == (3, 14, 8)' && \
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
RUN --network=none /opt/awscli-adp/bin/aws --version | grep -F 'aws-cli/2.37.4 Python/3.14.8'
COPY modules/domain-apps/superplane/executor/awscli-build/source-build.json /opt/awscli-adp/source-build.json
COPY modules/domain-apps/superplane/executor/awscli-build/verify_uuid_consumer.c /tmp/verify_uuid_consumer.c
RUN --network=none cc -O2 -Wall -Wextra -Werror /tmp/verify_uuid_consumer.c \
    -ldl -o /tmp/adp-awscli-uuid-check

FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY modules/harness/jobs /build/modules/harness/jobs
COPY modules/domain-apps/superplane /build/modules/domain-apps/superplane
# The maintained staging script copies policies, Terraform locks/scripts and
# bootstrap assets into their own distributions. No API package is installed.
RUN bash modules/domain-apps/superplane/src/superplane-api/scripts/stage-domain-auth.sh
RUN python -m venv /opt/executor && /opt/executor/bin/pip install --no-cache-dir \
    pip==26.2.2 \
    --constraint /build/modules/domain-apps/superplane/executor/runtime-constraints.txt \
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
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/pip-26.2.2-py3-none-any.whl
RUN --network=none python -m pip install --no-index --no-deps --no-cache-dir /tmp/pip-26.2.2-py3-none-any.whl \
    && rm /tmp/pip-26.2.2-py3-none-any.whl
COPY --from=build /opt/executor /opt/executor
COPY --from=terraform-builder /out/terraform /usr/local/bin/terraform
COPY --from=terraform-builder /src/LICENSE /usr/share/licenses/superplane-terraform/LICENSE
COPY --from=terraform-builder /build-tools /usr/share/licenses/superplane-terraform/build
COPY --from=kubectl-builder /out/kubectl /usr/local/bin/kubectl
COPY --from=kubectl-builder /src/LICENSES /usr/share/licenses/superplane-kubectl/upstream
COPY --from=kubectl-builder /build-tools /usr/share/licenses/superplane-kubectl/build
COPY --from=tools /opt/awscli-adp /opt/awscli-adp
RUN --network=none \
    --mount=type=bind,source=modules/domain-apps/superplane/executor/awscli-build,target=/awscli-maintenance,readonly \
    python /awscli-maintenance/maintain_libuuid.py
RUN ln -s /opt/awscli-adp/bin/aws /usr/local/bin/aws && \
    mkdir -p /var/lib/superplane-lifecycle && \
    chown 65531:65532 /var/lib/superplane-lifecycle && \
    chmod 0700 /var/lib/superplane-lifecycle
USER 65531:65532
ENV PATH="/opt/executor/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AWS_MAX_ATTEMPTS=1 AWS_RETRY_MODE=standard \
    SUPERPLANE_LIFECYCLE_STATE_DIR=/var/lib/superplane-lifecycle
# The tools-stage check cannot establish compatibility with the selected Python
# base. Exercise the copied binaries under the final non-root runtime as well;
# incompatible libc/Python combinations must fail the build, not the first task.
RUN --network=none \
    --mount=from=tools,source=/tmp/adp-awscli-uuid-check,target=/tmp/adp-awscli-uuid-check,readonly \
    python -c 'import sys; assert sys.version_info[:2] == (3, 12)' && \
    terraform version && kubectl version --client && aws --version && \
    LD_LIBRARY_PATH=/opt/awscli-adp/lib/aws-cli /tmp/adp-awscli-uuid-check

FROM runtime AS controller-service
ENTRYPOINT ["superplane-execution-service"]

FROM runtime AS paid-worker
ENTRYPOINT ["superplane-paid-worker"]
