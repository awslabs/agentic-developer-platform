# Context MCP Server — verb surface for the Knowledge layer (:5100)
# Issue: #1417
FROM public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app

# Install system dependencies + Debian security patches (#6122)
RUN apt-get update && apt-get -y upgrade \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Download Amazon RDS/Neptune CA bundle for TLS verification (#2224)
# Neither the server nor personal-context code invokes curl. Use the shipped
# Python TLS client for setup and health checks instead of retaining libcurl.
RUN python -c "from pathlib import Path; from urllib.request import urlopen; Path('/etc/ssl/certs/rds-global-bundle.pem').write_bytes(urlopen('https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem', timeout=30).read())" \
    && test -s /etc/ssl/certs/rds-global-bundle.pem

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (staged into build dir by bs-agent-context-image.yml pre_build)
COPY door/ ./door/
COPY personal_context/ ./personal_context/

# Non-root user for security
RUN addgroup --system --gid 10001 appgroup && \
    adduser --system --uid 10001 --ingroup appgroup appuser && \
    chown -R appuser:appgroup /app

# Expose MCP port
EXPOSE 5100

# Drop to non-root before the runtime instructions (USER must precede
# HEALTHCHECK/CMD so the missing-user check is satisfied).
# Canonical, hash-guarded CPython 3.13.15 source backports. No version override.
COPY security-stdlib/ /opt/adp-stdlib-security/
RUN apt-get update && apt-get install -y --no-install-recommends patch \
    && python /opt/adp-stdlib-security/apply.py \
    && python /opt/adp-stdlib-security/check.py \
    && apt-get purge -y patch && apt-get clean && rm -rf /var/lib/apt/lists/*

USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://localhost:5100/health', timeout=5).close()" || exit 1

# Run the server
CMD ["uvicorn", "door.server:app", "--host", "0.0.0.0", "--port", "5100"]
