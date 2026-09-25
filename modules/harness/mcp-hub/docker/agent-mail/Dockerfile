# MCP Agent Mail - FastMCP HTTP Server
# Source: https://github.com/Dicklesworthstone/mcp_agent_mail
#
# This builds the actual MCP Agent Mail Python application,
# NOT the Node.js GitHub Agent that was previously containerized.
#
# Upstream commit pinned for reproducible builds; bump explicitly when updating.
# Vulnerability constraints applied via constraints.txt after install.

FROM public.ecr.aws/docker/library/python:3.14-alpine3.24@sha256:9e9fde4d32eedce0b661d9ab91e826b62dddf28e928c230ec55f1866cac66b01 AS runtime-base

LABEL maintainer="agent-operations"
LABEL description="MCP Agent Mail - Agent registration, messaging, FTS5 search, Web UI"
LABEL source="https://github.com/Dicklesworthstone/mcp_agent_mail"

# Keep Git remote/local operations and SQLite FTS5 available at runtime.
RUN apk upgrade --no-cache && apk add --no-cache git sqlite libstdc++

# Native extensions build here; the compiler and headers never enter runtime.
FROM runtime-base AS build
RUN apk add --no-cache build-base cargo linux-headers libffi-dev openssl-dev

# Upgrade pip to resolve CVEs in the bundled version (PYSEC-2026-1795/1796/
# 2875/2876/196/3721), then install uv for fast package management.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir uv

WORKDIR /app

# Pin upstream to a known commit for build reproducibility and audit traceability.
# Commit 4b11f26 (2026-09-22): latest at time of remediation baseline.
ARG UPSTREAM_COMMIT=4b11f26277f611e60bcfdb3858c305d1af2fcc53
RUN git clone --depth 1 https://github.com/Dicklesworthstone/mcp_agent_mail.git . && \
    git fetch --depth 1 origin ${UPSTREAM_COMMIT} && \
    git checkout ${UPSTREAM_COMMIT} && \
    uv pip install --system -e .

# Apply security constraints to bump vulnerable transitive deps.
# See constraints.txt for per-advisory rationale.
COPY constraints.txt /tmp/constraints.txt
RUN uv pip install --system -r /tmp/constraints.txt && rm /tmp/constraints.txt

# Remove packages that are not needed at runtime:
#   ruff  - linter listed as upstream runtime dep by mistake; never imported
#   diskcache - orphan transitive dep (pickle deserialization CVE, PYSEC-2026-2447)
# Uninstalling after the editable install is safe; serve-http does not import them.
RUN pip uninstall -y ruff diskcache 2>/dev/null || true

# Build caches retain superseded wheels and unpacked vulnerable dependencies even
# after the runtime constraints upgrade them. Keep only the installed runtime.
RUN rm -rf /root/.cache/uv /root/.cache/pip

COPY patch_naive_datetimes.py /tmp/patch_naive_datetimes.py
RUN python /tmp/patch_naive_datetimes.py && rm /tmp/patch_naive_datetimes.py

# Copy installed application/dependencies, not compilers or build caches.
FROM runtime-base AS runtime
COPY --from=build /usr/local /usr/local
COPY --from=build /app /app
WORKDIR /app

# Create data directory with correct ownership
RUN mkdir -p /data && chown 10001:10001 /data

# Run as non-root user
USER 10001

# MCP Agent Mail default port
EXPOSE 8765

# Health check against the actual MCP Agent Mail liveness endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/health/liveness')" || exit 1

# Start MCP Agent Mail HTTP server
# NOTE: `python -m mcp_agent_mail` does NOT work because __main__.py hardcodes
# args=["--help"]. We invoke the Typer CLI directly instead.
# The serve-http subcommand starts the FastMCP Streamable HTTP transport on
# $HTTP_HOST:$HTTP_PORT (default 0.0.0.0:8765).
CMD ["python", "-c", "import sys; sys.argv = ['mcp-agent-mail', 'serve-http']; from mcp_agent_mail.cli import app; app()"]
