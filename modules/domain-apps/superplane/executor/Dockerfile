# Build from repository root. Release tooling must supply a reviewed digest-pinned
# Python 3.12 base; this component must never share the Go worker's credentials.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE} AS tools
# Tool installation happens only in this remote image build. Linux/amd64 is
# explicit; another architecture must add its reviewed artifacts first.
ARG TARGETARCH=amd64
RUN test "$TARGETARCH" = amd64 && apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg unzip && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tools
RUN curl --fail --silent --show-error --location \
      https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip -o terraform.zip && \
    echo '186e0145f5e5f2eb97cbd785bc78f21bae4ef15119349f6ad4fa535b83b10df8  terraform.zip' | sha256sum -c - && \
    unzip terraform.zip terraform && chmod 0755 terraform && \
    curl --fail --silent --show-error --location \
      https://dl.k8s.io/release/v1.31.4/bin/linux/amd64/kubectl -o kubectl && \
    echo '298e19e9c6c17199011404278f0ff8168a7eca4217edad9097af577023a5620f  kubectl' | sha256sum -c - && \
    chmod 0755 kubectl
# Pin both the AWS CLI version and the official AWS signing key fingerprint.
# A keyserver response cannot replace that identity or approve unsigned bytes.
RUN curl --fail --silent --show-error --location \
      'https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xFB5DB77FD5C118B80511ADA8A6310ACC4672475C' -o aws-signing.asc && \
    test "$(gpg --show-keys --with-colons aws-signing.asc | awk -F: '$1=="fpr" {print $10; exit}')" = FB5DB77FD5C118B80511ADA8A6310ACC4672475C && \
    gpg --batch --import aws-signing.asc && \
    curl --fail --silent --show-error --location \
      https://awscli.amazonaws.com/awscli-exe-linux-x86_64-2.31.22.zip -o awscliv2.zip && \
    curl --fail --silent --show-error --location \
      https://awscli.amazonaws.com/awscli-exe-linux-x86_64-2.31.22.zip.sig -o awscliv2.zip.sig && \
    gpg --batch --verify awscliv2.zip.sig awscliv2.zip && \
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
COPY --from=tools /tools/terraform /usr/local/bin/terraform
COPY --from=tools /tools/kubectl /usr/local/bin/kubectl
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
