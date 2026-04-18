# LiteLLM Proxy — pre-baked image for Agent Context Intelligence Platform
# Eliminates slow pip install at pod startup; dependencies are cached in the image layer.
FROM python:3.11-slim

RUN pip install --no-cache-dir 'litellm[proxy]'

# Non-root user for security
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser
USER appuser

EXPOSE 4000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health', timeout=5)" || exit 1

# Default command — can be overridden in K8s manifest
CMD ["litellm", "--port", "4000", "--host", "0.0.0.0"]
