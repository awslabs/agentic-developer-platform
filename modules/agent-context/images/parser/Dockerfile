# Isolated SCIP parser image — credential-free, minimal toolchain
#
# This image contains ONLY the SCIP indexer toolchain and the parser
# entrypoint.  It has NO:
# - AWS SDK (boto3, botocore)
# - Database drivers (psycopg2, gremlinpython)
# - HTTP clients (httpx, requests)
# - GitHub auth modules
# - Secret mounting or credential injection
#
# It runs as non-root (UID 1001), with read-only root filesystem,
# dropped capabilities, and no network access.

# Stage 1: Copy zoekt-git-index (not needed for SCIP, but keeps parity)
# We deliberately do NOT copy zoekt since the parser doesn't need it.

# Stage 2: SCIP toolchain
FROM python:3.13-slim AS parser

# Install system dependencies for SCIP indexers
RUN apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x (for scip-python and scip-typescript)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install SCIP indexers via npm
RUN npm install -g \
        @sourcegraph/scip-python@0.6.6 \
        @sourcegraph/scip-typescript@0.4.0 \
    && npm cache clean --force

# Install Go 1.26.8 and scip-go (#6122)
RUN curl -fsSL https://go.dev/dl/go1.26.8.linux-amd64.tar.gz \
    | tar -C /usr/local -xz
ENV PATH="/usr/local/go/bin:${PATH}"
RUN GOBIN=/usr/local/bin go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7 \
    && rm -rf /root/go

# Install Python dependencies — ONLY what the parser needs (#6122: pinned)
RUN pip install --no-cache-dir \
        protobuf==5.29.6 \
    && pip cache purge

# Verify NO credential-bearing packages are installed
RUN python3 -c "\
import importlib.util, sys; \
forbidden = ['boto3','botocore','httpx','requests','psycopg2','gremlinpython','opensearchpy']; \
found = [m for m in forbidden if importlib.util.find_spec(m)]; \
assert not found, f'FORBIDDEN packages found: {found}'; \
print('Import discipline verified: no forbidden packages')"

# Create non-root user (same UID/GID as ingestion image for mount compat)
RUN groupadd -r -g 1001 appgroup \
    && useradd -r -u 1001 -g appgroup -m -d /home/appuser appuser

# Set up directories
ENV HOME=/home/appuser \
    GOMODCACHE=/home/appuser/.cache/gomod \
    GOCACHE=/home/appuser/.cache/go-build \
    XDG_CACHE_HOME=/home/appuser/.cache \
    NPM_CONFIG_CACHE=/home/appuser/.cache/npm

RUN mkdir -p /app /input /output /source \
        "${GOMODCACHE}" "${GOCACHE}" "${XDG_CACHE_HOME}" "${NPM_CONFIG_CACHE}" \
    && chown -R appuser:appgroup /home/appuser /output

# Copy ONLY the parser entrypoint and required modules
COPY --chown=root:root isolated_parser.py /app/isolated_parser.py
COPY --chown=root:root parser_manifest.py /app/parser_manifest.py
COPY --chown=root:root scip_indexer.py /app/scip_indexer.py
COPY --chown=root:root scip_proto/ /app/scip_proto/
COPY --chown=root:root lang_go.py /app/lang_go.py

# Verify the parser can import without forbidden modules
RUN PYTHONPATH=/app python3 -c "\
import sys; sys.path.insert(0, '/app'); \
from parser_manifest import ParseInputManifest, ParseOutputManifest; \
print('Parser manifest import OK'); \
# Do NOT import scip_indexer here — it has optional deps that may fail \
# in the build context but work at runtime with proper PATH \
"

# Switch to non-root user
USER appuser
WORKDIR /app

# The parser reads the input manifest from /input/input_manifest.json
# and writes output to /output/output_manifest.json
ENV PYTHONPATH=/app
ENTRYPOINT ["python3", "/app/isolated_parser.py"]
