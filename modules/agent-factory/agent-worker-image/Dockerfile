# =============================================================================
# adp-agent worker image
# Base: Ubuntu 24.04 | Node 22 | Python 3.12
# Contains pre-built agent runtime, personas, skills, and CLI tooling.
# Target size: < 2 GB compressed
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
FROM public.ecr.aws/docker/library/ubuntu:24.04 AS stager

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
FROM public.ecr.aws/docker/library/ubuntu:24.04

LABEL org.opencontainers.image.title="adp-agent"
LABEL org.opencontainers.image.description="ADP hosted agent worker image"
LABEL org.opencontainers.image.source="https://github.com/aws-e/adp"

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# System packages: build tools, Python 3.12, git, utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Build essentials for native node modules in customer repos
    build-essential \
    pkg-config \
    libssl-dev \
    libffi-dev \
    # Python 3.12 (default in Ubuntu 24.04)
    python3 \
    python3-pip \
    python3-venv \
    # Version control
    git \
    # Networking
    curl \
    wget \
    ca-certificates \
    gnupg \
    # Text/search utilities
    ripgrep \
    fd-find \
    fzf \
    jq \
    vim-tiny \
    # Misc
    unzip \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install yq (YAML processor)
RUN curl -fsSL "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64" \
    -o /usr/local/bin/yq && chmod +x /usr/local/bin/yq

# Install Node.js 22
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install pnpm and yarn via corepack. The Node 22 image ships a yarn stub
# at /usr/bin/yarn, so `npm install -g yarn` fails with EEXIST. Corepack
# is the modern way to install both and handles replacement idempotently.
RUN corepack enable && \
    corepack prepare pnpm@latest --activate && \
    corepack prepare yarn@stable --activate

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscliv2.zip

# Install kubectl
RUN curl -fsSL "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl

# Install pip-based Python tools (uv, poetry)
RUN pip3 install --no-cache-dir --break-system-packages uv poetry

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

# Set working directory and switch to non-root user
WORKDIR /work
USER agent

# Environment defaults
ENV NODE_ENV=production
ENV HOME=/home/agent
ENV PATH="/app/node_modules/.bin:${PATH}"

# Healthcheck: verify node and python are available
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD node --version && python3 --version

ENTRYPOINT ["python3", "/app/entrypoint.py"]
