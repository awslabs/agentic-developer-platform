# DeepWiki image for Agent Context Intelligence Platform
# Based on upstream AsyncFuncAI/deepwiki-open, configured for LiteLLM proxy + Bedrock
#
# Build: docker build -t deepwiki:latest .
# The image uses OPENAI_BASE_URL to route all LLM calls through the LiteLLM proxy.

# Official upstream commit d92819a9c9f3b99416e3580ff235fc9d3adf8b89.
# Upstream publishes main/latest/sha tags; the configured v1.2.2 tag is unavailable.
FROM ghcr.io/asyncfuncai/deepwiki-open@sha256:730b72494f407397c896c586af0a31af9671d509af420e6792d834ff094a9c8f

# Override default config with our LiteLLM-proxy-aware configs
# These point the "openai" provider at our LiteLLM proxy which routes to Bedrock
COPY config/generator.json /app/api/config/generator.json
COPY config/embedder.json /app/api/config/embedder.json

# Create wiki cache directory and non-root user for security
RUN mkdir -p /root/.adalflow/wikicache && \
    addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser && \
    chmod 755 /root && \
    chown -R appuser:appgroup /root/.adalflow /app

# Environment defaults (overridden by K8s manifest)
ENV PORT=8001
ENV NODE_ENV=production
ENV LOG_LEVEL=INFO

EXPOSE 8001 3000

# Drop to non-root before the runtime instructions (USER must precede
# HEALTHCHECK/CMD so the missing-user check is satisfied).
USER appuser

# Health check
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
  CMD curl -sf http://localhost:8001/health || exit 1
