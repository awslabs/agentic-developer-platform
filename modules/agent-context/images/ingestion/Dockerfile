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
FROM public.ecr.aws/docker/library/python:3.11-slim

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
    markitdown

# Install Playwright browsers for crawl4ai JS rendering
RUN crawl4ai-setup || echo "crawl4ai-setup not available or optional — continuing"

# Copy pipeline scripts
COPY ingest-repo.py ingest-url.py ingest-doc.py refresh-repos.py \
     discover-infra.py sqs-worker.py publish-ingestion.py \
     correlate.py lint-wiki.py generate-indexes.py \
     generate-learning-artifacts.py generate-curricula.py /app/

WORKDIR /app

# Default: run refresh (CronJob entrypoint)
CMD ["python", "/app/refresh-repos.py"]
