# LiteLLM Proxy — pre-baked image for Agent Context Intelligence Platform
# Eliminates slow pip install at pod startup; dependencies are cached in the image layer.
FROM public.ecr.aws/docker/library/python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get -y upgrade \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Auth error classification imports prisma even when no database is configured.
RUN pip install --no-cache-dir 'litellm[proxy]==1.102.1' 'prisma==0.15.0' && pip check

# 1.102.1 includes the embedding health fix; fail builds if it regresses.
COPY validate-health.py /tmp/validate-health.py
RUN python /tmp/validate-health.py && rm /tmp/validate-health.py

# Non-root user for security
RUN addgroup --system --gid 10001 appgroup && \
    adduser --system --uid 10001 --ingroup appgroup --home /home/appuser appuser && \
    install -d -m 0700 -o appuser -g appgroup /home/appuser
ENV HOME=/home/appuser \
    XDG_CACHE_HOME=/home/appuser/.cache \
    PYTHONDONTWRITEBYTECODE=1
# Canonical, hash-guarded CPython 3.13.15 source backports. No version override.
COPY security-stdlib/ /opt/adp-stdlib-security/
RUN apt-get update && apt-get install -y --no-install-recommends patch \
    && python /opt/adp-stdlib-security/apply.py \
    && python /opt/adp-stdlib-security/check.py \
    && apt-get purge -y patch && apt-get clean && rm -rf /var/lib/apt/lists/*

USER appuser

EXPOSE 4000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health', timeout=5)" || exit 1

# Default command — can be overridden in K8s manifest
CMD ["litellm", "--port", "4000", "--host", "0.0.0.0"]
