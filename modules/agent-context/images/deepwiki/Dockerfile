# DeepWiki image for Agent Context Intelligence Platform
# Based on upstream AsyncFuncAI/deepwiki-open, configured for LiteLLM proxy + Bedrock
#
# Build: docker build -t deepwiki:latest .
# The image uses OPENAI_BASE_URL to route all LLM calls through the LiteLLM proxy.

FROM ghcr.io/asyncfuncai/deepwiki-open:v1.2.2

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

# Health check
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
  CMD curl -sf http://localhost:8001/health || exit 1

EXPOSE 8001 3000

USER appuser
