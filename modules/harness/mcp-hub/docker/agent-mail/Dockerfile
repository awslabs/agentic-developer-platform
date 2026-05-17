# MCP Agent Mail - FastMCP HTTP Server
# Source: https://github.com/Dicklesworthstone/mcp_agent_mail
#
# This builds the actual MCP Agent Mail Python application,
# NOT the Node.js GitHub Agent that was previously containerized.

FROM public.ecr.aws/docker/library/python:3.14-slim

LABEL maintainer="agent-operations"
LABEL description="MCP Agent Mail - Agent registration, messaging, FTS5 search, Web UI"
LABEL source="https://github.com/Dicklesworthstone/mcp_agent_mail"

# Install system dependencies for SQLite FTS5 and git
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python package management
RUN pip install --no-cache-dir uv

WORKDIR /app

# Clone and install MCP Agent Mail from source
RUN git clone --depth 1 https://github.com/Dicklesworthstone/mcp_agent_mail.git . && \
    uv pip install --system -e .

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
