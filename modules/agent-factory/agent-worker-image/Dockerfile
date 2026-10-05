# Keep Terraform1.15.7 behavior while rebuilding its exact dependency graph.
# Authenticated curl packages and licenses, built and tested in #6540.
# This immutable artifact is a build input, not a deployable worker image.
ARG CURL_SECURITY_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:782f7587ec1a803231eb0fa38518975842d1748df09a6b20f179af1caedf823b
ARG PYTHON314_EXPAT_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-arc-runner@sha256:9e6dd602c215e93c5e3cd489d4dbbd070d69ac7944caee65b0e46965bc49b881
FROM ${CURL_SECURITY_IMAGE} AS curl-security

FROM public.ecr.aws/docker/library/golang:1.26.8-trixie@sha256:bdca99a00bc16590cb1a0bb4e698f5fc5d6a64e4d5eef13d9f18a0ee08e5fa65 AS terraform-security
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
ADD --checksum=sha256:a81fbc916714d01effa519b8fe602d41a2b06bc9cdf898d809891fb57a7427ff https://codeload.github.com/hashicorp/terraform/tar.gz/214239d0a88cefc3919e79805c5ddd42b922af56 /tmp/terraform.tar.gz
RUN tar -xzf /tmp/terraform.tar.gz --strip-components=1 -C /src && rm /tmp/terraform.tar.gz
COPY modules/agent-factory/agent-worker-image/terraform-security-dependencies.patch /tmp/security.patch
RUN git apply --check /tmp/security.patch && git apply /tmp/security.patch \
    && sha256sum go.mod go.sum > /tmp/modules.sha256 \
    && (for attempt in 1 2 3; do \
          if go mod download; then break; fi; \
          [ "$attempt" -lt 3 ] || exit 1; \
          sleep 5; \
        done) \
    && go mod verify \
    && go build -mod=readonly -p 2 -buildvcs=false -trimpath \
      -ldflags "-s -w -X github.com/hashicorp/terraform/version.dev=no" -o /out/terraform . \
    && sha256sum -c /tmp/modules.sha256

# Export the verified CLI without copying the build toolchain or module cache.
FROM scratch AS terraform-security-artifact
COPY --from=terraform-security /out/terraform /terraform

# Exact CPython3.14.7 adapters with statically linked Expat2.8.5.
# Build-only artifact, source and normal XML checks are committed in
# platform/arc-runner/security/high-tools/Dockerfile.python314-expat.
FROM ${PYTHON314_EXPAT_IMAGE} AS python314-expat

# AWS CLI's official 2.37.4 bundle embeds vulnerable Python 3.14.6.
# Build the maintained portable-exe source distribution with fixed Python.
# This executable is ADP-built; the AWS release ZIP signature does not cover it.
FROM public.ecr.aws/docker/library/python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS awscli-security
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
COPY --from=python314-expat /modules/ /opt/awscli-adp/lib/aws-cli/python3.14/lib-dynload/
COPY --from=python314-expat /manifest.json /opt/awscli-adp/python314-expat-manifest.json
COPY --from=python314-expat /licenses/ /opt/awscli-adp/XML-LICENSES/
RUN PYTHONPATH=/opt/awscli-adp/lib/aws-cli/python3.14/lib-dynload python -c 'import pyexpat, _elementtree; import xml.etree.ElementTree as E; assert pyexpat.EXPAT_VERSION == "expat_2.8.5"; assert E.fromstring("<root>valid</root>").text == "valid"'
RUN --network=none /opt/awscli-adp/bin/aws --version | grep -F 'aws-cli/2.37.4 Python/3.14.7'

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
COPY modules/agent-factory/codex-reviewer/build-shared.mjs ./
COPY modules/agent-factory/codex-harness/package.json /codex-harness/package.json
COPY modules/agent-factory/codex-harness/src/projection.ts /codex-harness/src/projection.ts
COPY modules/agent-factory/codex-reviewer/src/ ./src/
RUN npm run build && npm prune --omit=dev

# -----------------------------------------------------------------------------
# Stage 3: Build the Task API investigator task agent (issue #5798)
#
# Its own stage with its own lockfile, mirroring codex-builder. The isolation is
# the point: this package must not share a dependency tree with the Claude or
# Codex packages, so legacy execution never initializes task-only dependencies and
# this build cannot move a version out from under them. `npm ci` (not
# `npm install`) so the lockfile is authoritative. It declares zero runtime
# dependencies — only the TypeScript toolchain, needed to compile and then pruned —
# so the final image copies dist/ and package.json with no node_modules at all.
# -----------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/node:24-bookworm-slim AS task-investigator-builder

WORKDIR /build
COPY modules/agent-factory/task-agents/investigator/package.json modules/agent-factory/task-agents/investigator/package-lock.json ./
RUN npm ci --include=dev
COPY modules/agent-factory/task-agents/investigator/tsconfig.json ./
COPY modules/agent-factory/task-agents/investigator/src/ ./src/
RUN npx tsc --project tsconfig.json && npm prune --omit=dev

# Shared native Codex SDK harness. Packaged only; persona admission is separate.
FROM public.ecr.aws/docker/library/node:24-bookworm-slim AS codex-harness-builder
WORKDIR /build
COPY modules/agent-factory/codex-harness/package.json modules/agent-factory/codex-harness/package-lock.json ./
RUN npm ci --include=dev --ignore-scripts
COPY modules/agent-factory/codex-harness/tsconfig.json modules/agent-factory/codex-harness/build.mjs ./
COPY modules/agent-factory/codex-harness/src/ ./src/
COPY --from=task-investigator-builder /build/dist/ /task-contracts/
COPY modules/tools/task-sdk/ /task-sdk/
RUN ADP_TASK_CONTRACTS_SOURCE=/task-contracts ADP_TASK_SDK_SOURCE=/task-sdk npm run build && npm prune --omit=dev --ignore-scripts

# Trusted SDK Task cyber driver has its own pinned dependency tree.
FROM public.ecr.aws/docker/library/node:24-bookworm-slim AS task-cyber-builder
WORKDIR /build
COPY modules/agent-factory/task-agents/cyber/package.json modules/agent-factory/task-agents/cyber/package-lock.json ./
RUN npm ci --ignore-scripts
COPY modules/agent-factory/task-agents/cyber/src/ ./src/
COPY modules/agent-factory/task-agents/cyber/build.mjs ./
COPY modules/tools/task-sdk/ /task-sdk/
RUN ADP_TASK_SDK_SOURCE=/task-sdk ADP_TASK_INCLUDE_CYBER=false npm run build && npm prune --omit=dev

# -----------------------------------------------------------------------------
# Stage 4: Stage personas and skills
# -----------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/debian:trixie-slim AS stager

WORKDIR /stage

# Copy the staging script
COPY modules/agent-factory/agent-worker-image/stage-personas.sh /stage/stage-personas.sh
RUN chmod +x /stage/stage-personas.sh

# Copy persona and skill source trees
COPY modules/agent-factory/rules/personas/ /source/agent-factory/personas/
COPY modules/agent-factory/skills/ /source/agent-factory/skills/

# Run the staging script to assemble /stage/personas and /stage/skills
RUN /stage/stage-personas.sh /source /stage

# -----------------------------------------------------------------------------
# Stage 5: Final runtime image
# -----------------------------------------------------------------------------
# Reviewed Python 3.13.16 + pip on Debian Trixie; keep its security manifest in sync.
# Uses deb.debian.org (Fastly CDN) for apt — far more reliable than
# Ubuntu's archive.ubuntu.com.
# Current stable Debian runtime; preserve all worker tooling and staged agents.
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS stdlib-security
COPY modules/gateway/security/stdlib/ /opt/stdlib-security/
RUN python /opt/stdlib-security/apply.py --verify-only --manifest-file manifest-3.13.16.json \
    && python /opt/stdlib-security/check.py

FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
COPY --from=stdlib-security /usr/local/lib/python3.13/stringprep.py /usr/local/lib/python3.13/stringprep.py
COPY --from=stdlib-security /usr/local/lib/python3.13/tarfile.py /usr/local/lib/python3.13/tarfile.py
COPY --from=stdlib-security /usr/local/lib/python3.13/urllib/request.py /usr/local/lib/python3.13/urllib/request.py
COPY modules/gateway/security/stdlib/ /opt/stdlib-security/
RUN python /opt/stdlib-security/apply.py --verify-only --manifest-file manifest-3.13.16.json \
    && python /opt/stdlib-security/check.py

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
ARG YQ_VERSION=4.53.6
RUN curl -fsSL "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_amd64" \
    -o /usr/local/bin/yq && chmod +x /usr/local/bin/yq

# Install fzf from official tarball (not apt) to avoid inheriting
# Debian bookworm's Go 1.19 toolchain CVEs (6 critical Go stdlib CVEs:
# CVE-2023-24531, CVE-2023-24540, CVE-2023-29402, CVE-2023-29404,
# CVE-2023-29405, CVE-2024-24790). The apt package is built with
# golang-1.19; the official release is compiled with Go 1.23+.
ARG FZF_VERSION=0.74.4
RUN curl -fsSL "https://github.com/junegunn/fzf/releases/download/v${FZF_VERSION}/fzf-${FZF_VERSION}-linux_amd64.tar.gz" \
      -o /tmp/fzf.tar.gz && \
    tar xzf /tmp/fzf.tar.gz -C /usr/local/bin fzf && \
    chmod +x /usr/local/bin/fzf && \
    rm /tmp/fzf.tar.gz && \
    fzf --version

# Pin the official Node24 release and replace its bundled npm with the fixed
# npm11 graph. This also removes the unpinned NodeSource setup script/repository.
ADD --checksum=sha256:fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6 https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz /tmp/node.tar.xz
RUN tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 && rm /tmp/node.tar.xz \
    && test "$(node --version)" = v24.21.0
ADD --checksum=sha256:d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483 https://registry.npmjs.org/npm/-/npm-11.20.0.tgz /tmp/npm.tgz
RUN rm -rf /usr/local/lib/node_modules/npm && mkdir -p /usr/local/lib/node_modules/npm \
    && tar -xzf /tmp/npm.tgz --strip-components=1 -C /usr/local/lib/node_modules/npm \
    && rm /tmp/npm.tgz && test "$(npm --version)" = 11.20.0

# Install pnpm and yarn via corepack. The Node 24 image ships a yarn stub
# at /usr/bin/yarn, so `npm install -g yarn` fails with EEXIST. Corepack
# is the modern way to install both and handles replacement idempotently.
ARG PNPM_VERSION=12.6.0
ARG YARN_VERSION=4.18.1
RUN corepack enable && \
    corepack prepare "pnpm@${PNPM_VERSION}" --activate && \
    corepack prepare "yarn@${YARN_VERSION}" --activate && \
    pnpm --version && yarn --version

# Install the OpenAI Codex CLI (issue #2704, EPIC #2702). Pinned so the image
# is reproducible and doesn't inherit a surprise "latest". Invoked headlessly
# as `codex exec` against Bedrock (openai.gpt-5.5); config baked in below.
ARG CODEX_VERSION=0.157.0
RUN npm install -g "@openai/codex@${CODEX_VERSION}" && \
    codex --version

# Install GitHub CLI from official tarball (not apt) to avoid inheriting
# Debian's Go 1.19 toolchain CVEs. Pinned to latest stable release.
ARG GH_VERSION=2.101.0
RUN curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
      -o /tmp/gh.tar.gz && \
    tar xzf /tmp/gh.tar.gz -C /tmp && \
    mv /tmp/gh_${GH_VERSION}_linux_amd64/bin/gh /usr/local/bin/gh && \
    chmod +x /usr/local/bin/gh && \
    rm -rf /tmp/gh.tar.gz /tmp/gh_${GH_VERSION}_linux_amd64 && \
    gh --version

# Reuse the reviewed source-built portable AWS CLI, including provenance.
COPY --from=awscli-security /opt/awscli-adp /opt/awscli-adp
RUN ln -s /opt/awscli-adp/bin/aws /usr/local/bin/aws \
    && aws --version

# Install kubectl
RUN curl -fsSL "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl

# Preserve the installed Terraform version; use the verified fixed Go rebuild.
COPY --from=terraform-security /out/terraform /usr/local/bin/terraform
RUN terraform version

# Install bun runtime — required by AIDLC v2 hooks and engine tools which are
# bun TypeScript. Pinned static binary from GitHub releases (same pattern as
# yq/fzf/gh/terraform above).
ARG BUN_VERSION=1.4.2
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
    'boto3>=1.36' \
    'rfc8785==0.1.4' \
    'PyJWT[crypto]>=2.10' \
    'requests>=2.32' \
    'cryptography>=44.0' \
    'pydantic>=2.13'

# Create non-root user
RUN groupadd -g 1001 agent && \
    useradd -m -u 1001 -g agent -s /bin/bash agent && \
    mkdir -p /app /work && \
    mkdir -p /home/agent/.cache/node && \
    cp -a /root/.cache/node/corepack /home/agent/.cache/node/corepack && \
    chown -R agent:agent /app /work /home/agent/.cache

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
COPY --chown=agent:agent modules/agent-factory/codex-reviewer/scripts/ /app/codex-reviewer/scripts/
COPY --chown=agent:agent modules/agent-factory/codex-reviewer/prompts/ /app/codex-reviewer/prompts/

# Task API investigator task agent (issue #5798). Same isolation rationale as
# codex-reviewer above, taken further: the package declares zero runtime
# dependencies, so there is no node_modules to copy and nothing it can drag into
# the image. Additive only — the Claude and Codex packages above are untouched,
# and nothing on the legacy execution path imports anything under /app/task-agents/.
#
# The host command that runs it (registered in entrypoint.py's allowlist by T4,
# issue #5797) is:
#     node /app/task-agents/investigator/dist/index.js --embedded
COPY --from=codex-harness-builder --chown=agent:agent /build/dist/ /app/codex-harness/dist/
COPY --from=codex-harness-builder --chown=agent:agent /build/package.json /app/codex-harness/package.json
COPY --from=codex-harness-builder --chown=agent:agent /build/node_modules/ /app/codex-harness/node_modules/
COPY --chown=agent:agent modules/agent-factory/rules/ /app/codex-harness/rules/
COPY --chown=agent:agent modules/agent-factory/codex-harness/scripts/report-probe* /app/codex-harness/scripts/
COPY --from=task-investigator-builder --chown=agent:agent /build/dist/ /app/task-agents/investigator/dist/
COPY --from=task-investigator-builder --chown=agent:agent /build/package.json /app/task-agents/investigator/package.json
COPY --from=task-cyber-builder --chown=agent:agent /build/dist/ /app/task-agents/cyber/dist/
COPY --from=task-cyber-builder --chown=agent:agent /build/package.json /app/task-agents/cyber/package.json
COPY --from=task-cyber-builder --chown=agent:agent /build/node_modules/ /app/task-agents/cyber/node_modules/

# Trusted local tool adapters; source and execution stay outside the SDK process.
COPY --chown=agent:agent modules/tools/adp_tools/ /app/adp_tools/
COPY --chown=agent:agent modules/tools/agentcore/agentcore_tools/ /app/agentcore_tools/

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
# Managed lifecycle hooks are trusted image content; inactive outside controlled runs.
COPY modules/agent-factory/agent-worker-image/codex-control-config.toml /etc/codex/config.toml
COPY modules/agent-factory/agent-worker-image/codex-control-hook.py /app/codex-control-hook.py

# Copy entrypoint and its lib/ support modules (vault_client, github_token, sts_assume)
COPY --chown=agent:agent modules/agent-factory/agent-worker-image/entrypoint.py /app/entrypoint.py
COPY --chown=agent:agent modules/agent-factory/agent-worker-image/lib/ /app/lib/

# Commit-scoped validation runner (implementation is included in lib/ above).
RUN printf '#!/bin/sh\nexec python3 -m lib.validation "$@"\n' > /usr/local/bin/adp-validate && \
    chmod +x /usr/local/bin/adp-validate

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

# Install after all apt operations so normal worker builds retain the reviewed
# curl repair instead of reverting to the vulnerable distribution packages.
COPY --from=curl-security /out/curl_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/curl.deb
COPY --from=curl-security /out/libcurl4t64_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/openssl.deb
COPY --from=curl-security /out/libcurl3t64-gnutls_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/gnutls.deb
RUN dpkg -i /tmp/adp-curl/*.deb && ldconfig && rm -rf /tmp/adp-curl
COPY --from=curl-security /licenses/ /opt/adp-curl-security/

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
