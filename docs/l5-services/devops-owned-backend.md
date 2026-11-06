# DevOps gateway provider checks

The installed gateway exports four request-bound DevOps callbacks: integration
observation, release authority, deployment verification evidence, and prevention
provider access. They use the current human configuration owner, Product team
membership, selected source rights, and verified connection. Checking a disabled
draft does not require creating an execution grant.

## Bounded provider authority

Readiness sessions last at most fifteen minutes. Their session policies limit
DevOps operations to the selected native space and region, with an explicit
expiration. GitHub service discovery is read-only and bounded to ten pages of
one hundred records. Service readback is limited to the selected account and
region. IAM permission simulation can inspect only the selected connection role.

The action/resource mapping follows the [AWS DevOps Agent authorization
reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_devops-agent.html).
AWS scopes `UpdateGoal` to an agent space. The gateway additionally confines that
method to the exact configured goal and an enabled/disabled state change.
Prevention status receives no `UpdateGoal` permission. Schedule updates require
the existing explicit owner consent and budget checks; their session policy
expires no later than that consent. Missing provider installation fails closed.

Provider checks never register services, associate repositories, launch jobs,
enable automation, or drive a browser. Native setup remains a separate explicit
operation. A native integration with enabled, unrequested automation remains
blocked. Event delivery is unverified when the native API supplies no evidence.

## Release readiness

Release admission independently reads the selected GitHub repository and exact
base/head commits, one successful deployment for the head, the exact native
repository/space association, and the connection role's `CreateBacklogTask`
permission. Missing or ambiguous deployment records produce an actionable
prerequisite instead of an eligibility claim.

The native API does not expose indexed commit revisions. The established
`INDEXING_UNAVAILABLE` contract remains explicit: `source_indexed` is `null`,
`indexed_revisions` is empty, and qualification records the native API limitation.
The existing fallback also requires the selected association's release-review
capability to be enabled. Source access alone does not prove native indexing.

Both exact commits must be present in the current configuration's `source_refs`
and its retained owner consent. Admission creates a child grant containing only
those commits and the selected credential. Its capture context is retained
unchanged in durable admission preparation. A historical base can be captured
and read only for explicitly scoped DevOps release consent while the selected
head and repository access remain current. This does not authorize arbitrary
repository history or change revision rules for other services.

## Independent deployment evidence

The verification reader does not infer isolation, privilege, network boundaries,
or deployed SHA from the request. A deployment owner must install an independent
evidence producer. No producer is installed by this change; missing evidence
returns `deployment_evidence_producer_required`.

The server binds an explicit owner-scoped session and an immutable evidence
reference to each admission. The reader does not use ambient Gateway credentials
or a process-global evidence reference. The reference has these nonempty fields:

| Field | Meaning |
| --- | --- |
| `bucket`, `key`, `version` | Immutable S3 object version; `null` versions are rejected |
| `sha256` | SHA-256 of the exact signed envelope bytes |
| `owner_account_id` | Expected S3 bucket owner |
| `signing_key_arn` | Exact trusted asymmetric KMS signing key |
| `producer` | Expected independent producer identity |

The object is at most one MiB and contains exactly `payload` and base64
`signature`. KMS verifies the SHA-256 digest of the canonical payload using
`RSASSA_PSS_SHA_256`; canonical JSON sorts keys and uses compact separators.
The payload has exactly these fields:

- `schema_version`: `1`.
- `producer`: the trusted configured producer identity.
- `scope`: exact `tenant_id`, `product_id`, `project_id`.
- `configuration`: exact `id`, `revision`, `hash`.
- `admission`: exact `operation` and `idempotency_key`.
- `issued_at`, `expires_at`: timezone-aware timestamps. Evidence must be issued
  within five minutes and expire within a day.
- `observation`: the deployment observation required by
  `devops/verification_targets.py`, including exact scenario artifact versions
  and hashes. The common validator independently enforces deployed SHA,
  isolation, non-admin privilege, selected network, fresh observations,
  run deadline coverage, and scenario/cleanup limits.

The producer needs signing authority; evidence validation uses `kms:Verify` on
that trusted key and versioned read access to the pinned object. Caller-supplied
evidence references cannot choose a different key, producer, bucket, or object.

The unmounted queue admission factory derives two short-lived sessions from
the current owner's selected AWS role. One reads the exact fixture; the other
signs, writes and reads only its admission and observation evidence prefixes.
Neither can invoke a function, delete data, change IAM or submit native work.
Current JWT owner/team access, configuration, source, binding, child grant and
parent consent are checked before and after session issuance and at evidence
boundaries. Each request owns its evidence reference. Retries preserve the
reader through admission, while a receipt replay does not regenerate evidence.

The two exact policies must each fit STS's 2,048-character inline-policy limit;
oversized policies fail instead of widening resource scope. The factory remains
unmounted pending separate native execution/result qualification.

## Remaining prerequisites

One repository per integration is currently supported by native readiness
observation. Workflow deployment and CloudWatch resource/native qualification
need their own installed evidence paths and return explicit unavailable reasons.

The existing release-verification worker adapter also requires independent
scenario outcomes, native result observations, and an owned-fixture cleanup
driver. A signed admission observation does not provide those capabilities, so
this gateway change does not install or claim a working verification executor.
