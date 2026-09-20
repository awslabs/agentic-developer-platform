# Deployment workflow controller (ENGINE-D2, #5151)

The K2 runner handles `deployment_pending` after the M2 merge receipt proves code
completion. D2 selects every matching entry in the packaged D1 manifest using
changed repository paths. Selection cannot come from issue prose. Each enabled
entry pins the merged artifact and workflow definition. Unresolved entries stay
blocked; this change does not activate the packaged targets.

A deployment requires the accepted policy's `deploy` permission, environment
connection, and explicit v2/v3 user-credential approval of both the vault entry
and AWS role. The existing vault ACL and tagged STS service resolve that role.
STS and EKS readback establish the physical target. No platform-account fallback
is used by the controller. A completed worker's grant is checked through an
engine-only delivery continuation bound to its current claim and merge receipt;
the default worker authorization path still refuses a passed story.

D2 observes a matching automatic run first. GitHub run metadata alone cannot
prove target or inputs: the approved workflow publishes a bounded context
artifact, and the provider checks repository/run/attempt, source and definition,
physical target, approved inputs, archive digest and contents. A reusable
migration run also requires the gateway caller definition to match at the
approved and source revisions, and its provider reference must pin the child.

Only `gateway-deploy.yml` and `run-gateway-migrations.yml` implement the optional
engine transport: `adp_correlation`, `adp_source_revision`, and
`adp_definition_revision`. The engine derives these values; they cannot be
business-input overrides in the manifest. Dispatch uses the default branch,
checks its workflow bytes against the approval, and the workflow checks the
actual definition revision before deployment. All source checkouts and gateway
image tags use the immutable source input. The correlated run name is
`ADP deployment <correlation>`. Existing automatic triggers remain unchanged;
absent transport values retain the normal manual source selection.

The context artifact is named
`adp-deployment-context-<workflow filename>-<run attempt>` and contains exactly
`deployment-context.json`. Only explicit non-secret fields are serialized.
Target identity is read using the workflow's resolved credentials. Failure to
prove the target prevents publication and adoption.

The durable K1 action contains the exact definition, inputs, correlation and
target. D1 serializes the physical target before dispatch. No ledger or lease
lock is held over provider I/O. The dispatch-started marker commits before the
HTTP mutation. A timeout retains the action and lease; later ticks reconcile
the recorded run or correlation instead of blindly dispatching again. Historical
observation uses the saved approval, even when a newer branch, manifest or
attempt allowance would refuse a new deployment. This is not an exactly-once
guarantee for the external provider.

Workflow completion, including failure/cancellation, produces a bounded
`WorkflowReceipt` and advances only to `awaiting_runtime_verification`. The
receipt retains target, run, artifact and conclusion evidence; D2 never marks a
deployment verified or releases its lease on workflow green. The existing
execution read model exposes a small run summary, excluding credential inputs.

ENGINE-D3 consumes that receipt. After verified runtime evidence and safe lease
settlement, D3 marks the corresponding action's `runtime_verified` field and
returns to `deployment_pending` when `remaining_entry_ids` is nonempty. D2 then
handles the next entry; it can observe the gateway run's migration artifact
without dispatching migrations twice. D3 owns final acceptance and rollback
decisions. A reviewed documentation-only selection creates a separate durable
`deployment_handoff`, with no external deployment effect, for D3's acceptance
path. An all-entries-verified handoff likewise preserves the final acceptance
boundary.

Code merge, deployed revision readback and live qualification are separate.
No authority activation, infrastructure apply, worker drain, credential
projection or qualification run is performed by this implementation.
