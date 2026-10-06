# LiteLLM Proxy — pre-baked image for Agent Context Intelligence Platform
# Eliminates slow pip install at pod startup; dependencies are cached in the image layer.
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81

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
# Verify the reviewed Python 3.13.16 source bytes and security behavior.
COPY security-stdlib/ /opt/adp-stdlib-security/
RUN python /opt/adp-stdlib-security/apply.py --verify-only --manifest-file manifest-3.13.16.json \
    && python /opt/adp-stdlib-security/check.py

USER appuser

EXPOSE 4000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health', timeout=5)" || exit 1

# Default command — can be overridden in K8s manifest
CMD ["litellm", "--port", "4000", "--host", "0.0.0.0"]
