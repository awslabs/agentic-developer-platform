# Optional connector deployment amendment

The owner clarified on 2026-10-27 that connector reuse is the key outcome, deployments must be able to switch it off, and its infrastructure must be separately optional. This amends the design merged in PR [#9075](https://github.com/aws-e/adp/pull/9075). The [canonical architecture §2.1](architecture.md#21-optional-deployment-and-connector-reuse) defines the flags, infrastructure ownership, API behavior and lifecycle; this supporting document records source evidence, story changes and verification. Proposed flags, modules and commands are implementation deliverables, not currently deployed features.

The existing POC still evaluates collection, lineage and results when the collection extension is enabled. Connector-only use is a mandatory independent deployment mode, not acceptance of the remaining POC criteria. Dashboard/SSO expansion remains outside this POC. Existing #7295 gates, V13 live deferral and S01 registry/fixture readiness remain unchanged.

## Inspected conventions

These references were inspected at ADP `93d25e8b38319a329d456ae8306957abc973f4f4`. They supplement, rather than regenerate, the original historical source inventories.

| Existing source | Finding and application |
|---|---|
| [Feature endpoint](https://github.com/aws-e/adp/blob/93d25e8b38319a329d456ae8306957abc973f4f4/modules/gateway/src/features/routes.py) | Optional add-ons use strict false-by-default parsing and deployment-wide `/features` flags. Add the two DevLake capabilities with this convention; the flag grants no user/source authority. |
| [Frontend feature client](https://github.com/aws-e/adp/blob/93d25e8b38319a329d456ae8306957abc973f4f4/modules/gateway/frontend/src/services/features.ts) | Several core features fail open while optional features default false. New connector controls must stay hidden/unavailable when optional fields are missing or feature discovery fails. |
| [CLI capability contract](https://github.com/aws-e/adp/blob/93d25e8b38319a329d456ae8306957abc973f4f4/modules/gateway/src/cli_capabilities/contract.py) | Supported, enabled, permitted and ready are distinct. Extend the strict flag set and operation contracts; missing service readiness is not enabled functionality or authorization. |
| [Gateway renderer](https://github.com/aws-e/adp/blob/93d25e8b38319a329d456ae8306957abc973f4f4/platform/scripts/deploy-all.sh), [gateway workflow](https://github.com/aws-e/adp/blob/93d25e8b38319a329d456ae8306957abc973f4f4/.github/workflows/gateway-deploy.yml) | Both deployment paths render feature configuration; test parity and safe absence instead of wiring a switch only in UI. |
| [Insights deploy script](https://github.com/aws-e/adp/blob/93d25e8b38319a329d456ae8306957abc973f4f4/modules/insights/deploy.sh), [workflow](https://github.com/aws-e/adp/blob/93d25e8b38319a329d456ae8306957abc973f4f4/.github/workflows/insights-deploy.yml) | Shared Insights has its own state and push-triggered deployment. Reuse the module convention, but use separate optional roots/state and explicit module activation so POC edits do not silently install DevLake. |
| [Insights workloads](https://github.com/aws-e/adp/blob/93d25e8b38319a329d456ae8306957abc973f4f4/modules/insights/platform/workloads.tf) | The shared namespace/quota is already owned. A connector deployment must not claim it or require its presence. Shared Insights prerequisites apply only to the selected collection extension. |

## Story amendments and preserved obligations

No story is added, renumbered or reparented. Native success dependencies remain; an operator's supported deployment mode is distinct from the order of engineering story acceptance. Existing 41 AC/SCN IDs remain; three new pairs bring coverage to 44 ACs and 44 scenarios across the existing 13 stories and 10 ECs.

| Story | Acceptance changes | Owned outcome |
|---|---|---|
| [S01 #8888](stories/s01.md) | Strengthen AC-01 / SCN-01 | Freeze master/collection switches, optional mode matrix, scope/owners, disable deadlines and resource boundaries; connector reuse is primary. |
| [S02 #8889](stories/s02.md) | Add AC-05 / SCN-05 | Separate infrastructure roots/state, explicit entry points, renderer parity, absent/connector-only/full modes, disable/re-enable and owned removal. Existing IAM, schema and finalizer criteria remain. |
| [S03 #8890](stories/s03.md) | Strengthen AC-03 / SCN-03 | Collection requires both flags plus current grants. No jobs when off; fence in-flight writes/exports during disable. |
| [S07 #8894](stories/s07.md) | Strengthen AC-02 / SCN-02 | Integrated mode/lifecycle proof alongside existing recovery, IAM and B1/B2 tests. |
| [S08 #8895](stories/s08.md) | Strengthen AC-03 / SCN-03 | Turning off is distinct from removal. Finalizer runs while off and proves retention/shared-resource preservation. |
| [S09 #8901](stories/s09.md) | Strengthen AC-01 / SCN-01 | Independent connector/worker artifacts and optional publication; no database/Insights startup in connector-only package. |
| [S10 #8902](stories/s10.md) | Add AC-04 / SCN-04 | Enforce feature-disabled behavior through gateway, direct Go and broker routes, with no provider work or late result admission. |
| [S11 #8903](stories/s11.md) | Add AC-04 / SCN-04 | UI/CLI hide/disable only the optional integration; preserve ordinary Profile/native tools and saved decisions. Demonstrate connector-only onboarding. |
| [S12 #8919](stories/s12.md) | Strengthen AC-03 / SCN-03 | Reject disabled-module contexts and revoke leases/generations during disable; re-enable does not revive stale authority. |

S04, S05, S06 and S13 retain their acceptance tables. Existing retained Insights reads remain governed by S13; switching off connector ingestion neither deletes accepted evidence nor widens its audience. Every issue points to the same design revision after publication. Historical original-body hashes and the earlier review-amendment record retain their original meaning.

## Verification procedure

Use the existing [determinism controls](verification.md#fixture-and-determinism-contract) and real deployment/UI/API/CLI entry points. All selectors below remain planned in their stories.

1. **Absent/off baseline:** render and exercise missing, false, empty and invalid flag values with no optional module outputs, endpoints, credentials or images configured. Observe successful ordinary ADP/Profile/native-tool use. Inspect plan/state/resources to prove no DevLake workloads, roles, DBs or repositories were created. A normal upgrade and a shared Insights deployment must not activate the add-on.
2. **Connector-only:** S02 first accepts a real bounded runtime/startup/gating harness without S10/S11; then S10/S11 supply the end-to-end provider/Profile proof. Enable connectors with collection false. Deploy only its dedicated module and use actual extracted code for two providers through ADP Profile. Independently trace provider results, inspect package/process configuration without DSN, and verify absence of collector Pods, cell DBs and evidence dependencies. Authentication and Product/Project/source permission checks remain required.
3. **Full POC:** opt into collection separately, using its dedicated state and the existing instance. Re-run the original cell IAM/isolation and complete POC criteria; selecting a mode never satisfies them automatically. Reject collection true with connectors false before provisioning or job submission.
4. **Bypass/transition:** call gateway, direct Go, broker and collection routes while disabled; authorized callers receive feature_disabled without provider traffic, jobs or imported mutations. Unauthenticated and foreign-scope behavior retains existing concealment. Pause a provider page and worker transaction, disable through the real deployment procedure, revoke/fence contexts, then release barriers. No late result is committed after disable completes. Verify actual replica flags and render parity; a mere parameter update is insufficient evidence.
5. **Recovery/removal:** preserve saved Profile choices, native connections, sealed evidence and current retained-read permissions while disabled. Re-enable with renewed authorization and compatible versions, not stale contexts or automatic backfill. Separately remove owned module resources through the finalizer while the flags remain off; inject an interruption and verify ledger resume, shared PostgreSQL/ADP/Insights preservation, holds and any remaining storage cost. Cleanup never depends on re-enabling collection.

Freeze expected resource inventories, source call counts, Profile snapshots and result digests before testing. Pin deployment/config/image revisions; use generation barriers and bounded completion checks. Keep observations of user capability, runtime readiness, permissions, retained state and ongoing costs separate. Existing core/module resources are independently read before and after each transition.
