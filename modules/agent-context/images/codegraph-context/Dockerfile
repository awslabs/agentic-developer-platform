# CodeGraphContext — pre-baked image for Agent Context Intelligence Platform
# Eliminates slow apt-get + pip install at pod startup.
FROM public.ecr.aws/docker/library/python:3.13-slim

RUN apt-get update && \
    apt-get -y upgrade && \
    apt-get install -y --no-install-recommends git curl gcc g++ make && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
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
