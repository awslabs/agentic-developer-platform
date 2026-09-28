# Base image pulled from public.ecr.aws (AWS-hosted mirror of Docker Hub) to
# dodge the unauthenticated Docker Hub pull rate limit that periodically
# fails CodeBuild runs with HTTP 429.
#
# Security: as of 2026-09, python:3.13-slim resolves to Debian Trixie (13).
# Bookworm (12) entered LTS on 2026-07-11; Trixie is the current stable with
# the smallest OS-level CVE surface. The rolling tag pulls the latest 3.13.x
# patch (currently 3.13.15). Pin to a digest for reproducible builds when
# needed; the rolling tag is intentional here so that `apt-get -y upgrade`
# in the runtime stage and image rebuilds pick up Debian security fixes.
# Issue #6112: documented base image posture for security scan reconciliation.
#
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

# Reviewed official CPython 3.13 security fixes, with exact before/after guards.
# Patch tooling stays in the builder; a changed base requires explicit review.
COPY security/stdlib /tmp/gateway-stdlib-security/
RUN apt-get update && apt-get install -y --no-install-recommends patch \
    && python /tmp/gateway-stdlib-security/apply.py \
    && python /tmp/gateway-stdlib-security/check.py \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Runtime stage
FROM public.ecr.aws/docker/library/python:3.13-slim

ARG GATEWAY_RELEASE=""
ENV GATEWAY_RELEASE=${GATEWAY_RELEASE}
ENV DEBIAN_FRONTEND=noninteractive
# The service uses Python HTTP clients and starts no curl subprocess. Fetch the
# RDS bundle with the existing TLS client rather than shipping unused libcurl.
RUN apt-get update && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends ca-certificates \
    && python -c "from pathlib import Path; from urllib.request import urlopen; Path('/etc/ssl/certs/rds-global-bundle.pem').write_bytes(urlopen('https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem', timeout=30).read())" \
    && test -s /etc/ssl/certs/rds-global-bundle.pem \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Issue #5675: run the gateway as an unprivileged account instead of root.
#
# This pod mounts the platform's token-signing key, the internal shared secret,
# the GitHub App private key and the agent signing keys. As root, any code
# execution in this process reaches all of them plus the container's own
# filesystem, which turns a moderate defect into a full credential compromise.
# Dropping to an ordinary user is the layer the other two (the pod
# securityContext in k8s/deployment.yaml and the namespace Pod Security
# Admission labels in k8s/namespace.yaml) are measured against: if the IMAGE
# still starts as root, `runAsNonRoot: true` is the only thing standing between
# us and uid 0, and one deleted line puts it back.
#
# UID 65532 and `useradd --create-home` mirror the superplane API image
# (modules/domain-apps/superplane/src/superplane-api/Dockerfile), the closest
# analogue in this repo — Python + alembic, same shape. 16 of the 18 real
# Dockerfiles here already set USER; the gateway was the outlier.
#
# A HOME is created because Python and botocore write first-use caches under it;
# with a read-only root filesystem a homeless user surfaces that as a confusing
# mid-request error rather than a clean failure. The pod also mounts emptyDir
# volumes over the writable paths — see k8s/deployment.yaml.
RUN useradd --create-home --uid 65532 appuser
ENV HOME=/home/appuser

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /usr/local/lib/python3.13/stringprep.py /usr/local/lib/python3.13/stringprep.py
COPY --from=builder /usr/local/lib/python3.13/tarfile.py /usr/local/lib/python3.13/tarfile.py
COPY --from=builder /usr/local/lib/python3.13/urllib/request.py /usr/local/lib/python3.13/urllib/request.py
COPY security/stdlib /opt/gateway-stdlib-security/
RUN python /opt/gateway-stdlib-security/apply.py --verify-only \
    && python /opt/gateway-stdlib-security/check.py
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
RUN python src/auth/operation_contract.py
COPY alembic/ alembic/
COPY alembic.ini .
# Issue #4146: the /setup page's Download button is served by GET /api/cli/{script_name}
# (src/cli_download/routes.py), which reads bg-cognito-auth.sh off disk. Without this
# the route 404s in the image while working fine locally.
COPY cli/ cli/

# Everything above needed privilege (apt-get, the RDS bundle fetch, the contract
# self-check). Everything the container actually RUNS is below this line, so the
# switch goes here: the build keeps its privileges, the served process gets none.
#
# Port 8080 is deliberately unchanged and deliberately above 1024 — an
# unprivileged process cannot bind a port below 1024, so a gateway on :80 would
# have made this switch an immediate startup failure. It is not, which is why
# CMD below is untouched.
#
# The Lambda path (orchestration tick, issue #4203) overrides entry_point but not
# the user, so it inherits this too.
USER 65532:65532

EXPOSE 8080
CMD ["uvicorn", "src.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "4"]
