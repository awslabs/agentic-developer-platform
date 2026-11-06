# Automatic deployment and verification handoff

This is the execution contract for the registered inactive draft. Its technical
prerequisites must be qualified before activation. There is no per-BOLT release approval. The initial accepted policy binds
the repository, dev target, allowed workflows, credential delegation and limits.
Within that scope, passing conditions advance delivery automatically.

## Conditions and ownership

1. Observe the required reviewed merges and workflow-bound CI results. The chosen
   release must include every required BOLT change and compatible peer contracts.
   A previously green run for an older release cannot qualify it.
2. Resolve the immutable source from verified merge evidence. Validate the workflow
   definition and inputs against the accepted scope and acquire the physical-target
   lease. Observe a matching automatic run first; otherwise use the authorized
   deployment controller when its workflow transport is supported.
3. Verify migration/schema, API image, served UI assets, installed CLI checksums and
   peer artifacts as applicable. A successful Actions run is not runtime proof.
4. Pass that verified release receipt to the agents running the BOLT's actual
   UI/CLI/API checks. Record attempts, negative cases, independent readback and
   cleanup. Publish the packet described in [human-checks.md](human-checks.md).
5. Ask for one BOLT evidence decision after execution. Accepted evidence releases
   dependent BOLTs; rejected evidence returns to the existing delivery owner.

D01 #8242 configures and qualifies the first rollout; later V stories reuse it.
B01 V01/V03 share D01's release, and B09 V11 consumes V10's rollout receipt.
Concurrent branches serialize deployment and revision-sensitive verification on
the shared target, or use isolated targets explicitly admitted by the policy.
The eight-action ceiling is not permission to replace a target during its checks.

Peer availability is also checked automatically: B09 needs actual Context/L5
handoff success, and B11 needs attributed Insights events, storage and the read
service. The named integration owners supply those contracts and fixtures. Missing
capabilities produce a specific blocked result; they do not open readiness gates
or turn an unavailable-state test into successful integration evidence.

## Existing support and the remaining gap

Source inspected at `ecf16bb8de4132249c555df790721f654fcf9cf0`:

- `scripts/check-assistant-deploy-boundary.sh` explicitly permits reviewed `main`
  source on the exact gateway/frontend dev targets without a per-release assistant
  revision approval. The presence of `adp_approved_revision` in a workflow is not
  evidence that every dev rollout needs a human.
- The [D2 controller](../../../architecture/deployment-workflow-controller.md)
  derives source from verified merge evidence, checks workflow provenance, observes
  automatic runs and can dispatch configured workflows. D1 serializes the target;
  D3 supplies runtime verification. Packaged unresolved manifest entries still
  need real environment/workflow/runtime bindings. Gateway and migration workflows
  have controller transport; frontend and Insights must have an admitted observation
  or transport path before activation, not an assumed operator dispatch.
- The operations-worker API still refuses dispatch of workflows declaring
  `adp_approved_revision`. Use the controller or matching automatic runs; do not
  bypass this restriction or require a human dispatch after every BOLT.
- Literal `deployment_*revision:` inputs pin a release known at acceptance time.
  The optional, typed `operational_release_binding` described in the
  [authoring guide](../../README.md) binds later engine-verified predecessor
  receipts, workflow definition and manifest target before dispatch. A `node:`
  prerequisite alone checks completion, not release provenance. D01 must still
  qualify this path against authorized live targets before activation; no plan
  amendment per merged release is part of the intended flow.

**Activation prerequisite:** qualify an automatic, verified receipt handoff into
operational verification. D01 owns its integration/qualification, not a general
engine redesign. #5411 must assign the bounded platform prerequisite below if no
already-owned implementation provides it. This prerequisite is proposed, not filed
or counted among the 38 Product stories. Reconcile its owner and include any added
work in the same $2,000/eight-action plan before activation. Manual
per-release amendments are not an acceptable implementation of this proposal.

## Bounded platform prerequisite for the proposal

Outcome: resolve an operate node's release from specifically admitted predecessor
receipts within an already accepted policy, then carry the immutable resolution
through dispatch, run observation, repair and evidence verification. Keep existing
literal-pin contracts compatible. Exclude new deployment engines, production
targets, evidence-review UI redesign and unrelated workflow authoring. Split if
this cannot fit one medium story; do not bury it in D01.

The following are proposed, testable acceptance cases for that prerequisite:

| Case | Required observation |
|---|---|
| Future release | Accept a policy before a predecessor merges; observe the later verified merge/deployment receipt; dispatch verification against exactly that source, workflow definition and target without another human approval or plan edit. |
| Provenance and scope | Wrong tenant, flow, predecessor, target, definition, incomplete merge coverage or stale/replayed receipt produces a typed hold and no deployment/verification dispatch. Changed workflow bytes outside the accepted scope do not acquire authority from a new source SHA. |
| Durable retry | Restart after release resolution or an uncertain dispatch; resume the same durable binding and reconcile the recorded run. No unbounded duplicate dispatch and no silent switch to newer main. |
| Evidence and integration | Exercise the real scheduler → operations gateway → workflow/runtime evidence path. A report for a different revision/attempt cannot pass. Qualify one real dev rollout and client run after the adapter is deployed; helper tests alone do not qualify it. |

No new `required_inputs` syntax is invented in the review graph. Bind the actual
supported contract after this prerequisite is delivered and qualified. If it is
unavailable, keep the registered draft inactive with this explicit engineering gap;
do not silently reintroduce human readiness gates.
