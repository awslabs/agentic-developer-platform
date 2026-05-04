# =============================================================================
# adp-agent-runtime — per-PR overlay on top of adp-agent-runtime-base
# =============================================================================
# FROMs the pre-baked base image (Dockerfile.base), copies compiled agent
# code + entrypoint + personas + skills. No apt, no pip — all of that lives
# in the base image and is rebuilt on a weekly cron or when Dockerfile.base
# itself changes (via codebuild/bs-agent-runtime-base.yml).
#
# Build time: ~30s (pure COPY operations).
# Rebuild cadence: every PR that touches agent-worker-image/** or agent/src/**.
# Target size: < 2 GB compressed (same ballpark as today's fat image).
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
# Stage 3: Final runtime — thin overlay on pre-baked toolchain base
# -----------------------------------------------------------------------------
# Pull from our own ECR. The base image tag is passed in at build time so
# CI can pin to a known-good revision; defaults to `latest` for local dev.
ARG BASE_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime-base:latest
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="adp-agent"
LABEL org.opencontainers.image.description="ADP hosted agent worker image"
LABEL org.opencontainers.image.source="https://github.com/aws-e/adp"

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

# Healthcheck: verify node and python are available
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD node --version && python3 --version

ENTRYPOINT ["python3", "/app/entrypoint.py"]
