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

# The two sibling source packages app/ imports at module scope: the domain
# authorization policy (issue #5055, U14 — app/auth.py) and the domain contracts
# (issue #5053, U7b — app/main.py, and the routers/services under it). Both are
# maintained one directory above this component and staged into this context by
# scripts/stage-domain-auth.sh, because releases/build-image.sh pins the build
# context to this directory and Docker cannot COPY from above it.
#
# Neither is optional and neither is in pyproject.toml's dependency list: without
# them `app.main` raises ModuleNotFoundError at startup. The build fails here with
# that named cause rather than producing an image that crash-loops on import —
# which is the whole point of checking, since the test lane pip-installs both from
# their paths and cannot see their absence.
COPY vendor/superplane-auth ./vendor/superplane-auth
COPY vendor/superplane-contracts ./vendor/superplane-contracts
# `set -e` plus one `if` per package rather than a chain of `||`/`&&`: those two
# operators have equal precedence and associate left-to-right, so a mixed chain
# guarding two tests reads as though each failure is caught independently when it
# does not. Written out so the build's failure behaviour is not a precedence puzzle.
RUN set -eu; \
    for pkg in "superplane-auth/superplane_auth/policy.py" \
               "superplane-contracts/superplane_contracts/emission.py"; do \
      if [ ! -f "vendor/$pkg" ]; then \
        echo "error: vendor/$pkg is missing; run scripts/stage-domain-auth.sh before docker build" >&2; \
        exit 1; \
      fi; \
    done; \
    pip install --no-cache-dir --prefix=/install ./vendor/superplane-auth; \
    pip install --no-cache-dir --prefix=/install ./vendor/superplane-contracts

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
