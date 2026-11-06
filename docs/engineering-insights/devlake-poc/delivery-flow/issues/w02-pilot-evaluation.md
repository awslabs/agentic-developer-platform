> Part of [epic #8887](https://github.com/aws-e/adp/issues/8887). Wave 2 run-only evaluation. Run after the one coordinated release campaign has completed, including its resumable 48-hour soak. Do not implement anything here.

## Description

Evaluate the pilot phase of the [POC flow](../README.md). Follow its [verification contract](../verification.md). The implementation is merged, but [defect #9425](https://github.com/aws-e/adp/issues/9425) records the reproduced no-op CLI and incompatible workflow/evidence handoff. Fix and verify that handoff before running this evaluation. A missing runner, field inventory or required input blocks evaluation; it is not permission to implement it here. Code work starts after initial acceptance; S01 records readiness gaps. Named owners/delegation and private launch inputs must be verified before live operations. No work is dispatched by creating this issue.

The operation runs the product checker before success; the machine evaluator independently verifies its exact Actions run/attempt and `summary.json` artifact using the accepted predicates. No manual dispatch or report acceptance is required.

## Checks (all must pass — no judgment calls)

1. `python3 modules/insights/poc/devlake/verify_campaign.py --phase pilot --manifest "$DEVLAKE_POC_MANIFEST" --plan docs/engineering-insights/devlake-poc/delivery-flow/acceptance-mapping.json` exits 0, reports `PASS pilot`, and reports 41 passed required non-S08 ACs, zero failed/skipped/not-run. Source: the exact original AC/SCN rows in the acceptance map; all S01-S07 and S09-S13 Validation tables.
2. The same reader verifies one compatible source/workflow/image/schema/mapper/configuration manifest against independent deployment, provider, DB, stored-evidence and CI receipts. Generated fixtures cannot replace required real provider/API/CLI/UI observations. S01 launch approvals must resolve to attributed records accepted before the run; no new owner decision is requested.
3. The same reader reports default-off resource absence; connector-only Profile use without Insights/cell DB; explicit collection activation; renewable IAM-only DB auth after >15 minutes on every physical client; workload credential rotation; wrong TLS/schema/role denials. Source: S02 AC-01..05, S09 AC-01..03, S10 AC-01..04, S11 AC-01..04 and S12 AC-01..03.
4. The same reader reports normalized native oracles, immutable B1 replay across B2 edits/deletes, DML-only writers, sealed export and conflict quarantine, detailed requirement/test/artifact/deployment chain, independent work/cost totals and fresh serving-time revocation. Source: S03-S06 AC-01..03 and S13 AC-01..03.
5. The same reader reports at least 20 controlled updates over at least 48 hours, p95 freshness <=15 minutes, S01 resource/cost bounds met, all isolation/fault/upgrade/rollback checks passed and independently reachable failure/abort/expiry cleanup while off. Source: S07 AC-01..04. Retain timestamps, actual adverse outcomes and cleanup receipts.
6. Live API contract: the reader uses established adp-cred authentication to call the real Profile endpoints, checks every required frontend-interface field exists in the response and asserts fixture keys are a subset of live keys. Source: S11's backend schema, implemented TS interfaces and `modules/insights/tests/devlake_poc/profile-contract-fields.json`. S11/S07 derive, pin and publish the inventory automatically from reviewed interfaces, and compare it to live response keys. No manual issue edit or suite amendment gates execution. Missing inventory or fields fails the check.

The code repair requires an admitted, private campaign manifest and a separately
approved protected source-workflow binding for every required criterion and the
native cross-check. Bind only source jobs that actually probe live provider, DB,
Profile and cleanup behavior; a fixture job cannot establish this evidence.
Missing binding, source artifact, provenance or job success is a failed check,
not permission for this run-only evaluation to create a validation service.
The generic v2 delivery receipt is separate from the native `summary.json`
artifact and must also be present before operation completion.

## Defect protocol

Product checks run before the operations owner can finish, so normal failures stay in its bounded repair/CI/merge/retry loop. The native read-only evaluator only retries observation; it cannot resurrect a passed operation. A remaining provenance/authority or unsupported-scope failure is an explicit exception. If an evaluation check fails:
1. File a NEW five-section defect issue using the repository defect template, quoting the exact failing command, redacted actual output and owning story.
2. Attach it as a native sub-issue of #8887.
3. Under accepted execution authority, dispatch the developer persona on the DEFECT using `adp-trigger`; never dispatch implementation on this evaluation or the merged story.
4. Re-run this evaluation after the defect PR passes checks and its merge is verified. Redeploy only changed installed artifacts; reuse the coherent release for verification-only repairs.
5. Close this issue ONLY when every required check passes in one complete evaluation run. No skipped/not-run required check counts as passed.
6. Post the passing transcript before closing; keep private evidence in its protected store and link a sanitized report.

## Evaluation report

Lead with whether this phase is ready. Include passed, failed, skipped and not-run counts and missing required checks. For each failure name the affected user workflow, next owner/action and defect link. Include exact commands and sanitized transcript under technical details; preserve release/evidence provenance privately. The native machine reader records acceptance from the exact bound evidence. No routine human evaluation is required. Its read-only observation does not implement repairs or grant deployment authority.
