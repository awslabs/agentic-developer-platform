# Durable L5 consumer receivers

Context and Engineering Insights store immutable L5 results independently of the
producer outbox. Context uses its own PostgreSQL database; Insights uses its
versioned evidence bucket. Both use conditional writes, preserve the original
admission receipt across replay, and retain monotonic invalidation tombstones.
Neither receiver launches a provider job.

The shared contract and state machine live in
`libs/python/adp-common/src/adp_platform_common/l5_contributions/`. Consumer
admission does not use personal memory or the business contribution catalogue.

## Wire contract

All envelopes retain the producer's complete scope, immutable result reference,
source/evidence references and result body. Security retains its existing
envelope without a `kind` property; its `result_ref.record_type` identifies it.

| Service | Suffix | Destinations |
| --- | --- | --- |
| Security | `l5-contributions` | Context |
| DevOps | `l5-devops-contributions` | Context, Insights |
| FinOps | `l5-cost-contributions` | Context, Insights |
| Transform | `l5-transform-contributions` | Context, Insights |
| Well-Architected | `l5-wa-contributions` | Context, Insights |

The base is `/internal/v1/{context|insights}/{suffix}`. `POST` admits the frozen
envelope; `GET /{contribution_id}` returns the original receipt; `GET
/{contribution_id}/projection` returns the original complete envelope plus
`contribution_hash`. A durable receipt contains `receipt_id`, `contribution_id`,
`admitted_schema`, `consumer_revision`, `record_refs`, `contribution_hash`, and
`claim_count`. `record_refs` always includes the exact result reference, including
for a Security result with no findings.

Security claim pages return HTTP 202 until every page is durably present and the
reconstructed full envelope matches the producer's original digest. Incomplete
results cannot be read. The receiver accepts retained legacy empty first pages
and a bounded single claim with more than 100 evidence references.

`POST /{contribution_id}/invalidate` applies a producer-owned watermark. A lower
watermark cannot restore visibility. Removing a revoked contribution is allowed
only for its exact registered producer and stored pending invalidation. That
permission does not authorize a new admission or content read.

`GET` and `POST /{contribution_id}/tasks/{task_id}` read/create durable later-task
receipts. The Gateway verifies the canonical Task's owner, current standing
policy, Product, project, and causal independence. An Insights usage receipt
references the already durable Context retrieval receipt. `GET` never creates
usage. Revocation fences cached task receipts as well as new reads.

## Authority and readback

Workers call their existing IAM-authenticated Gateway transport and supply the
durable run delegation. Gateway compares admission bytes with the stored outbox
envelope, including exact Security pages. It then signs a 30-second Ed25519
assertion bound to method, path, body, audience, actor, result and current
publication snapshot. Consumer public keys cannot issue assertions.

Both receivers call the Gateway's non-data authority endpoint before durable
effects, replay, and read release. That endpoint rechecks current Product
membership, relationship, source visibility, binding generation, grant and parent,
immutable run and publication state. The Gateway also rechecks before returning
the peer's response. Unsigned direct requests and another destination's assertion
are rejected. No positive authority cache is used.

Human readback is available under the ordinary Product service prefix:

```
/v1/products/{product_id}/projects/{project_id}/services/{service_id}
```

Append `/contributions/{contribution_id}/consumer/receipt` or `/projection` for a
GET. Append `/contributions/{contribution_id}/consumer/tasks/{task_id}` for GET or
POST with an empty body. These calls preserve the authenticated human identity;
they do not impersonate the producer. Existing WA design-usage calls use the same
mediation and canonical Task checks.

## Installation and qualification

Follow the canonical deployment guide and the consumer modules' deployment
owners. Installation, native qualification and consumer acceptance are separate.
Do not enable unqualified consumers or alter existing run/outbox history.

1. Deploy the Gateway with the shared library and new routes. Configure
   `L5_CONSUMER_TARGETS` as a JSON map from `product-context` and
   `engineering-insights` to their private service origins. Reuse the existing
   protected Gateway Ed25519 signing key and public key distribution.
2. Run Context migration `016_l5_contributions` using the normal blocking
   migration job, then deploy the Context MCP image. Its build stages the shared
   library. The receiver uses the existing database pool and
   `DOOR_VERIFICATION_KEYS`; its authority origin is `L5_AUTHORITY_GATEWAY_URL`,
   falling back to the existing `GATEWAY_CALLBACK_URL`.
3. Build Insights with repository root as context and
   `modules/insights/emitter/Dockerfile`. Apply its module-owned infrastructure
   with the immutable emitter image, public verification keys and private
   authority origin. The emitter retains its existing role/boundary and receives
   GetObject only for its own L5 contribution prefix and KMS decrypt through S3.
   The ClusterIP receiver admits network traffic only from Gateway pods. The
   Terraform replica default remains zero; start it only for bounded installation
   verification/qualification. Retain S3 versioning and tombstones on rollback.
4. Install the actual modular L5 workers, queues, repair jobs, database access and
   registered service identities. Gateway deployment alone does not install them.
   Their existing `worker_environment` map carries `L5_CONSUMER_CONTRACTS` after
   qualification. Absent configuration disables publication without disabling
   native execution. Security and DevOps factory wiring belongs to their service
   release; FinOps and Transform use the same per-run binding wrapper.
5. Exercise installed own/foreign-scope admission, replay after lost acknowledgement,
   exact durable projection readback, source/grant/parent revocation, monotonic
   invalidation, and actual later-task use with current Task policy. Record
   acceptance evidence separately from image or infrastructure success.

`L5_CONSUMER_CONTRACTS` maps service ID to destination to a contract containing
exactly `kind`, `schema_version: "1.0"`, `path`,
`private_read_fencing: true`, and a nonempty `qualification_ref`. The reference
records completed qualification; the string itself is not evidence. WA retains
its existing `L5_WA_PEER_CONTRACTS` configuration. Human task use also requires
existing human Task enrollment and policy; receiver installation never grants it.

## Bounded installed qualification

`modules/L5-services/scripts/qualify-consumers.py` exercises retained results
through the installed repair Lambda's actual IAM identity. It does not submit
provider work, create runs, reset attempts, edit SQL, change infrastructure or
issue credentials. The repair role has a separate registry identity and only its
service's contribution HTTP paths. It cannot use the producer's credential or
native execution routes.

Select an existing completed result and its durable outbox rows. Keep normal
`L5_CONSUMER_CONTRACTS` and `L5_WA_PEER_CONTRACTS` absent during this check. Through
the owning service's `worker_environment`, install
`L5_CONSUMER_QUALIFICATION_CASES` as a JSON list of the exact selected cases. Each
case contains exactly:

| Field | Value copied from the durable result/outbox |
| --- | --- |
| `service_kind` | `security`, `devops`, `finops`, `transform`, or `well_architected` |
| `kind` | Exact result type, such as `finops_cost_result` |
| `destination` | `product-context` or `engineering-insights` |
| `schema_version` | `1.0` |
| `contribution_id`, `contribution_hash`, `run_id` | Existing immutable IDs and digest |
| `scope` | Complete tenant, Product, project, owner, visibility and relationship revision |
| `result_ref` | Complete existing connection/type/ID/revision/tenant/project reference |

The installed wrapper rejects different scopes, revisions, hashes or service
ownership. Use one result's Context/Insights pair per service invocation; Security
has only Context. WA requires an existing comparison result with revision greater
than one. The check may activate its existing schema-unadmitted publication debt
using the normal bounded WA repair transition; it never creates a new result.

Create a private mode-0600 fixture outside the checkout containing exactly these
top-level fields:

| Field | Required selection |
| --- | --- |
| `protocol` | `l5-consumer-fixture-v1` |
| `expected_account`, `region` | Verified deployment target |
| `repair_function` | Exact unqualified installed repair Lambda ARN |
| `expected_code_sha256`, `expected_role_arn` | Observed installed archive digest and repair IAM role |
| `service_id`, `cases` | Installed service and the exact case list above |
| `gateway_url` | HTTPS Gateway origin |
| `owner_token_file`, `foreign_token_file` | Absolute mode-0600 files containing current access tokens, or JSON with `access_token` |
| `reads` | One object per contribution with exactly `contribution_id`, `task_id`, `foreign_task_id`, `foreign_product_id`, `foreign_project_id` |

The later task must already exist, belong to the authenticated owner and selected
Product/project, have current Task enrollment and standing policy, and be created
after the original run admission. Use the same task for a result's Context and
Insights cases. The foreign actor, Product and task must be real independent
fixture selections; the checker does not create them or enroll anyone.

Run from the repository root in the installed Gateway/L5 Python environment:

```bash
PYTHONPATH=modules/L5-services/src python modules/L5-services/scripts/qualify-consumers.py \
  --profile "$AWS_PROFILE" --fixture "$PRIVATE_DIR/consumer.json" \
  --phase admit --output "$PRIVATE_DIR/consumer-admit.json"
```

Before any fixture event, the checker verifies STS account, exact Lambda ARN,
code digest, role, handler, active state, case configuration and version. It first
invokes the safe `{"l5_runtime_probe":1}` request and requires the installed
`consumer_fixture_protocol` marker. Older recovery handlers treat unknown events
as scheduled repair, so calling a fixture event without this probe is unsafe.

Admission records original durable receipts, exact complete projection digests,
replay equality, native job/readback fingerprints, current owner/foreign
actor/Product/task reads, and real later-task use/readback. Insights use must link
to the actual Context retrieval receipt. This first report remains incomplete.

After an actual authorized Product source/grant revocation has produced the
durable owed invalidation rows, run:

```bash
PYTHONPATH=modules/L5-services/src python modules/L5-services/scripts/qualify-consumers.py \
  --profile "$AWS_PROFILE" --fixture "$PRIVATE_DIR/consumer.json" \
  --phase invalidate --baseline "$PRIVATE_DIR/consumer-admit.json" \
  --output "$PRIVATE_DIR/consumer-invalidate.json"
```

The second phase refuses to invent a tombstone. It checks stale-watermark
rejection, actual invalidation, rejection of late original-byte replay, and loss
of owner/foreign projection and task-receipt access. Both reports must refer to
the same pinned installation and immutable cases. Evidence files use mode 0600;
tokens and response bodies are never printed. Failed reports identify the failed
step and a concrete operator action.

`bounded_consumer_checks_complete` records only these observed consumer checks.
It does not assert native service acceptance or complete every original story
criterion. After the applicable service qualification is complete, preserve the
private reports, remove `L5_CONSUMER_QUALIFICATION_CASES`, and install normal
contracts referencing that evidence. Retain all outbox history and tombstones.

Gateway targets persist through the protected SSM parameter
`/adp/<environment>/gateway/l5-consumer-targets`. Its default is `{}`. Both
deployment renderers and runtime accept only private Kubernetes service origins,
such as `http://context-mcp.example-context.svc.cluster.local:5100` and
`http://insights-emitter.adp-insights.svc.cluster.local:8080`; paths, credentials,
public hosts and metadata endpoints are rejected.

The public Ed25519 keyring is already distributed in ConfigMap
`adp-control-verification-keys`, namespace `adp-agents`, key `keys.json`. Context's
`scripts/ensure-door-identity.py` copies that public map into its namespace. Never
copy the Gateway signing private key. Insights consumes the public map through
`l5_consumer_verification_keys` and the private callback origin through
`l5_authority_gateway_url` in its owning Terraform module.

## Verification

The shared-library durable tests use real local PostgreSQL, separate connection
pools, and independent clients against a versioned S3 test service. Gateway tests
exercise current persisted Product rows and the canonical DynamoDB Task/policy
adapters. Insights Terraform mock tests verify the bounded storage permissions,
retained role boundaries, network selectors and disabled deployment default.
These tests are implementation evidence; installed acceptance remains required.
