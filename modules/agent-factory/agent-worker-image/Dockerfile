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
# Stage 2: Build the Codex SDK execution adapter
# -----------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/node:24-bookworm-slim AS codex-builder

WORKDIR /build
COPY modules/agent-factory/codex-reviewer/package.json modules/agent-factory/codex-reviewer/package-lock.json ./
RUN npm ci
COPY modules/agent-factory/codex-reviewer/tsconfig.json ./
COPY modules/agent-factory/codex-reviewer/src/ ./src/
RUN npm run build && npm prune --omit=dev

# -----------------------------------------------------------------------------
# Stage 3: Stage personas and skills
# -----------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/debian:trixie-slim AS stager

WORKDIR /stage

# Copy the staging script
COPY modules/agent-factory/agent-worker-image/stage-personas.sh /stage/stage-personas.sh
RUN chmod +x /stage/stage-personas.sh

# Copy persona and skill source trees
COPY modules/agent-factory/rules/personas/ /source/agent-factory/personas/
COPY modules/agent-factory/skills/ /source/agent-factory/skills/
COPY modules/domain-apps/ /source/domain-apps/

# Run the staging script to assemble /stage/personas and /stage/skills
RUN /stage/stage-personas.sh /source /stage

# -----------------------------------------------------------------------------
# Stage 4: Final runtime image
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

# Install the OpenAI Codex CLI (issue #2704, EPIC #2702). Pinned so the image
# is reproducible and doesn't inherit a surprise "latest". Invoked headlessly
# as `codex exec` against Bedrock (openai.gpt-5.5); config baked in below.
ARG CODEX_VERSION=0.145.0
RUN npm install -g "@openai/codex@${CODEX_VERSION}" && \
    codex --version

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

# Install bun runtime — required by AIDLC v2 hooks and engine tools which are
# bun TypeScript. Pinned static binary from GitHub releases (same pattern as
# yq/fzf/gh/terraform above).
ARG BUN_VERSION=1.2.15
RUN curl -fsSL "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64.zip" \
      -o /tmp/bun.zip && \
    unzip -q /tmp/bun.zip -d /tmp && \
    mv /tmp/bun-linux-x64/bun /usr/local/bin/bun && \
    chmod +x /usr/local/bin/bun && \
    rm -rf /tmp/bun.zip /tmp/bun-linux-x64 && \
    bun --version

# Install pip-based Python tools (uv, poetry, ruff — the pre-submit table in
# agent-worker.ts tells agents to run `ruff check`; it must exist in the image).
RUN pip3 install --no-cache-dir --break-system-packages uv poetry ruff

# Python runtime deps for entrypoint.py + lib/ and bundled skills.
#   boto3             — Secrets Manager, STS, DynamoDB clients (vault_client, sts_assume)
#   PyJWT[crypto]     — GitHub App installation-token JWT minting (github_token)
#   requests          — HTTPS calls to GitHub API (github_token)
#   cryptography      — Ed25519 verification of gateway model-policy decisions
#   pydantic          — shared contract models
RUN pip3 install --no-cache-dir --break-system-packages \
    'boto3>=1.34' \
    'rfc8785==0.1.4' \
    'PyJWT[crypto]>=2.9' \
    'requests>=2.31' \
    'cryptography>=42.0' \
    'pydantic>=2.0'

# Install optional app requirements from the same domain staging contract.
COPY --from=stager /stage/requirements/ /tmp/domain-requirements/
RUN find /tmp/domain-requirements -name '*.txt' -exec \
    pip3 install --no-cache-dir --break-system-packages -r {} \;

# Create non-root user
RUN groupadd -g 1001 agent && \
    useradd -m -u 1001 -g agent -s /bin/bash agent && \
    mkdir -p /app /work && \
    chown -R agent:agent /app /work

# GIT_ASKPASS helper reads token from a file kept fresh by token-refresh.ts,
# with env-var fallback for backward compat. gh wrapper does the same for the
# gh CLI. Both solve the frozen-env problem in SDK subprocesses (issue #1469).
# Move real gh binary so the wrapper can shadow it at /usr/local/bin/gh.
RUN mv /usr/local/bin/gh /usr/bin/gh
COPY --chown=agent:agent modules/agent-factory/agent-worker-image/git-askpass-helper /usr/local/bin/git-askpass-helper
COPY --chown=agent:agent modules/agent-factory/agent-worker-image/gh-wrapper /usr/local/bin/gh
RUN chmod +x /usr/local/bin/git-askpass-helper /usr/local/bin/gh && \
    git config --system --add safe.directory /work/repo

# Copy pre-built agent runtime from builder stage
COPY --from=builder --chown=agent:agent /build/dist/ /app/dist/
COPY --from=builder --chown=agent:agent /build/prod_modules/ /app/node_modules/

# Codex is an execution engine inside the same worker image. Keep its dependency
# tree isolated so SDK versions cannot perturb the Claude worker runtime.
COPY --from=codex-builder --chown=agent:agent /build/dist/ /app/codex-reviewer/dist/
COPY --from=codex-builder --chown=agent:agent /build/node_modules/ /app/codex-reviewer/node_modules/
COPY --from=codex-builder --chown=agent:agent /build/package.json /app/codex-reviewer/package.json
COPY --chown=agent:agent modules/agent-factory/codex-reviewer/prompts/ /app/codex-reviewer/prompts/

# Copy staged personas and skills
COPY --from=stager --chown=agent:agent /stage/personas/ /app/personas/
# Ship the same issue contract for hosted workers outside the ADP repository.
COPY --chown=agent:agent modules/agent-factory/rules/agents/issue-authoring.md /app/rules/agents/issue-authoring.md
COPY --chown=agent:agent modules/agent-factory/rules/templates/developer-issue.md /app/rules/templates/developer-issue.md
COPY --from=stager --chown=agent:agent /stage/skills/ /app/skills/

# Codex CLI config (issue #2704). Codex reads $HOME/.codex/config.toml; the
# runtime HOME is /home/agent (non-root `agent` user), so it lands there —
# NOT /root/.codex. Auth is the pod IRSA credential chain (zero secrets).
COPY --chown=agent:agent modules/agent-factory/agent-worker-image/codex-config.toml /home/agent/.codex/config.toml

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

# Install adp-trigger CLI (agent-to-agent dispatch via API, #2153).
# Same pattern as adp-cred: Python package + shell wrapper.
COPY --chown=agent:agent modules/agent-factory/agent-worker-image/adp_trigger/ /app/adp_trigger/
RUN printf '#!/bin/sh\nexec python3 -m adp_trigger "$@"\n' > /usr/local/bin/adp-trigger && \
    chmod +x /usr/local/bin/adp-trigger

# Install adp-review CLI (formal PR review submission, #5350). Same pattern again.
# This is the ONLY sanctioned path for a reviewer to publish a verdict: it catches
# GitHub's self-review 422 (author and reviewer are the same App), publishes the
# verdict anyway, and names the pending human approval instead of silently
# downgrading to a comment that sets no reviewDecision.
COPY --chown=agent:agent modules/agent-factory/agent-worker-image/adp_review/ /app/adp_review/
RUN printf '#!/bin/sh\nexec python3 -m adp_review "$@"\n' > /usr/local/bin/adp-review && \
    chmod +x /usr/local/bin/adp-review

# Issue #5146: the shared review-result contract. ONE normative validator, read by
# the gateway, the orchestration tick and this worker, so the producer here and the
# consumer there cannot drift into disagreeing (the #4029 failure).
#
# Until now this image did NOT carry it, so `lib/review_result.py` spelled the
# contract's constants and regex patterns locally and the only thing keeping the two
# in step was a CI job. That is two hand-maintained copies of one rule, which is the
# drift the shared contract exists to prevent — and the producer could not validate
# its own output before emitting it, so a malformed artifact travelled all the way to
# the gateway to be refused.
#
# This image's build context is the repository root (`codebuild/bs-agent-runtime.yml`
# builds with `-f modules/.../Dockerfile .`), so unlike the gateway's context the
# tree is reachable directly and needs no staging step. pydantic is already installed
# above. `lib/contract_selfcheck.py` is the build gate.
COPY --chown=agent:agent contracts/ /app/contracts/

# Set working directory and switch to non-root user
WORKDIR /work
USER agent

# Environment defaults
ENV NODE_ENV=production
ENV HOME=/home/agent
ENV PATH="/app/node_modules/.bin:${PATH}"
ENV PYTHONPATH="/app:${PYTHONPATH}"

# Codex v2 gateway cutover (#2713): the custom `adp-gateway` provider in
# codex-config.toml declares env_key=ADP_GATEWAY_PLACEHOLDER_KEY. Codex 0.142.5
# requires this env var to EXIST (a missing var is a hard startup error) but
# never validates its value — the sigv4-proxy sidecar strips the Bearer header
# and re-signs with pod IRSA. This is a placeholder, NOT a secret: no tenant API
# key is minted (SigV4-only invariant). Do not source it from Secrets Manager.
ENV ADP_GATEWAY_PLACEHOLDER_KEY="unused-sidecar-restrips-and-resigns"

# Healthcheck: verify node and python are available
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD node --version && python3 --version

ENTRYPOINT ["python3", "/app/entrypoint.py"]
