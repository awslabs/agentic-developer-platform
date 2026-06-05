# =============================================================================
# adp-agent worker image
# Base: python:3.13-slim-bookworm (Debian, Python pre-installed)
# Pulls from Debian apt mirrors (Fastly CDN) — more reliable than
# archive.ubuntu.com, which had repeated connection-timeout outages
# blocking our builds.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Build the TypeScript agent runtime
# -----------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/node:24-bookworm-slim AS builder

WORKDIR /build

# Install dependencies first (layer cache)
COPY modules/agent-factory/agent/package.json modules/agent-factory/agent/package-lock.json* ./
RUN npm ci --omit=dev --ignore-scripts && \
    cp -r node_modules /build/prod_modules && \
    npm ci

# Copy source and compile
COPY modules/agent-factory/agent/tsconfig.json ./
COPY modules/agent-factory/agent/src/ ./src/
RUN npx tsc --project tsconfig.json

# -----------------------------------------------------------------------------
# Stage 2: Stage personas and skills
# -----------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/debian:trixie-slim AS stager

WORKDIR /stage

# Copy the staging script
COPY modules/agent-factory/agent-worker-image/stage-personas.sh /stage/stage-personas.sh
RUN chmod +x /stage/stage-personas.sh

# Copy persona and skill source trees
COPY modules/agent-factory/rules/personas/ /source/agent-factory/personas/
COPY modules/domain-apps/ /source/domain-apps/

# Run the staging script to assemble /stage/personas and /stage/skills
RUN /stage/stage-personas.sh /source /stage

# -----------------------------------------------------------------------------
# Stage 3: Final runtime image
# -----------------------------------------------------------------------------
# python:3.13-slim-bookworm includes Python 3.13 + pip, based on Debian 12.
# Uses deb.debian.org (Fastly CDN) for apt — far more reliable than
# Ubuntu's archive.ubuntu.com.
FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="adp-agent"
LABEL org.opencontainers.image.description="ADP hosted agent worker image"
LABEL org.opencontainers.image.source="https://github.com/aws-e/adp"

ENV DEBIAN_FRONTEND=noninteractive

# System packages. Python + pip come with the base image, so we only install:
#   build tools (for native node modules in customer repos)
#   git, curl, wget, ca-certificates, gnupg (version control + downloads)
#   ripgrep, jq (CLI tools the agent uses)
#   unzip, sudo, procps (required by various downstream installs)
#
# fzf is installed separately from GitHub releases (not apt) to avoid
# Debian's Go 1.19 toolchain CVEs. fd-find comes from apt.
RUN apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libssl-dev \
    libffi-dev \
    git \
    curl \
    wget \
    ca-certificates \
    gnupg \
    ripgrep \
    fd-find \
    jq \
    unzip \
    zip \
    sudo \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install yq (YAML processor). Pinned to avoid inheriting Go stdlib CVEs
# from an unpredictable "latest" binary (Go 1.25+ clears all 6 C3 CVEs).
ARG YQ_VERSION=4.53.2
RUN curl -fsSL "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_amd64" \
    -o /usr/local/bin/yq && chmod +x /usr/local/bin/yq

# Install fzf from official tarball (not apt) to avoid inheriting
# Debian bookworm's Go 1.19 toolchain CVEs (6 critical Go stdlib CVEs:
# CVE-2023-24531, CVE-2023-24540, CVE-2023-29402, CVE-2023-29404,
# CVE-2023-29405, CVE-2024-24790). The apt package is built with
# golang-1.19; the official release is compiled with Go 1.23+.
ARG FZF_VERSION=0.57.0
RUN curl -fsSL "https://github.com/junegunn/fzf/releases/download/v${FZF_VERSION}/fzf-${FZF_VERSION}-linux_amd64.tar.gz" \
      -o /tmp/fzf.tar.gz && \
    tar xzf /tmp/fzf.tar.gz -C /usr/local/bin fzf && \
    chmod +x /usr/local/bin/fzf && \
    rm /tmp/fzf.tar.gz && \
    fzf --version

# Install Node.js 24 from NodeSource (works on Debian bookworm).
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install pnpm and yarn via corepack. The Node 24 image ships a yarn stub
# at /usr/bin/yarn, so `npm install -g yarn` fails with EEXIST. Corepack
# is the modern way to install both and handles replacement idempotently.
RUN corepack enable && \
    corepack prepare pnpm@latest --activate && \
    corepack prepare yarn@stable --activate

# Install GitHub CLI from official tarball (not apt) to avoid inheriting
# Debian's Go 1.19 toolchain CVEs (7 critical Go stdlib CVEs cleared).
ARG GH_VERSION=2.92.0
RUN curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
      -o /tmp/gh.tar.gz && \
    tar xzf /tmp/gh.tar.gz -C /tmp && \
    mv /tmp/gh_${GH_VERSION}_linux_amd64/bin/gh /usr/local/bin/gh && \
    chmod +x /usr/local/bin/gh && \
    rm -rf /tmp/gh.tar.gz /tmp/gh_${GH_VERSION}_linux_amd64 && \
    gh --version

# Install AWS CLI v2 (binary install from AWS — mirror-independent)
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscliv2.zip

# Install kubectl
RUN curl -fsSL "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl

# Install Terraform — required by deploy-test agents running platform/infra,
# gateway/infra, agent-factory/infra, webhook-ingress/infra apply steps.
# Pinned to 1.15.3 (latest stable as of 2026-05-13) which satisfies the
# strictest module constraint (gateway/infra requires >= 1.14.0).
ARG TERRAFORM_VERSION=1.15.3
ARG TERRAFORM_SHA256=c3d4b579064745a5f7e918125db23b12ba52a8a7287adb9f32c49d637e02e3bf
RUN curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
      -o /tmp/terraform.zip && \
    echo "${TERRAFORM_SHA256}  /tmp/terraform.zip" | sha256sum -c - && \
    unzip -q /tmp/terraform.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/terraform && \
    rm /tmp/terraform.zip && \
    terraform version

# Install pip-based Python tools (uv, poetry). The slim image has pip built-in.
RUN pip3 install --no-cache-dir --break-system-packages uv poetry

# Python runtime deps for entrypoint.py + lib/ and bundled skills.
#   boto3             — Secrets Manager, STS, DynamoDB clients (vault_client, sts_assume)
#   PyJWT[crypto]     — GitHub App installation-token JWT minting (github_token)
#   requests          — HTTPS calls to GitHub API (github_token)
#   bedrock-agentcore — AgentCore Browser client (generate_ws_headers for
#                       SigV4-signed CDP WebSocket) used by the url-analysis skill
#   playwright        — Chromium automation over CDP for url-analysis
#   pydantic          — evidence_schema.py / verdict.py models
#   Pillow            — shrink_for_claude image resize for Bedrock image input
RUN pip3 install --no-cache-dir --break-system-packages \
    'boto3>=1.34' \
    'PyJWT[crypto]>=2.9' \
    'requests>=2.31' \
    'bedrock-agentcore>=0.1' \
    'playwright>=1.45' \
    'pydantic>=2.0' \
    'Pillow>=10.0'

# Create non-root user
RUN groupadd -g 1001 agent && \
    useradd -m -u 1001 -g agent -s /bin/bash agent && \
    mkdir -p /app /work && \
    chown -R agent:agent /app /work

# GIT_ASKPASS reads $GITHUB_TOKEN at every git network call — zero disk persistence,
# transparent token refresh when the env var rotates. See token-refresh.ts.
RUN printf '#!/bin/sh\necho "${GITHUB_TOKEN}"\n' > /usr/local/bin/git-askpass-helper && \
    chmod +x /usr/local/bin/git-askpass-helper && \
    git config --system --add safe.directory /work/repo

# Copy pre-built agent runtime from builder stage
COPY --from=builder --chown=agent:agent /build/dist/ /app/dist/
COPY --from=builder --chown=agent:agent /build/prod_modules/ /app/node_modules/

# Copy staged personas and skills
COPY --from=stager --chown=agent:agent /stage/personas/ /app/personas/
COPY --from=stager --chown=agent:agent /stage/skills/ /app/skills/

# Copy entrypoint and its lib/ support modules (vault_client, github_token, sts_assume)
COPY --chown=agent:agent modules/agent-factory/agent-worker-image/entrypoint.py /app/entrypoint.py
COPY --chown=agent:agent modules/agent-factory/agent-worker-image/lib/ /app/lib/

# Install adp-cred CLI (vault credential access for skills, #137).
# /usr/local/bin/adp-cred is a small shell wrapper that execs the Python
# package at /app/adp_cred/. No symlink; the package doesn't ship a
# wrapper script of its own.
COPY --chown=agent:agent modules/agent-factory/agent-worker-image/adp_cred/ /app/adp_cred/
RUN printf '#!/bin/sh\nexec python3 -m adp_cred "$@"\n' > /usr/local/bin/adp-cred && \
    chmod +x /usr/local/bin/adp-cred

# Set working directory and switch to non-root user
WORKDIR /work
USER agent

# Environment defaults
ENV NODE_ENV=production
ENV HOME=/home/agent
ENV PATH="/app/node_modules/.bin:${PATH}"
ENV PYTHONPATH="/app:${PYTHONPATH}"

# Healthcheck: verify node and python are available
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD node --version && python3 --version

ENTRYPOINT ["python3", "/app/entrypoint.py"]
