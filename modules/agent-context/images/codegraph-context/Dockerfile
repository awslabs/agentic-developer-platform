# CodeGraphContext — pre-baked image for Agent Context Intelligence Platform
# Eliminates slow apt-get + pip install at pod startup.
FROM python:3.11.15-slim@sha256:9a7765b36773a37061455b332f18e265e7f58f6fea9c419a550d2a8b0e9db834

RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl gcc g++ make && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir codegraphcontext

# Non-root user for security
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import codegraphcontext; print('ok')" || exit 1

# Keep running for kubectl exec access (cgc is used interactively)
CMD ["sleep", "infinity"]
