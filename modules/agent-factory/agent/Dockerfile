# Multi-stage build for ADP Agent (GitHub + Chat entrypoints)
#
# BUILD CONTEXT: modules/agent-factory/  (one level up from this Dockerfile)
# REASON: we need to COPY personas from `../rules/personas/` which is outside
# the `agent/` directory. Docker refuses paths that escape the build context,
# so the build context must be the parent of both `agent/` and `rules/`.
#
# Build with:
#   docker build -f modules/agent-factory/agent/Dockerfile \
#                -t <registry>/adp-agent-gateway:<tag> \
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

# Install dumb-init for proper signal handling
RUN apk add --no-cache dumb-init

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

# Set environment variables
ENV NODE_ENV=production \
    PORT=8765 \
    AGENT_ENTRYPOINT=github \
    PERSONAS_DIR=/app/personas

# Start the application (dispatched by AGENT_ENTRYPOINT)
CMD ["./startup.sh"]
