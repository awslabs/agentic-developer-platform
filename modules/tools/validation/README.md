# Dedicated Task validation service

The service executes admitted checks without giving shared Task workers Kubernetes
credentials. The worker persists a bounded source manifest as a Task result
artifact, then sends only its artifact ID, digest, length and admitted check name.
The service independently fetches gateway-authorized provider source, reconstructs
the exact Git tree, and runs the authoritative image/argv in an isolated Pod.
Worker and service commit hashes differ; receipts preserve both and bind the tree.

`POST /tools/validation` uses API Gateway AWS_IAM plus forwarded Task workload
proof/run credential. Public operations are `inspect`, `run`, `status` and
`cancel_jobs`. Task scope comes from fresh gateway authorization. The public body
cannot select commands, image, URLs, namespace, tenant or Kubernetes credentials.
Result artifact reads require same-Task attachment and tenant/owner identity.

A separate DynamoDB table holds at most 128 operations per Task. Admission and
claim are transactional and use the monotonic Task-version mirror. One active
owner holds an opaque nonce; duplicate delivery never grants a second execution.
Up to three deliveries, at least five seconds apart, can repair lost queue delivery.
Pending delivery exhaustion remains unresolved; it never implies a failed check.
Cancellation fences admission/claim and waits for the active owner. Unknown
execution retains the active fence, even if the Pod or job row is missing.
Kubernetes intent and observed termination remain authoritative for cleanup.
The execution monitor rechecks Task authority, durable cancellation and remaining
Lambda time. Checks are capped at 120 seconds within a 180-second invocation.

Build from the repository root:

```sh
docker build --platform linux/amd64 --provenance=false \
  -f modules/tools/validation/Dockerfile -t adp-validation-service:qualification .
```

Lambda requires a single architecture image manifest. Disabling build provenance
here prevents Docker from wrapping the image and its attestation in a manifest
index that Lambda cannot execute. After pushing to ECR, supply the immutable
registry digest to Terraform; a local image/config digest is not that reference.

The image contains Git, source reconstruction, validation, Task authority and job
storage code. It contains no SDK, model runtime, provider credentials or shell tool
endpoint. Repository code executes only in the credential-free validation Pod.

The standalone `infra` module defaults both provisioning and capability to off.
It creates a dedicated Lambda IAM role, encrypted job table, AWS_IAM API route,
private async self-invocation, EKS access entry, and bindings to the existing
validation namespace Role and exact namespace-read ClusterRole. The worker IAM
policy permits only the service API route. No cluster-wide EKS access policy is
attached. Lambda retries are disabled; the durable claim handles delivery repeats.

Deployment requires the canonical
[agent deploy guide](../../../../docs/adp-platform-deployment/deploy-with-agent.md),
a reviewed plan for the confirmed account, and qualified validation isolation from
`modules/agent-factory/webhook-ingress/infra/codex-validation.tf`. The module
resolves the endpoint, CA and VPC from the selected EKS cluster and creates a
dedicated Lambda security group with HTTPS egress and TCP 443 ingress to the
cluster API. The supplied private subnets must belong to that VPC and have routes
to gateway Task endpoints, DynamoDB and Lambda APIs. Subnet routing still needs
live verification; a security-group rule alone does not provide connectivity. Supply the gateway's existing service-account registry table; the module registers
the dedicated role with internal scope and no allowed models. The gateway still
verifies the forwarded Task workload and credential on every operation. Publish
the added API route through the platform's existing API deployment owner. This
module does not create an alternate gateway identity store or enable a persona.

After deployment qualification, set webhook-ingress Terraform
`codex_validation_service_endpoint` to the reviewed API route. This supplies the
following host configuration without enabling personas:

```text
ADP_CODEX_VALIDATION_BACKEND=service
ADP_CODEX_VALIDATION_SERVICE_ENDPOINT=https://<api>/<stage>/tools/validation
```

The model cannot select these values. A replacement host retains its authenticated
attempt and asks the service to stop/reconcile prior work before Task settlement.
Unresolved owner or Pod evidence prevents successful finalization. Disable new
capability admission while retaining cleanup access during rollback.

Run isolated tests from the repository root:

```sh
python3 modules/agent-factory/codex-harness/test/run-isolated.py -- \
  python3 -m pytest modules/tools/validation/tests
```

Unit/service tests use Moto; real Kubernetes and SDK qualification remains
separate. Local delivery substitutes HTTP/IAM and Lambda scheduling and therefore
does not prove deployed IAM, gateway registration, EKS access or production Task
admission. No production deployment or persona rollout is implied by these tests.
