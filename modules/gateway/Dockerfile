# Base image pulled from public.ecr.aws (AWS-hosted mirror of Docker Hub) to
# dodge the unauthenticated Docker Hub pull rate limit that periodically
# fails CodeBuild runs with HTTP 429.
# Build stage
FROM public.ecr.aws/docker/library/python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml .
# Install core dependencies
RUN pip install --no-cache-dir .
# Issue #144: Install tracing dependencies (adds ~20MB but only active when OTEL_ENABLED=true)
RUN pip install --no-cache-dir ".[tracing]" || echo "Tracing dependencies not available, skipping"

# Runtime stage
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini .

EXPOSE 8080
CMD ["uvicorn", "src.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "4"]
