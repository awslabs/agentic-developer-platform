# Explicit knowledge publication admission

The shared `startBindingOperation` API accepts the DevOps-only `sync_knowledge`
action. Saving configuration does not publish native assets. Admission retains
the operation as `blocked`, with `knowledge_worker_unqualified` and a specific
`next_action`; it does not enqueue work or request provider credentials. The
owner must separately authorize native publication and explicitly resume the
original operation. Installed DevOps workers handle that continuation; deployments
without the worker keep the retained operation blocked.

The selected `devops.knowledge_policy.publications` contains one explicit entry
per `source_refs` reference: `source_ref`, `kind`, `scope`, `active` and
`native_metadata`. Only the documented skill and instruction mappings in the
[native contract](native-knowledge-contract.md) are accepted. Publication requires
`product_team` visibility, manual refresh and between one and twenty unique
sources. Instructions require an explicit native agent type and remain active.
Private publication, generic context mapping and automatic refresh remain
unqualified. An empty list never requests deletion. Cleanup is not part of this
action, and the accepted removal set is explicitly empty.

The Product configuration owner reviews the existing `delegation-scope` API with
`for_control=true`, `control_action=devops_knowledge_publication` and the exact
ready `binding_id`, then uses the existing delegation API to record bounded
consent. This grant records publication and source capture/read operations. It
does not grant `credential:session`, native UI access or task execution. Current
Product membership, configuration, connection, binding revision/generation and
the exact normalized publication selection are checked again on use.

The binding-operation body includes `action`, `configuration_revision`,
`binding_id`, `binding_revision` and `binding_generation`. The latter two identify
the current binding before the operation. Standard `Idempotency-Key` and the
binding's quoted `If-Match` ETag are required. A successful admission advances its
revision/generation atomically while preserving readiness for other capabilities.
Same-key replay returns the original operation and never recaptures the sources;
a changed payload conflicts. A second unresolved knowledge operation for the
same binding is rejected and identifies the retained operation.

Before capture, an immutable admission preparation retains the selected scope,
request hash, source references, byte/file bounds, original configuration and
policy hashes, binding/native identity, connection revision and current consent
identity/revision/expiry. The existing Product capture boundary checks each
source's entitlement before and after connector I/O. The final setup intent
retains the exact original snapshot IDs, versioned content and manifest hashes,
mapping version and targeting. Workers read only these admitted snapshots and
cannot repin newer content. Source count is bounded at twenty, capture at 1 MiB per source,
and instruction admission at 25,000 bytes. Retention is bounded by the original
consent expiry.

The owner reads and creates separate consent at
`binding-operations/{operation_id}/knowledge-native-consent`, under the same
Product/project/DevOps API prefix. The preview identifies the original operation,
intent hash, source grant, snapshot IDs, native space and exactly five native
actions: `CreateAsset`, `UpdateAsset`, `GetAsset`, `GetAssetContent` and
`ListAssets`. Creation requires the preview's `scope_digest`, quoted operation
ETag, an `Idempotency-Key` and an expiry within the original source grant's
deadline. Saving this consent does not dispatch work. It grants no task execution,
UI session, deletion, integration mutation or IAM authority. It cannot be used
at the generic credential-session endpoint.

After native consent, the owner calls the existing
`binding-operations/{operation_id}/reconcile` endpoint with a reason and a fresh
idempotency key. It resumes the same SetupWork ID and generation, preserves the
intent, sources, effect history and fence, and advances the fence only when the
next worker claims the work. Replaying the same request does not recapture or
duplicate work. A current lease, changed binding/selection, revoked source access,
expired original consent or unavailable captured receipt prevents continuation.
Native consent may be renewed while the original source authority and retention
remain valid; renewal does not extend them. Expired original admission has no
automatic retention-extension path. Do not edit retained intent/receipt rows or
create duplicate publication to bypass it.

The current rightful Product/configuration owner can cancel a publication before
any native effect starts through `binding-operations/{operation_id}/cancel`.
The request supplies `reason`, the quoted operation ETag and an `Idempotency-Key`.
An expired or revoked source grant does not prevent this local cancellation.
A current worker lease or any `started`, `outcome_unknown` or `ready` effect
prevents cancellation. The cancelled operation, original snapshots, work ID,
generation, fence, plan history and prior published ownership receipt remain
retained. Cancellation makes no native call and no cleanup claim. Replaying the
same key/body/ETag returns the original cancellation; a changed request conflicts.
After cancellation the owner can review a new, explicit publication admission.
After a native effect starts, the current human owner may explicitly acknowledge
abandonment through `binding-operations/{operation_id}/abandon` once the worker
lease ends. The request includes a reason, `native_effects_may_remain: true`, the
operation ETag and an idempotency key. The local operation becomes `abandoned`;
original effects, snapshots and native ownership reservations remain retained.
No native cleanup or successful release acceptance is claimed. See the
[completion design](knowledge-completion-design.md) for the exact boundary.

An explicit resume after the transport dispatch allowance is exhausted grants
at most ten additional dispatch attempts. The audit records the lifetime attempt
count and prior/new limits; the original attempt count is never reset. Replaying
the same resume does not add another allowance. Generic runs and other setup
actions retain their existing ten-attempt limit. Automatic repair cannot grant
more attempts.

The worker rechecks current source rights, owner/native consent and the work
claim before provider calls and durable transitions. Each call uses only the
selected space and accepted publication metadata/content. The configuration,
binding and SetupWork locks fence append-only effect records. `planned`,
`started`, `outcome_unknown` and `ready` events retain the original asset plan,
stable client token, work owner and fence in a hash chain. An uncertain create or
update is reconciled by readback only; missing or changed readback remains blocked
and never authorizes another write. The native API has no atomic compare-and-swap
parameter: ADP checks the expected version before update and verifies the result
independently afterwards. Unrelated assets and occupied instruction targets are
preserved. Cleanup remains outside this action, with an empty removal set.

Successful content publication returns `waiting` with
`knowledge_native_use_unqualified`, `content_written=true` and
`native_use_qualified=false`. Installing and authorizing the worker does not
require earlier native-use proof; controlled publication produces the content
needed for that later acceptance. Completion still requires evidence that a new
native session uses the exact source/version, changes behavior as intended, and
respects revocation and cross-Product isolation.

Deploy the Gateway routes and `179_l5_knowledge_effects` Alembic bridge (which
installs the `l5_knowledge_effects` tables and dispatch allowance column), roll out
the shared immutable L5 artifact with the DevOps factory, and update the DevOps
registry with `l5:knowledge-publication` through the normal modular release path.
Sibling and repair worker registry scopes remain unchanged. The selected Product
connection role must independently allow the five asset actions. The operator-owned
`infra/provider-access` root offers `devops.knowledge_agent_space_arns` as an
explicit opt-in set; its empty default adds no permission to existing DevOps
selections. This source option does not itself apply live IAM changes.

The official [AWS service authorization table][authorization] lists all five
actions with required resource type `agentspace` and defines that ARN as
`arn:${Partition}:aidevops:${Region}:${Account}:agentspace/${AgentSpaceId}`.
The policy therefore targets each selected exact space, without asset wildcards.
Review and simulate the connection and session policies for those actions and
space ARNs before live use; infrastructure installation and policy simulation
are separate from successful publication/native-use qualification.

The bounded human evidence API records actual native-use artifacts and current
owner review against this same operation. It preserves manual provenance and
requires every selected publication before completion. The release qualification
rollup remains separate; full DA19 acceptance still needs its original live checks.
The contract and outstanding task IDs are specified in the
[completion design](knowledge-completion-design.md).

[authorization]: https://docs.aws.amazon.com/service-authorization/latest/reference/list_devops-agent.html
