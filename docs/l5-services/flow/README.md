# Security delivery flow

This bundle turns [epic #8682](https://github.com/aws-e/adp/issues/8682) and the
[delivery waves](../delivery-plan.md) into an engine proposal. The
[public proposal](proposal.authored.json) has illustrative tenant and target
bindings; it must not be submitted unchanged. The authenticated Delivery Flows
page holds the registered instance and its exact initial-trigger hash.
The [registration summary](submission.json) records the inert draft and checks;
it is not an execution or live-acceptance receipt.

There are **16 development nodes, one later SA15 operations node, and four
machine evaluations**. The server adds the initial acceptance node. SA15's two
nodes are ordered phases of the same issue; SA16 constructs the harness before
SA15 runs it. No recurring human gates are authored. The final engine wave
contains logical waves 4 and 5, with one release owner through qualification.

## What starts and what must be built

The first three waves and SA16 harness construction finish on reviewed, verified
merges and phase tests. Ordinary gateway/frontend auto-deployments continue
without gating successor code work. Use `Refs #N` on construction PRs where the
issue still requires live evidence. Keep all original AC/SCN IDs and native
parents; a construction-node pass does not accept live criteria or close the epic.

[L5 Services CI](../../../.github/workflows/l5-services-ci.yml) supplies the
`L5 contract and module tests` check from the first story. It runs on every PR,
so changing a file outside the planned map cannot silently remove this evidence.
Initially it verifies the existing specification examples. SA01 adds its contract
tests; SA02 supplies the package's offline `dev` dependencies; later stories add
their real module suites. Live tests use the separate protected qualification
workflow. Existing host CI remains required for host changes. A green design
check is never proof that the missing handlers have been implemented.

SA01 resolves the module/feature catalog mappings before dependent story work.
SA02 owns delegated Product/source access. SA11 owns Context interoperability.
The Product profile can be read today; source, provider and Context capabilities
must be verified through their actual APIs before the live phase.

SA15 builds the grouped release workflow, packaging, schema integration, identity
and runtime checks. SA16 builds the live drivers, independent reader, workflow
integration and acceptance rollup. Those are future deliverables. Registering the
flow does not assert that L5 workers or its release workflow already exist.

## Optional planned files

`file_groups` is display metadata for the expandable **Planned files** panel.
It is optional, non-exhaustive and non-binding. Agents may add, move or change
unlisted files as needed for the accepted behavior, without first amending this
map. A missing list or a diff that differs from it must not fail a node, review,
evaluation or release. No execution-policy `repair_paths` is derived from it.

## Release handoff for SA15 and SA16

The operations node uses the existing `workflow-evidence/v2` delivery contract
and `repository-evaluation/v1` machine verifier. Implement this handoff within the
existing product/module boundaries; do not add an L5-specific orchestration engine.

1. SA15 introduces `.github/workflows/l5-services-release.yml`; SA16 integrates
   its `.github/workflows/l5-security-live.yml` procedures into the combined run.
   The top-level successful job is named `L5 release and live qualification`.
   The protected workflow accepts the engine's correlation, source revision,
   definition revision and contract digest inputs. Keep credentials and actual
   target configuration in protected storage.
2. Source selection uses verified SA15 and SA16 merge receipts and ancestry.
   SA16's receipt pins the complete workflow definition, including live-harness
   integration. The real existing source baseline in the delivery contract is
   an ancestry anchor. Its `definition_revision` is superseded by the explicit
   `definition_predecessor` at resolution; it is not a claim that the future
   workflow exists at the baseline. Never invent future commit hashes.
3. Reuse compatible gateway/frontend automatic deployments. Apply accumulated
   L5 infrastructure and publish the compatible wheel/workers once. Verify actual
   source/package/schema/runtime identities, immediate event dispatch and delayed
   ten-minute polling. Readiness is intermediate progress inside this operation.
4. Execute real SA16 journeys, resume long assessments from durable attempts,
   and retain the release owner through recovery and cleanup. A success-only
   wrapper cannot replace the required negative, isolation and recovery cases.
5. Emit the built-in `delivery-evidence` artifact with version-2 delivery evidence,
   actual target/revision identity, every `required_checks` result and successful
   cleanup. Also emit `l5-security-qualification/qualification.json` for the final
   machine evaluation, described below. Raw evidence remains private.

The selected existing platform connection and namespace are private registration
bindings. No new AWS account/environment is implied. The logical `l5-security`
component describes the coordinated runtime result; the generic delivery contract
binds it to the selected real namespace without modifying another flow's manifest.

## Final machine evidence

`qualification.json` is produced by the protected workflow and independently
checked against provider, Product, source, runtime and client observations. It
contains the workflow provenance fields required by the repository verifier and:

| Field | Required result |
|---|---|
| `schema_version` | `1` |
| `source_revision`, `definition_revision`, `gateway_revision` | Actual revisions matching the verifier's resolved source/definition; gateway source matches the qualified candidate |
| `run_id`, `run_attempt` | Strings identifying this workflow run and attempt |
| `epic_criteria` | Exactly EC-01 through EC-09, each with `id`, `outcome: passed` and `evidence_verified: true` |
| `story_criteria` | All 49 qualified IDs, `SA01/AC-01` through each story's existing ACs; SA16 has four |
| `scenarios` | All 49 corresponding qualified SCN IDs; preserve each scenario's required verification layer |
| `live_checks` | Every independently observed release check listed in the proposal's delivery contract, with `outcome: passed`, `observation: independent-live`, `evidence_verified: true` |
| `cleanup.outcome` | `passed`, backed by owned-resource readback |
| `unresolved` | Empty; missing, skipped or inconclusive required evidence remains incomplete |

Each criterion/scenario record additionally carries requirement revision,
source/runtime revisions, run/attempt identity, actual verification layer and
private evidence references. The independent reader must validate those fields
and observations before setting `evidence_verified`. The engine verifies the
workflow's identity, revision, successful job, artifact provenance and complete
expected record sets; it cannot turn a self-reported boolean into live proof.
SA16 must exercise rejection of fabricated, stale, wrong-source, incomplete and
cleanup-failed reports. Only complete accepted evidence permits issue/epic closure.

## Bounds and recovery readiness

The draft proposes a seven-day window, a USD 1,000 ceiling, three attempts per
node and at most three concurrent actions. The one operations node serializes
the planned L5 release. These are proposed bounds until initial acceptance; no
credential, spending authority or deployment is activated by registration.

The operations source-repair path supports an explicit schema-v3
`repair_scope: repository` policy with the existing `operate`, `repair` and
`merge` actions and required CI checks. The owner can accept that scope through
the existing plan amendment API; this does not derive authority from the human
file map or introduce another checkpoint. Previously accepted policies are not
silently expanded. Until the flow has accepted this scope, omitted
`repair_paths` still grants no operations file edits. Recovery qualification
must prove that repository, workflow, credential, CI and merge boundaries remain
enforced and that native repair receipts advance the pinned release revisions.

## Registration and verification

Use private binding data to replace the illustrative tenant, environment
connection and namespace, and set `product_id` to an accessible Product. Verify GitHub repository identity, current connection
metadata, CI workflow identity and installed verifier digest. Preview through
`POST /orchestration/flows/drafts/preview`; inspect the transformed graph for
exactly one initial gate, four machine evaluations and the optional file groups.
Register with `adp flow create --file PRIVATE_PROPOSAL --json`, without `--yes`.
The normal non-interactive result is a pending draft and exit code 4.

Read back the stored plan and execution state. The initial trigger uses its
current gate ID and exact plan hash. Do not reuse a hash from the sanitized
public proposal. Registration and local checks prove plan consistency, not
live implementation, provider eligibility or completed delivery.

If a registered draft has no Product, select one in its initial approval panel
and choose **Save Product**. Create Product opens onboarding in another tab;
**Reload Products** refreshes the selection afterward. Saving creates a new inert
plan revision; review that revision before approving it.

API clients can repair the same draft through
`POST /orchestration/flows/{flow_id}/draft/product`, with `product_id`,
`expected_plan_version` and `expected_plan_hash` from the current plan. This
operation preserves the graph, machine specifications, proposed execution bounds
and planned files. It requires a human plan approver with access to the Product
and refuses stale, approved or started drafts. It does not grant execution.
