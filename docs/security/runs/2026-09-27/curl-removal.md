# Gateway and Superplane API runtime curl removal

Both services use Python HTTP clients and contain no curl subprocess or pycurl
calls. Remove their unused Debian curl/libcurl runtime. The gateway downloads
the same RDS CA bundle using Python urllib with TLS verification and a timeout.
The previously reviewed curl source/backport bundle remains historical evidence;
its build stage and checks are no longer needed in an image without libcurl.
The Superplane API also applies available Debian security updates during build.

| Candidate | Live baseline raw Critical / High | Candidate raw Critical / High |
|---|---:|---:|
| Gateway | 16 / 70 | 0 / 50 |
| Superplane API | 16 / 71 | 0 / 50 |

Same frozen Grype database (2026-09-26T06:29:14Z), no suppressions/only-fixed
filter, and no new Critical/High advisory/package pairs versus each baseline.
The other captured gateway digest (17 / 70) also needs eventual replacement;
these rows do not count it twice or establish any live deduction.
Exact archive/config/SBOM/scan hashes are in `curl-removal-evidence.json`.

## Validation

Both packaged images run as UID65532 with a read-only root, temporary /tmp,
network disabled and no cloud credentials. Curl and system libcurl are absent.
Gateway health/lifespan succeeds using the documented development in-memory
rate-limit fixture; database/cloud integrations are intentionally unavailable.
The real native AWS Lambda Runtime Interface Client polls a mock invocation and
posts its result over loopback to a separate test process. This confirms the
Lambda runtime path works without the removed system library. The RDS bundle
remains present. This is not a live orchestration-tick business-flow test.

The Superplane API starts with strict domain authorization and synthetic issuer
configuration; health reports enforcement and readiness returns503 for the
unavailable database. Its production authentication defaults are preserved.
27 existing gateway container-hardening contract tests pass. Ruff and diff
checks pass. Source functionality CI remains required for merge.

Run the committed checks against the built images from repository root:

```sh
docker run --rm --network none --read-only --tmpfs /tmp \
  -e BG_CONFIG_DIR=/tmp/adp-test \
  -e BG_TOKEN_SECRET_KEY=security-test-only-signing-key-0123456789 \
  -e AWS_EC2_METADATA_DISABLED=true -e OTEL_ENABLED=false -e BG_OTEL_ENABLED=false \
  -e PYTHONPATH=/app -e RATELIMIT_SECURITY_PROFILE=development \
  -e RATELIMIT_ALLOW_MEMORY_BACKEND=true -e RATELIMIT_BACKEND_TYPE=memory \
  -v "$PWD/modules/gateway/tests/container/verify_runtime.py:/test.py:ro" \
  --entrypoint python security27/gateway:fixed /test.py

docker run --rm --network none --read-only --tmpfs /tmp \
  -e AWS_EC2_METADATA_DISABLED=true -e PYTHONPATH=/app \
  -e DATABASE_URL=postgresql+asyncpg://localhost/superplane_test \
  -e SUPERPLANE_DATABASE_ALLOW_UNVERIFIED_LOCAL_TLS=true \
  -e JWT_SECRET_KEY=security-test-only-signing-key-0123456789 \
  -e COGNITO_ISSUER=https://issuer.example.invalid \
  -e COGNITO_JWKS_URL=https://issuer.example.invalid/jwks \
  -e 'DOMAIN_AUTH_ALLOWED_CLIENT_IDS=["security-test"]' \
  -v "$PWD/modules/domain-apps/superplane/src/superplane-api/tests/verify_container_runtime.py:/test.py:ro" \
  --entrypoint python security27/superplane-api:fixed /test.py
```

These environment overrides are test fixtures only. Deployment must retain the
existing database, Redis, trust, authorization and cloud configuration.
No live rollout occurred; live web/Lambda acceptance and digest reconciliation
remain open under #6521. Remaining High findings remain open.
Related to epic #6492 and the cross-image curl story #6511.
