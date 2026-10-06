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
# It runs as non-root (UID 10001), with read-only root filesystem,
# dropped capabilities, and no network access.

# Stage 1: Copy zoekt-git-index (not needed for SCIP, but keeps parity)
# We deliberately do NOT copy zoekt since the parser doesn't need it.

# Stage 2: SCIP toolchain
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-ingestion@sha256:f5928de2bc4f007f0e7b7becf502d2f2b750542d08fb3ad5577411b9ad6cf8e2 AS high-security-tools
FROM public.ecr.aws/docker/library/node:22.23.3-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS node-release
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS parser

# Install system dependencies for SCIP indexers
RUN apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Fixed Node runtime and npm dependency tree (CVE-2026-48930 / GHSA-23hp-3jrh-7fpw).
# The official Node image verifies signed release checksums. Do not install Node20.
COPY --from=node-release /usr/local/bin/node /usr/local/bin/node
COPY --from=node-release /usr/local/LICENSE /opt/node/LICENSE
ADD --checksum=sha256:d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483 https://registry.npmjs.org/npm/-/npm-11.20.0.tgz /tmp/npm.tgz
RUN test ! -e /usr/bin/node && ln -s /usr/local/bin/node /usr/bin/node \
    && mkdir -p /usr/lib/node_modules/npm \
    && tar -xzf /tmp/npm.tgz --strip-components=1 -C /usr/lib/node_modules/npm \
    && rm /tmp/npm.tgz \
    && ln -s /usr/lib/node_modules/npm/bin/npm-cli.js /usr/bin/npm \
    && ln -s /usr/lib/node_modules/npm/bin/npx-cli.js /usr/bin/npx \
    && test "$(node --version)" = v22.23.3 \
    && test "$(npm --version)" = 11.20.0

# Install SCIP indexers via npm
RUN npm install -g --prefix /usr \
        @sourcegraph/scip-python@0.6.6 \
        @sourcegraph/scip-typescript@0.4.0 \
    && npm cache clean --force

# SCIP Python embeds a native TypeScript compiler; retain ingestion's tested
# compiler build with fixed x/text rather than npm's affected bundled binary.
COPY --from=high-security-tools /bin/tsc /usr/lib/node_modules/@sourcegraph/scip-python/node_modules/@typescript/typescript-linux-x64/lib/tsc

# Install Go 1.26.8 and scip-go (#6122)
RUN curl -fsSL https://go.dev/dl/go1.26.8.linux-amd64.tar.gz \
    | tar -C /usr/local -xz
ENV PATH="/usr/local/go/bin:${PATH}"
# Use the same reviewed scip-go build as ingestion.
COPY --from=high-security-tools /bin/scip-go /usr/local/bin/scip-go

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
RUN groupadd -r -g 10001 appgroup \
    && useradd -r -u 10001 -g appgroup -m -d /home/appuser appuser

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
# Verify the reviewed Python 3.13.16 source bytes and security behavior.
COPY security-stdlib/ /opt/adp-stdlib-security/
RUN python /opt/adp-stdlib-security/apply.py --verify-only --manifest-file manifest-3.13.16.json \
    && python /opt/adp-stdlib-security/check.py

USER appuser
WORKDIR /app

# The parser reads the input manifest from /input/input_manifest.json
# and writes output to /output/output_manifest.json
ENV PYTHONPATH=/app
ENTRYPOINT ["python3", "/app/isolated_parser.py"]
