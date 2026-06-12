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
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Syft for SBOM generation (dual-rail #1358)
ARG SYFT_VERSION=1.11.1
RUN curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh \
    | sh -s -- -b /usr/local/bin "v${SYFT_VERSION}"

# Python deps
RUN pip install --no-cache-dir \
    codegraphcontext \
    tree-sitter \
    tree-sitter-python \
    tree-sitter-typescript \
    tree-sitter-go \
    requests \
    crawl4ai \
    boto3 \
    gremlinpython \
    opensearch-py \
    markitdown \
    PyJWT[crypto] \
    pydantic-settings \
    psycopg2-binary

# Install Playwright browsers for crawl4ai JS rendering
RUN crawl4ai-setup || echo "crawl4ai-setup not available or optional — continuing"

# Copy pipeline scripts (config.py must be first — all scripts import it)
COPY config.py s3_store.py wiki_store.py sbom_parser.py db.py /app/
COPY ingest-repo.py ingest-url.py ingest-doc.py refresh-repos.py \
     discover-infra.py sqs-worker.py publish-ingestion.py \
     correlate.py lint-wiki.py generate-indexes.py \
     generate-learning-artifacts.py generate-curricula.py /app/

# GitHub App token minting script (from scripts/github-app-token.py)
COPY github-app-token.py /app/github-app-token.py

# Git credential helper for authenticated cloning (GIT_ASKPASS)
COPY git-credential-helper.sh /app/git-credential-helper.sh
RUN chmod +x /app/git-credential-helper.sh

WORKDIR /app

# Default: run refresh (CronJob entrypoint)
CMD ["python", "/app/refresh-repos.py"]
