# Multi-stage build for ADP Agent (GitHub + Chat entrypoints)
#
# BUILD CONTEXT: modules/agent-factory/  (one level up from this Dockerfile)
# REASON: we need to COPY personas from `../rules/personas/` which is outside
# the `agent/` directory. Docker refuses paths that escape the build context,
# so the build context must be the parent of both `agent/` and `rules/`.
#
# Build with:
#   docker build -f modules/agent-factory/agent/Dockerfile \
#                -t <registry>/adp-chat-agent:<tag> \
#                modules/agent-factory
#
FROM public.ecr.aws/docker/library/node:22-alpine AS builder

# Create non-root user
RUN addgroup -g 10001 agent && \
    adduser -u 10001 -G agent -s /bin/sh -D agent

# Set working directory
WORKDIR /app

# Copy package files from the agent subdir
COPY agent/package*.json ./

# Builder needs devDependencies (typescript) to compile — install them all here.
# The production stage copies only the runtime node_modules produced below.
RUN npm ci

# Copy the rest of the agent source
COPY agent/ ./

# Build the TypeScript → dist/
RUN npm run build

# Prune dev dependencies before the production copy so we don't ship devDeps.
RUN npm prune --omit=dev

# Production stage
FROM public.ecr.aws/docker/library/node:22-alpine AS production

# Create non-root user
RUN addgroup -g 10001 agent && \
    adduser -u 10001 -G agent -s /bin/sh -D agent

# Upgrade all system packages to pick up security patches in the base layer.
RUN apk upgrade --no-cache

# Install dumb-init for signal handling, and bash for the Claude Agent SDK's
# Bash tool. The SDK invokes /bin/bash directly — alpine ships only BusyBox
# ash, which the tool rejects with "shell environment isn't configured here"
# (the error message mentions SHELL but the real requirement is a bash binary).
RUN apk add --no-cache dumb-init bash

# Keep the bundled package manager usable while replacing its vulnerable
# transitive dependencies. npm11 supports this Node22 runtime (>=22.9).
# Release artifact hash is independently retained in the runner security receipt.
RUN wget -q https://registry.npmjs.org/npm/-/npm-11.20.0.tgz -O /tmp/npm.tgz \
    && echo "d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483  /tmp/npm.tgz" | sha256sum -c - \
    && rm -rf /usr/local/lib/node_modules/npm \
    && mkdir -p /usr/local/lib/node_modules/npm \
    && tar -xzf /tmp/npm.tgz --strip-components=1 -C /usr/local/lib/node_modules/npm \
    && rm /tmp/npm.tgz \
    && npm --version

# Create directories with proper ownership
RUN mkdir -p /app /data /tmp/workspace && \
    chown -R agent:agent /app /data /tmp/workspace

# Set working directory
WORKDIR /app

# Switch to non-root user
USER agent:agent

# Copy built application from builder stage
COPY --from=builder --chown=agent:agent /app/dist ./dist
COPY --from=builder --chown=agent:agent /app/node_modules ./node_modules
COPY --from=builder --chown=agent:agent /app/package*.json ./

# Copy personas from the sibling rules/ directory (inside build context).
# Baked into the image so persona loading has zero runtime dependencies.
COPY --chown=agent:agent rules/personas/ ./personas/
# Canonical issue contract is readable even in customer repos without ADP docs.
COPY --chown=agent:agent rules/agents/issue-authoring.md ./rules/agents/issue-authoring.md
COPY --chown=agent:agent rules/templates/developer-issue.md ./rules/templates/developer-issue.md

# Copy startup script
COPY --chown=agent:agent agent/startup.sh ./startup.sh
RUN chmod +x startup.sh

# Expose port
EXPOSE 8765

# Health check (only active for github entrypoint; chat agent exits after processing)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8765/health || exit 1

# Use dumb-init for proper signal handling
ENTRYPOINT ["dumb-init", "--"]

# Set environment variables.
# SHELL=/bin/bash is required by the Claude Agent SDK's Bash tool — the tool
# invokes bash directly. bash is installed above via apk; without it the tool
# errors out with "shell environment isn't configured here" (misleading — the
# message mentions SHELL but the real requirement is the bash binary).
# CLAUDE_CONFIG_DIR points the Agent SDK's session store at ephemeral,
# agent-writable container storage (issue #2079). persistSession:true in the
# worker writes session transcripts here so a stalled run can be TRULY resumed
# (options.resume) on retry instead of restarting from scratch. /tmp/workspace
# is created and chowned to the agent user in the runtime stage above; it lives
# only for the pod's lifetime, so this leaves no durable footprint.
ENV NODE_ENV=production \
    PORT=8765 \
    AGENT_ENTRYPOINT=github \
    PERSONAS_DIR=/app/personas \
    CLAUDE_CONFIG_DIR=/tmp/workspace/.claude \
    SHELL=/bin/bash

# Start the application (dispatched by AGENT_ENTRYPOINT)
CMD ["./startup.sh"]
