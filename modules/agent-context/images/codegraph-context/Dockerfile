# CodeGraphContext — pre-baked image for Agent Context Intelligence Platform
# Eliminates slow apt-get + pip install at pod startup.
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81

RUN apt-get update && \
    apt-get -y upgrade && \
    apt-get install -y --no-install-recommends git curl gcc g++ make && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Staged from images/shared by the canonical build and local validation helpers.
COPY security-build/patch-codegraph-wheel.py /tmp/patch-codegraph-wheel.py
RUN pip download --no-deps --only-binary=:all: --dest /tmp/upstream codegraphcontext==0.6.13 && \
    python /tmp/patch-codegraph-wheel.py \
        /tmp/upstream/codegraphcontext-0.6.13-py3-none-any.whl \
        /tmp/patched/codegraphcontext-0.6.13-py3-none-any.whl && \
    pip install --no-cache-dir /tmp/patched/codegraphcontext-0.6.13-py3-none-any.whl && \
    pip check && rm -rf /tmp/upstream /tmp/patched /tmp/patch-codegraph-wheel.py

# Non-root user for security
RUN addgroup --system --gid 10001 appgroup && \
    adduser --system --uid 10001 --ingroup appgroup appuser && \
    install -d -m 0700 -o appuser -g appgroup /data
# CodeGraphContext 0.6.13 uses Path.home(), not CGC_HOME. Mount /data
# writable when running with a read-only root filesystem.
ENV HOME=/data
# Verify the reviewed Python 3.13.16 source bytes and security behavior.
COPY security-stdlib/ /opt/adp-stdlib-security/
RUN python /opt/adp-stdlib-security/apply.py --verify-only --manifest-file manifest-3.13.16.json \
    && python /opt/adp-stdlib-security/check.py

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import codegraphcontext; print('ok')" || exit 1

# Keep running for kubectl exec access (cgc is used interactively)
CMD ["sleep", "infinity"]
