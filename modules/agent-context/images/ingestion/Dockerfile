# Unified Ingestion Pipeline — enrichment image
# Runs: ingest-repo.py, ingest-url.py, ingest-doc.py, refresh-repos.py,
#        discover-infra.py, sqs-worker.py, publish-ingestion.py,
#        correlate.py, lint-wiki.py, generate-indexes.py,
#        generate-learning-artifacts.py, generate-curricula.py
#
# Build:
#   docker build -t agent-context/ingestion:latest .
#   docker tag agent-context/ingestion:latest ${ECR_REGISTRY}/agent-context/ingestion:latest
#   docker push ${ECR_REGISTRY}/agent-context/ingestion:latest
# zoekt-git-index binary, copied from the SAME official image the webserver runs
# (ghcr.io/sourcegraph/zoekt:${ZOEKT_IMAGE_TAG}) so the indexer and webserver are
# guaranteed version-matched (#2361). This replaces a `go install` that pinned a
# non-existent pseudo-version. ZOEKT_IMAGE_TAG defaults to "latest" (matches
# manifests/zoekt.yaml + config.env).
ARG ZOEKT_IMAGE_TAG=latest
FROM ghcr.io/sourcegraph/zoekt:${ZOEKT_IMAGE_TAG} AS zoekt
# Resolve the indexer binary to a fixed path regardless of where the upstream
# image places it (avoids hardcoding /usr/local/bin which may drift).
SHELL ["/bin/sh", "-c"]
RUN cp "$(command -v zoekt-git-index || find / -name zoekt-git-index -type f 2>/dev/null | head -1)" /tmp/zoekt-git-index

FROM public.ecr.aws/docker/library/python:3.13-slim

# System deps: git for cloning, build tools for tree-sitter native extensions
RUN apt-get update && \
    apt-get -y upgrade && \
    apt-get install -y --no-install-recommends \
        git \
        curl \
        gcc \
        g++ \
        make \
        unzip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# SCIP indexer toolchain (Phase 1 + Phase 2 languages)
# ---------------------------------------------------------------------------

# Node.js 20.x (required for scip-python, scip-typescript)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# SCIP indexers via npm (global install)
# Note: scip-java deferred — not in current corpus (D7 in neptune-deep-graph-design.md)
RUN npm install -g \
    @sourcegraph/scip-python@0.6.6 \
    @sourcegraph/scip-typescript@0.4.0 \
    && npm cache clean --force

# Go 1.25+ (scip-go v0.2.7 requires go >= 1.25; kept for dep resolution of Go repos)
ARG GO_VERSION=1.25.4
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
    | tar -xz -C /usr/local
ENV PATH="/usr/local/go/bin:/root/go/bin:${PATH}"
ENV GOPATH="/root/go"

# scip-go (module at github.com/scip-code/scip-go, binary in cmd/scip-go)
RUN go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7

# zoekt-git-index — builds .zoekt shards from a git checkout (#2361).
# Copied from the official zoekt image (the `zoekt` build stage above) so it's
# the exact same version as the webserver — no fragile go-install pseudo-version.
COPY --from=zoekt /tmp/zoekt-git-index /usr/local/bin/zoekt-git-index

# Ruby + Sorbet (for scip-ruby — best-effort on untyped repos)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ruby ruby-dev bundler && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    gem install sorbet-static sorbet --no-document || true

# .NET SDK (for scip-dotnet)
RUN curl -fsSL https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 8.0 --install-dir /usr/share/dotnet && \
    ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet || true
ENV DOTNET_ROOT="/usr/share/dotnet"

# JVM deferred — scip-java not in current corpus (D7 in neptune-deep-graph-design.md)
# When Java support is added, install: default-jdk-headless maven

# Install Syft for SBOM generation (dual-rail #1358)
ARG SYFT_VERSION=1.11.1
RUN curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh \
    | sh -s -- -b /usr/local/bin "v${SYFT_VERSION}"

# Install OSV-Scanner + Trivy for vulnerability scanning (#2536)
ARG OSV_SCANNER_VERSION=2.3.8
ARG TRIVY_VERSION=0.71.0
RUN curl -sSfL "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/osv-scanner_linux_amd64" \
        -o /usr/local/bin/osv-scanner && \
    chmod +x /usr/local/bin/osv-scanner && \
    curl -sSfL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
        | tar xz -C /usr/local/bin trivy

# Python deps
RUN pip install --no-cache-dir \
    codegraphcontext \
    python-hcl2 \
    tree-sitter \
    tree-sitter-python \
    tree-sitter-typescript \
    tree-sitter-go \
    requests \
    defusedxml>=0.7.1 \
    crawl4ai \
    boto3 \
    gremlinpython \
    opensearch-py \
    markitdown \
    PyJWT[crypto] \
    pydantic-settings \
    psycopg2-binary \
    alembic \
    sqlalchemy \
    asyncpg \
    protobuf \
    python-json-logger==2.0.7 \
    opentelemetry-api>=1.20.0 \
    opentelemetry-sdk>=1.20.0 \
    opentelemetry-exporter-otlp-proto-grpc>=1.20.0 \
    opentelemetry-sdk-extension-aws>=2.0.0 \
    opentelemetry-propagator-aws-xray>=1.0.0

# Download Amazon RDS/Neptune CA bundle for TLS verification (#2224)
RUN curl -fsSL -o /etc/ssl/certs/rds-global-bundle.pem \
        https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    && test -s /etc/ssl/certs/rds-global-bundle.pem

# Install Playwright browsers for crawl4ai JS rendering
RUN crawl4ai-setup || echo "crawl4ai-setup not available or optional — continuing"

# Copy pipeline scripts (config.py must be first — all scripts import it)
# scope.py + status_callback.py are imported by sqs-worker.py / ingest-repo.py /
# publish-ingestion.py — without them the ScaledJob worker dies at import (#2213).
# registry_reader.py is imported by publish-ingestion.py's --from-registry path
# (publish_from_registry, #2172); without it the daily refresh crashes at Step 1
# with ModuleNotFoundError (#2800).
COPY config.py telemetry.py tracing.py metrics.py s3_store.py wiki_store.py code_embedder.py sbom_parser.py db.py stage_tracker.py github_auth.py scope.py status_callback.py registry_reader.py lang_go.py /app/
COPY scip_ingester.py scip_indexer.py scip_neptune_csv.py scip_neptune_loader.py /app/
COPY iac_terraform_parser.py iac_neptune_csv.py /app/
COPY scip_proto/ /app/scip_proto/
COPY ingest-repo.py ingest-url.py ingest-doc.py refresh-repos.py \
     discover-infra.py sqs-worker.py publish-ingestion.py \
     correlate.py lint-wiki.py generate-indexes.py \
     generate-learning-artifacts.py generate-curricula.py \
     scan-vulns.py /app/

# Build-time import smoke check: fail the image build (not at 06:00 UTC in prod)
# if a top-level /app module is missing from the COPY list. registry_reader was
# shipped in source but never copied, silently breaking the daily refresh (#2800).
# Only pure-import modules (no env/DB needed at import time) are listed here.
RUN cd /app && python -c "import registry_reader, scope, status_callback, lang_go"

# Vulnerability scanner pipeline package (used by scan-vulns.py, #2536)
COPY pipeline/ /app/pipeline/

# GitHub App token minting script (from scripts/github-app-token.py)
COPY github-app-token.py /app/github-app-token.py

# Git credential helper for authenticated cloning (GIT_ASKPASS)
COPY git-credential-helper.sh /app/git-credential-helper.sh
RUN chmod +x /app/git-credential-helper.sh

# Package alembic config + migrations for in-cluster migration Jobs (#1437)
COPY alembic/alembic.ini /app/alembic/alembic.ini
COPY alembic/env.py /app/alembic/env.py
COPY alembic/script.py.mako /app/alembic/script.py.mako
COPY alembic/versions/ /app/alembic/versions/

WORKDIR /app

# Default: run refresh (CronJob entrypoint)
CMD ["python", "/app/refresh-repos.py"]
