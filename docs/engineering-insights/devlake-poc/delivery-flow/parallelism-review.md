# Dependency and planned-file review

The native feature graph remains in
[dependencies.json](../dependencies.json).
The flow maps each feature issue to a code phase and a later acceptance phase;
it does not delete native blockers to force work through. Flow-aware dispatch must
use the accepted node's code completion boundary. If the installed scheduler also
requires predecessor GitHub issues to be closed, qualify that interaction before
activation; hold and correct the plan/dispatch mapping rather than closing an
unaccepted story. Do not switch to a board dispatcher with whole-issue blockers.

## Source development

S09 and S12 can run concurrently after S01: their file sets do not overlap.
S05 and S13 can run concurrently after S04; S06 then consumes S13 while S05
finishes. All other dependencies follow reviewed code and interface handoffs,
not deployed predecessor evidence. Developers may prepare bounded fixtures and
test scaffolding early, but cannot merge an implementation whose required
interfaces or independent checks are missing.

| Shared area | Ownership/order | Reason |
|---|---|---|
| Scope service and flags | S12 creates authority; S02 adds runtime admission. | File creation and scope semantics precede optional runtime integration. |
| Pinned Go source/build recipe | S09 creates; S10 patches adapters; S07 integrates release scripts. | One extraction strategy and independent connector/worker artifacts. |
| Cell schema manifest | S09 creates; S02 adds migrator/runtime contract; S03 adds sealing; S07 reconciles final manifest. | Serialize real schema writers; runtime workers never create/alter/drop. |
| Finalizer workflow and dispatcher | S09 creates artifact handlers; S02 adds runtime handlers; S07 consumes. | Failure cleanup must be ready before publication/provisioning. |
| Gateway routes/Profile/UI/CLI | S12 owns initial scope route registration; S10 broker/discovery; S11 Profile consumers. | Existing auth, connections and source authority stay canonical. |
| Insights result authorization/publication | S13 owns reader/policy descriptors; S06 consumes those contracts. | Controlled admitted fixtures qualify reader before real dataset publication. |
| Acceptance harness | Every story owns its scenario file; S07 owns shared fixture lifecycle, campaign, aggregation and independent reader. | No separate deploy/publisher/evidence stack per feature story. |

The proposal's `file_groups` are the single planned-file inventory. `modify` paths
exist at the authoring revision; `create` paths are absent; `modify_after` refers
to a reachable creator. The validator rejects unordered writers, duplicate
creators and missing existing files. These paths are scope forecasts, not repair
authority. If implementation changes them, update the plan before assignment.
The named `devlake_cell_authority.py` migration is a proposed literal filename;
S12 assigns its actual Alembic revision/down-revision against current heads and
updates the file plan if repository migration naming requires another filename.
Pinned extracted upstream sources live in the build workspace; the lock, recipe,
patches and license inventory record them rather than claiming those generated
files already exist in ADP.

## Runtime ordering inside one campaign

1. Verify private target/authority, selected sources, bounded costs, existing
   shared capabilities and independently reachable scheduled/manual finalizer.
   Verify baseline and default-off module absence before module publication.
2. Publish checked immutable artifacts once. Establish S12 controlled authority
   fixtures, then connector runtime and S02 bounded startup/gating checks.
3. Prove S10 real provider discovery and S11 Profile UI/installed CLI in
   connector-only mode, with no collection resources or DSN dependency.
4. Provision only the explicit collection state, run migration-only DDL, verify
   S02 IAM roles/denials, and run S03 collection/sealing. Then S04 admission.
5. Run S05 detailed lineage and S13 controlled admitted-fixture authorization.
   Only then run S06 real publication. No S13-to-S06 acceptance cycle.
6. Complete cumulative S07 soak, aged IAM/credential rotation, revocation,
   isolation, failure finalization and upstream upgrade/rollback. Independently
   read back results. Keep each original AC/SCN outcome.

The campaign steps are scenario/module boundaries, not separate full platform
deployments. The S02 harness uses bounded runtime probes before provider tests;
S12 does not depend on provisioning to prove its controlled contract. S09
bootstrap artifact finalization precedes S02 runtime provisioning/finalization.

Serialize Terraform state changes, migration ownership and deployments to each
physical target. Observe existing automatic main releases and reuse matching
verified artifacts. Never cancel unrelated releases, freeze shared gateway
deployment, or roll back unrelated changes. An incompatible shared release
requires reconciling the candidate and rerunning affected checks. A code-only
fix has no deployment step unless it changes installed artifacts.

## Unattended completion amendment

All three evaluations use the native machine repository reader. Scope/consent,
rubric, delegated review and resource disposition move to initial acceptance.
S09 qualifies repair CI coverage; S07 qualifies actual operations repair, the
short-job credential renewal and 56-hour campaign window, restart recovery and post-verdict issue
closure. S11's response fields come from reviewed schemas automatically. These
checks add no application deployment stage. See [unattended execution](unattended-execution.md).

## Coexistence with other flows

The [parallel execution contract](parallel-execution.md) caps this POC at two
concurrent actions, names the shared CLI merge ordering and records the engine
input-resolution limits. S09 CI owns shared-file checks; S07 owns fresh capability
evidence and shared-target serialization. These are implementation deliverables,
not effects of adding a descriptive prerequisite.
