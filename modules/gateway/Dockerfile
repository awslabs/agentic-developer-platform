# Base image pulled from public.ecr.aws (AWS-hosted mirror of Docker Hub) to
# dodge the unauthenticated Docker Hub pull rate limit that periodically
# fails CodeBuild runs with HTTP 429.
# Build stage
FROM public.ecr.aws/docker/library/python:3.13-slim AS builder
WORKDIR /app
COPY pyproject.toml .
# Install core dependencies
RUN pip install --no-cache-dir .
# Issue #144: Install tracing dependencies (adds ~20MB but only active when OTEL_ENABLED=true)
RUN pip install --no-cache-dir ".[tracing]" || echo "Tracing dependencies not available, skipping"
# Issue #4203: the AWS Lambda Runtime Interface Client, so this same image can be
# invoked as a container Lambda (the orchestration tick) as well as served by
# uvicorn in the pod. The tick needs src/ plus the async SQLAlchemy stack plus the
# RDS CA bundle below, all of which only exist in this image — no Lambda layer
# provides them.
#
# The image's ENTRYPOINT/CMD are deliberately left alone: the K8s deployment
# overrides neither, so it relies on the uvicorn CMD at the bottom of this file.
# The Lambda supplies the RIC via `image_config.entry_point` in
# infra/modules/orchestration-tick/main.tf instead, so the pod path is untouched.
RUN pip install --no-cache-dir awslambdaric==3.1.1

# Runtime stage
FROM public.ecr.aws/docker/library/python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL --retry 3 \
         -o /etc/ssl/certs/rds-global-bundle.pem \
         https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    && test -s /etc/ssl/certs/rds-global-bundle.pem \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src/ src/
# Issue #4969: the shared Bedrock pricing policy and its immutable rate snapshots.
# src/budget and src/proxy import it at request time, and the migration seed in
# alembic/ is validated against the same snapshot files, so the package (including
# snapshots/*.json) must be present in the runtime image, not just installed as a
# build-time dependency.
COPY pricing_policy/ pricing_policy/
# Issue #5146: the shared review-result contract — ONE normative validator read by
# the gateway, the orchestration tick and the agent worker, so producer and consumer
# cannot drift (#4029). It has no pyproject.toml and is not pip-installed, and the
# repository-root `contracts/` tree is outside this build context
# (`cd modules/gateway && docker build .`), so `scripts/stage-contracts.sh` copies it
# into the context first. Without this the review path fails closed at runtime with
# `contract_unavailable` — honest, but a total outage discovered on first use, which
# is why `src.orchestration.review_contract_selfcheck` gates the build below.
COPY contracts/ contracts/
COPY alembic/ alembic/
COPY alembic.ini .
# Issue #4146: the /setup page's Download button is served by GET /api/cli/{script_name}
# (src/cli_download/routes.py), which reads bg-cognito-auth.sh off disk. Without this
# the route 404s in the image while working fine locally.
COPY cli/ cli/

EXPOSE 8080
CMD ["uvicorn", "src.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "4"]
