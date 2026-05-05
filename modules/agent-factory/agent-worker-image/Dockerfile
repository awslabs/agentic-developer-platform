# =============================================================================
# adp-agent worker image
# Base: python:3.12-slim-bookworm (Debian, Python pre-installed)
# Pulls from Debian apt mirrors (Fastly CDN) — more reliable than
# archive.ubuntu.com, which had repeated connection-timeout outages
# blocking our builds.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Build the TypeScript agent runtime
# -----------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/node:22-bookworm-slim AS builder

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
FROM public.ecr.aws/docker/library/debian:bookworm-slim AS stager

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
# python:3.12-slim-bookworm includes Python 3.12 + pip, based on Debian 12.
# Uses deb.debian.org (Fastly CDN) for apt — far more reliable than
# Ubuntu's archive.ubuntu.com.
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

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
# fzf and fd-find are available in Debian bookworm with the same names;
# vim-tiny is vim-tiny on Debian too. We just drop apt cache after install.
RUN apt-get update && apt-get install -y --no-install-recommends \
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
    fzf \
    jq \
    vim-tiny \
    unzip \
    sudo \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install yq (YAML processor)
RUN curl -fsSL "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64" \
    -o /usr/local/bin/yq && chmod +x /usr/local/bin/yq

# Install Node.js 22 from NodeSource (works on Debian bookworm).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install pnpm and yarn via corepack. The Node 22 image ships a yarn stub
# at /usr/bin/yarn, so `npm install -g yarn` fails with EEXIST. Corepack
# is the modern way to install both and handles replacement idempotently.
RUN corepack enable && \
    corepack prepare pnpm@latest --activate && \
    corepack prepare yarn@stable --activate

# Install GitHub CLI — same apt-repo approach works on Debian.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2 (binary install from AWS — mirror-independent)
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscliv2.zip

# Install kubectl
RUN curl -fsSL "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl

# Install pip-based Python tools (uv, poetry). The slim image has pip built-in.
RUN pip3 install --no-cache-dir --break-system-packages uv poetry

# Python runtime deps for entrypoint.py + lib/
#   boto3        — Secrets Manager, STS, DynamoDB clients (vault_client, sts_assume)
#   PyJWT[crypto] — GitHub App installation-token JWT minting (github_token)
#   requests     — HTTPS calls to GitHub API (github_token)
RUN pip3 install --no-cache-dir --break-system-packages \
    'boto3>=1.34' \
    'PyJWT[crypto]>=2.9' \
    'requests>=2.31'

# Create non-root user
RUN groupadd -g 1001 agent && \
    useradd -m -u 1001 -g agent -s /bin/bash agent && \
    mkdir -p /app /work && \
    chown -R agent:agent /app /work

# Configure git credential helper for GitHub token auth
RUN git config --system credential.helper 'store' && \
    git config --system --add safe.directory '*'

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
