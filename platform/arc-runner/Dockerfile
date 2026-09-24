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

# ── Node.js 22.x (pinned — bump to fix CVE-2026-21636) ────────────────────────
ENV NODE_VERSION=22.22.3
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
    && rm /tmp/node.tar.xz \
    && node --version && npm --version

# ── Fix GHSA-73rr-hfp2-rc4v: override npm's bundled diff with >=5.2.2 ────────
# Pack the pinned dependency independently: installing inside npm's package
# directory also resolves npm's unpublished development dependencies.
RUN patch_dir=$(mktemp -d) \
    && npm pack diff@5.2.2 --ignore-scripts --pack-destination "$patch_dir" \
    && rm -rf /usr/local/lib/node_modules/npm/node_modules/diff \
    && mkdir -p /usr/local/lib/node_modules/npm/node_modules/diff \
    && tar -xzf "$patch_dir/diff-5.2.2.tgz" --strip-components=1 \
        -C /usr/local/lib/node_modules/npm/node_modules/diff \
    && rm -rf "$patch_dir" \
    && node -e "const v = require('/usr/local/lib/node_modules/npm/node_modules/diff/package.json').version; \
       if (v !== '5.2.2') { console.error('diff version mismatch: ' + v); process.exit(1); } \
       console.log('diff=' + v);"

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
RUN pip3 install --no-cache-dir --break-system-packages boto3==1.35.99 \
    && python3 -c "import boto3, botocore.auth; print(f'boto3={boto3.__version__} botocore={botocore.__version__}')"

# ── AWS CLI v2 ──────────────────────────────────────────────────────────────
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscli.zip \
    && aws --version

# ── Terraform (matches project version per session-context: 1.14.x) ─────────
ENV TERRAFORM_VERSION=1.14.2
RUN curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
        -o /tmp/terraform.zip \
    && unzip -q /tmp/terraform.zip -d /usr/local/bin \
    && rm /tmp/terraform.zip \
    && terraform version

# ── kubectl (matches cluster: 1.35.x) ───────────────────────────────────────
ENV KUBECTL_VERSION=1.35.0
RUN curl -fsSL "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client

# ── Helm 3.17.x ─────────────────────────────────────────────────────────────
RUN curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | \
    DESIRED_VERSION=v3.17.3 bash \
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
# Pinned to v1.24.0 (final release; repo archived 2025-06-03).
# Do NOT use :latest — pin tag + digest to avoid silent CVE regression.
COPY --from=gcr.io/kaniko-project/executor:v1.24.0@sha256:4e7a52dd1f14872430652bb3b027405b8dfd17c4538751c620ac005741ef9698 /kaniko /kaniko
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
