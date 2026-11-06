> Part of [epic #8887](https://github.com/aws-e/adp/issues/8887). Wave 3 run-only evaluation. Run after delegated S08 recommendation review and preaccepted normal finalization have completed; no new application deployment is required. Do not implement anything here.

## Description

Evaluate the closure phase of the [POC flow](../README.md). Follow its [verification contract](../verification.md). These commands are planned S07 deliverables, not installed tools today. A missing runner, field inventory or required input blocks evaluation; it is not permission to implement it here. Code work starts after initial acceptance; S01 records readiness gaps. Named owners/delegation and private launch inputs must be verified before live operations. No work is dispatched by creating this issue.

The operation runs the product checker before success; the machine evaluator independently verifies its exact Actions run/attempt and `summary.json` artifact using the accepted predicates. No manual dispatch or report acceptance is required.

## Checks (all must pass — no judgment calls)

1. `python3 modules/insights/poc/devlake/verify_campaign.py --phase closure --manifest "$DEVLAKE_POC_MANIFEST" --plan docs/engineering-insights/devlake-poc/delivery-flow/acceptance-mapping.json` exits 0, reports `PASS closure`, and reports all 44 ACs and ten ECs accounted for with all required checks passed and zero skipped/not-run. Source: S08 AC-01..03, EC-01..10 and retained Wave 2 receipt.
2. The same reader resolves independently reviewed recommendations for connector-code reuse, normalized-model reuse and full-runtime reuse against the launch-accepted rubric; each cites measured benefits/costs and gaps. Review is delegated at launch; business adoption is not authorized. Every gap/patch has its destination, owner and effort basis. Source: S08 AC-01/02. The evaluator does not choose the adoption outcome.
3. The same reader checks independent resource absence or authorized hold/expiry/cost against the protected ownership ledger; default-off does not substitute for removal. Shared PostgreSQL, ADP/Insights state, shared connections and other tenants are preserved. Retained evidence uses current authorization. Source: S08 AC-03 and S02/S07 finalizer receipts.
4. The same reader verifies interrupted cleanup resumed and early failure/abort/expiry cleanup did not require successful S07/S08. Original failures/not-run history remains visible; no missing cleanup authority is reported as success. Source: S08 AC-03 and the canonical independent finalizer contract.

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
