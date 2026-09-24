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

# Stage the maintained auth, contracts, shared executor, account and workspace
# lifecycle packages from the same reviewed checkout. Docker's component context
# cannot read their source directories directly. Missing source refuses the build
# instead of producing an API whose preview or worker cannot import its runtime.
COPY vendor/superplane-auth ./vendor/superplane-auth
COPY vendor/superplane-contracts ./vendor/superplane-contracts
COPY vendor/harness-jobs ./vendor/harness-jobs
COPY vendor/account-factory ./vendor/account-factory
COPY vendor/account-provisioning ./vendor/account-provisioning
COPY vendor/superplane-bootstrap ./vendor/superplane-bootstrap
COPY vendor/workspace-provisioning ./vendor/workspace-provisioning
COPY vendor/superplane-executor ./vendor/superplane-executor
# `set -e` plus one `if` per package rather than a chain of `||`/`&&`: those two
# operators have equal precedence and associate left-to-right, so a mixed chain
# guarding two tests reads as though each failure is caught independently when it
# does not. Written out so the build's failure behaviour is not a precedence puzzle.
RUN set -eu; \
    for pkg in "superplane-auth/superplane_auth/policy.py" \
               "superplane-contracts/superplane_contracts/emission.py" \
               "harness-jobs/harness_jobs/facade.py" \
               "account-factory/account_factory/modes.py" \
               "account-provisioning/account_provisioning/creation_runner.py" \
               "superplane-bootstrap/superplane_bootstrap/workspace.py" \
               "workspace-provisioning/workspace_provisioning/preview.py" \
               "superplane-executor/superplane_executor/inventory.py"; do \
      if [ ! -f "vendor/$pkg" ]; then \
        echo "error: vendor/$pkg is missing; run scripts/stage-domain-auth.sh before docker build" >&2; \
        exit 1; \
      fi; \
    done; \
    pip install --no-cache-dir --prefix=/install \
      ./vendor/superplane-auth ./vendor/superplane-contracts ./vendor/harness-jobs \
      ./vendor/account-factory ./vendor/account-provisioning \
      ./vendor/superplane-bootstrap ./vendor/workspace-provisioning \
      ./vendor/superplane-executor

# Exercise installed wheels and their maintained data during the remote image
# build, without importing the source checkout or contacting a provider.
RUN PYTHONPATH=/install/lib/python3.12/site-packages python -c \
    'from workspace_provisioning.preview import preview_workspace; from superplane_bootstrap.admission import required_proofs; from account_factory.dependencies import load; from account_factory.bootstrap import policy_path; from account_provisioning.creation_runner import create_account; assert required_proofs(); load(); assert policy_path("bootstrap-trust-policy.json").is_file()'

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
