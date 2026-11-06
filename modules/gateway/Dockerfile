# Pin the reviewed Python 3.13.16 multi-platform image: its stdlib includes
# the five security fixes previously backported onto 3.13.15. Digest changes
# require source-hash review and the bundled security regressions below.
# Debian package security updates are still applied in the runtime stage.
# AWS-hosted mirror avoids unauthenticated Docker Hub pull rate limits.
#
# Build stage
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS builder
WORKDIR /app
# Refresh pip's vendored trust store as well as the application certifi dependency.
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/pip-26.2.2-py3-none-any.whl
RUN --network=none python -m pip install --no-index --no-deps --no-cache-dir /tmp/pip-26.2.2-py3-none-any.whl \
    && rm /tmp/pip-26.2.2-py3-none-any.whl
COPY pyproject.toml .
# Install core dependencies
RUN pip install --no-cache-dir .
COPY common-wheel/ /tmp/common-wheel/
RUN (cd /tmp/common-wheel && sha256sum -c wheel.sha256) \
    && for wheel in /tmp/common-wheel/adp_common-*.whl; do pip install --no-cache-dir "${wheel}[l5-consumers]"; done \
    && python -c 'from adp_platform_common.l5_contributions import assertions, http, stores'
COPY .l5-release/ /tmp/l5-release/
RUN (cd /tmp/l5-release && sha256sum -c *.whl.sha256) \
    && pip install --no-cache-dir --no-deps /tmp/l5-release/*.whl
# Issue #144: Install tracing dependencies (adds ~20MB but only active when OTEL_ENABLED=true)
RUN pip install --no-cache-dir ".[tracing]" || echo "Tracing dependencies not available, skipping"
# Issue #4203: the AWS Lambda Runtime Interface Client, so this same image can be
# invoked as a container Lambda (the orchestration tick) as well as served by
# uvicorn in the pod. The tick needs src/ plus the async SQLAlchemy stack plus the
# RDS CA bundle below, all of which only exist in this image — no Lambda layer
# provides them.
#
# The default K8s serving command matches the uvicorn CMD below. The optional
# capacity profile declares its own serving arguments.
# The Lambda supplies the RIC via `image_config.entry_point` in
# infra/modules/orchestration-tick/main.tf instead, so the pod path is untouched.
RUN pip install --no-cache-dir awslambdaric==3.1.1

# Verify upstream fixes against reviewed source bytes and security regressions.
# Do not reapply the 3.13.15 backports to the already fixed 3.13.16 sources.
COPY security/stdlib /tmp/gateway-stdlib-security/
RUN python /tmp/gateway-stdlib-security/apply.py --verify-only --manifest-file manifest-3.13.16.json \
    && python /tmp/gateway-stdlib-security/check.py

COPY skills-source/ /app/skills-source/
COPY scripts/build-skills-archive.sh /app/build-skills-archive.sh
COPY cli/adp /app/skills-cli-version
RUN bash /app/build-skills-archive.sh --build /app/skills-source /app/generated-skills /app/skills-cli-version

# Runtime stage
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81

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
RUN python /opt/gateway-stdlib-security/apply.py --verify-only --manifest-file manifest-3.13.16.json \
    && python /opt/gateway-stdlib-security/check.py
COPY l5-wheel/ /opt/l5-wheel/
RUN cd /opt/l5-wheel && sha256sum -c wheel.sha256 \
    && python -m pip install --no-deps --no-cache-dir adp_l5_services-*.whl \
    && python -c 'from l5_services.api.router import create_router'
COPY src/ src/
COPY deployment_evidence/ deployment_evidence/
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
# Import the staged observer with only the standard library available. Missing
# staging, damaged bytes or an adapter application dependency must fail the build.
RUN python -I -B -S src/orchestration/adapter_observer_selfcheck.py
RUN python -I -B src/internal/domain_closed_cleanup_source.py
RUN python -c "from contracts.insights import results; assert results.ResultsReader"
RUN python src/auth/operation_contract.py
RUN python -c "from src.products.jira_context_contract import JiraContextQuery; assert JiraContextQuery.model_fields['limit'].default == 20"
COPY alembic/ alembic/
COPY alembic.ini .
COPY .l5-release/ l5-release/
# Issue #4146: the /setup page's Download button is served by GET /api/cli/{script_name}
# (src/cli_download/routes.py), which reads bg-cognito-auth.sh off disk. Without this
# the route 404s in the image while working fine locally.
COPY cli/ cli/
COPY --from=builder /app/generated-skills/adp-skills.tar.gz /app/generated-skills/adp-skills.tar.gz.sha256 cli/

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
# Both gateway ALBs retain idle target connections for 900 seconds. Keep the
# server connection alive longer so the ALB retires it first instead of racing
# a five-second server close and intermittently returning 502 on connection reuse.
# One async worker per default one-CPU container. Uvicorn 0.34's multiprocess
# supervisor can kill workers while injected Python instrumentation initializes
# before their five-second ping check; Kubernetes owns process health/restarts.
CMD ["uvicorn", "src.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--timeout-keep-alive", "910"]
