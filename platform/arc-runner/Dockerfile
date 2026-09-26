# =============================================================================
# ADP Custom ARC Runner Image
# =============================================================================
# Pre-installs the CLI tools ADP workflows need, so deploy steps don't waste
# 3-5 minutes apt-installing zip/aws/kubectl/terraform on every run.
#
# Adapted from aws-innovate/AISuperPlane's infra/arc-runner/Dockerfile.
#
# Pin the runner release and refresh it before GitHub deprecates the version.
# With ARC updates disabled, a deprecated release exits before accepting jobs.
#
# Build context: this directory (no parent references).
# =============================================================================

# Preserve Terraform 1.14.9 behavior while updating its embedded Go dependencies.
# The reviewed patch fixes the dependency graph; no floating go get runs here.
FROM public.ecr.aws/docker/library/golang:1.26.8-trixie@sha256:bdca99a00bc16590cb1a0bb4e698f5fc5d6a64e4d5eef13d9f18a0ee08e5fa65 AS terraform-build
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/hashicorp/terraform.git \
    && git fetch --depth=1 origin c3c23dd8f1201506dbd21788f482420ee3c1b248 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = c3c23dd8f1201506dbd21788f482420ee3c1b248
COPY terraform-security-dependencies.patch /tmp/terraform-security-dependencies.patch
RUN git apply --check /tmp/terraform-security-dependencies.patch \
    && git apply /tmp/terraform-security-dependencies.patch \
    && sha256sum go.mod go.sum > /tmp/terraform-modules.sha256 \
    && go mod download \
    && go mod verify \
    && go build -mod=readonly -p 2 -buildvcs=false -trimpath \
        -ldflags "-s -w -X github.com/hashicorp/terraform/version.dev=no" \
        -o /out/terraform . \
    && sha256sum -c /tmp/terraform-modules.sha256

# Signed maintained Kaniko release; rebuild only executor with reviewed dependencies.
FROM ghcr.io/osscontainertools/kaniko:v1.28.5@sha256:738807f0e31daf07743f89260a95956dcc6ee4f62553f4f6344c990cabab8675 AS kaniko-release
FROM public.ecr.aws/docker/library/golang:1.26.8-trixie@sha256:bdca99a00bc16590cb1a0bb4e698f5fc5d6a64e4d5eef13d9f18a0ee08e5fa65 AS kaniko-build
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/osscontainertools/kaniko.git \
    && git fetch --depth=1 origin 9c3dcc112b7a71bed4de3bc8c4ed089ef98cbf0c \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 9c3dcc112b7a71bed4de3bc8c4ed089ef98cbf0c
COPY kaniko-security-dependencies.patch /tmp/kaniko-security-dependencies.patch
RUN git apply --check /tmp/kaniko-security-dependencies.patch \
    && git apply /tmp/kaniko-security-dependencies.patch \
    && sha256sum go.mod go.sum > /tmp/kaniko-modules.sha256 \
    && go mod download \
    && go mod verify \
    && go build -mod=readonly -p 1 -buildvcs=false -trimpath \
        -ldflags "-s -w -X github.com/osscontainertools/kaniko/pkg/version.version=v1.28.5" \
        -o /out/executor ./cmd/executor \
    && sha256sum -c /tmp/kaniko-modules.sha256

FROM ghcr.io/actions/actions-runner:2.337.0

ENV DEBIAN_FRONTEND=noninteractive

USER root

# Use authenticated Ubuntu mirrors for both indexes and package downloads.
# Plain HTTP returned a stale package/index pair in CodeBuild (package 404).
RUN find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) \
        -exec sed -i \
        -e 's|http://archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g' \
        -e 's|http://security.ubuntu.com/ubuntu|https://security.ubuntu.com/ubuntu|g' {} +

# ── Base utilities ──────────────────────────────────────────────────────────
RUN apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends \
        curl \
        wget \
        zip \
        unzip \
        git \
        jq \
        ca-certificates \
        gnupg \
        lsb-release \
        software-properties-common \
        sudo \
        apt-transport-https \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 22.x (pinned — security release 2026-09-23: OpenSSL 3.5.6, ────────
#    zlib use-after-free, URL-parsing crash fix)
ENV NODE_VERSION=22.23.3
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
    && rm /tmp/node.tar.xz \
    && node --version && npm --version

# ── Guard GHSA-73rr-hfp2-rc4v: npm >=10.9.8 bundles diff >=5.2.2 natively.
# The manual override was removed when Node 22.23.3 (npm 10.9.9) absorbed
# the fix. This check fails the build if a future Node bump re-introduces
# a vulnerable diff version.
RUN node -e " \
  const v = require('/usr/local/lib/node_modules/npm/node_modules/diff/package.json').version; \
  const [ma,mi,pa] = v.split('.').map(Number); \
  if (ma < 5 || (ma === 5 && mi < 2) || (ma === 5 && mi === 2 && pa < 2)) { \
    console.error('GHSA-73rr-hfp2-rc4v: bundled diff ' + v + ' < 5.2.2'); process.exit(1); } \
  console.log('diff=' + v + ' (>=5.2.2, GHSA-73rr-hfp2-rc4v clear)');"

# ── Python 3.12 + pip (for CodeBuild log parsing, yaml checks, etc.) ────────
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12-venv \
        python3.12-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python3.12 - --break-system-packages \
    && python3 --version && pip3 --version

# ── boto3 (required by assume-customer-creds.py for SigV4 signing — EPIC #1107)
# botocore.auth.SigV4Auth signs the platform-pod's call to
# /internal/v1/credential-assume-role on the platform API Gateway. Without this,
# every cross-account deploy fails at Load deployment config with
# "botocore is required for SigV4 auth. Install boto3."
RUN pip3 install --no-cache-dir --break-system-packages boto3==1.43.102 \
    && python3 -c "import boto3, botocore.auth; print(f'boto3={boto3.__version__} botocore={botocore.__version__}')"

# ── AWS CLI v2 ──────────────────────────────────────────────────────────────
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscli.zip \
    && aws --version

# ── Terraform (matches project version per session-context: 1.14.x) ─────────
ENV TERRAFORM_VERSION=1.14.9
COPY --from=terraform-build /out/terraform /usr/local/bin/terraform
COPY --from=terraform-build /src/LICENSE /usr/local/share/licenses/terraform/LICENSE
COPY terraform-security-NOTICE /usr/local/share/licenses/terraform/NOTICE
RUN terraform version

# ── kubectl (matches cluster: 1.35.x) ───────────────────────────────────────
ENV KUBECTL_VERSION=1.35.9
RUN curl -fsSL "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client

# ── Helm 3 (security refresh, checksum-verified upstream binary) ─────────────
ENV HELM_VERSION=3.22.0
RUN curl -fsSL "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" \
        -o /tmp/helm.tar.gz \
    && echo "1e4ab49e429626cf6c6958d914248b78c9730803c2751b87627e171dc800e7bb  /tmp/helm.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/helm.tar.gz -C /tmp linux-amd64/helm \
    && install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm \
    && rm -rf /tmp/helm.tar.gz /tmp/linux-amd64 \
    && helm version

# ── gh CLI (latest) ─────────────────────────────────────────────────────────
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && out=$(mktemp) \
    && wget -nv -O "$out" https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && cat "$out" | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* "$out" \
    && gh --version

# ── Docker CLI (ECR login/push; actual builds via CodeBuild or Kaniko) ──────
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version

# ── Kaniko executor (daemonless container builds; no Docker daemon needed) ──
# Maintained v1.28.5 helpers plus executor rebuilt with patched dependencies.
# Keep the release digest and exact source commit aligned.
COPY --from=kaniko-release /kaniko /kaniko
COPY --from=kaniko-build /out/executor /kaniko/executor
COPY --from=kaniko-build /src/LICENSE /usr/local/share/licenses/kaniko/LICENSE
COPY kaniko-security-NOTICE /usr/local/share/licenses/kaniko/NOTICE
RUN chmod -R 777 /kaniko
ENV PATH="/kaniko:${PATH}"

# ── Cleanup ─────────────────────────────────────────────────────────────────
RUN apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* \
    && rm -rf /root/.cache/pip

# Back to runner user
USER runner

# The ARC base emits diagnostic lines alongside --version on stdout. Require
# a successful command and the exact standalone version line.
# Verify every tool is accessible as the runner user.
RUN runner_version_output=$(/home/runner/bin/Runner.Listener --version) \
    && printf '%s\n' "$runner_version_output" | grep -x '2\.337\.0' \
    && echo "=== ADP ARC Runner Tool Verification ===" \
    && node --version \
    && python3 --version \
    && aws --version \
    && terraform version \
    && kubectl version --client \
    && helm version \
    && gh --version \
    && docker --version \
    && jq --version \
    && zip -v | head -1 \
    && /kaniko/executor version \
    && echo "=== All tools verified ==="
