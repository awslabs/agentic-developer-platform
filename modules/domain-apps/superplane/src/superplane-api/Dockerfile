# ============================================================================
# Multi-stage Dockerfile for the Superplane API Server
# ============================================================================

# Stage 1: Build dependencies
FROM public.ecr.aws/docker/library/python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy dependency spec and install
COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install .

# Stage 2: Runtime image
FROM public.ecr.aws/docker/library/python:3.12-slim

# Install runtime system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --uid 65532 appuser

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code (owned by non-root user)
WORKDIR /app
COPY --chown=65532:65532 app/ ./app/
COPY --chown=65532:65532 alembic/ ./alembic/
COPY --chown=65532:65532 alembic.ini .

# Switch to non-root user
USER 65532:65532

# Health check port
EXPOSE 8000

# Run with uvicorn
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
