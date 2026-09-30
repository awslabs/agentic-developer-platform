# Populated usage exports and remote-control failure — 26 September 2026

[EC2 regression run 36222747391](https://github.com/aws-e/adp/actions/runs/36222747391) completed with **1 passed, 1 failed**. It resumed evaluation `adp-e2e-20260926-055844-1ef044` after the first attempt failed preflight without creating EC2 or a Task. Both attempts and the original reservation remain recorded.

E21 passed the served CLI usage summary, timeline, model, request and log views filtered to existing Task invocation `57e3ed64-0794-4df0-828f-565479f9ddac`. JSON export completed. CSV and NDJSON each read two one-record pages, observed two distinct records, exercised continuation and correctly reported an incomplete bounded export. Scope and every returned invocation ID matched the selected Task and verified aws-e membership. No new inference was generated for usage checks. This establishes populated serialization and pagination, not multi-entity accounting, late settlement or retention acceptance for #5628.

The existing nightly workflow now receives the verified three-ID `usage_tenant` fixture through `CLI_UPLIFT_NIGHTLY_FIXTURES_JSON`. It contains no credentials or fixed invocation ID and does not add inference or alter the schedule. The optional selected-run fixture remains available for deliberate bounded tests.

E42 observed Task `tsk_d2bee6e2-2e1f-45cd-8ef0-bcef6296c166` running, but its model request returned 409 at 06:11:32.239 UTC, before the first cancellation request at 06:11:33.410. Admission had pricing generation 157; the serving gateway resolved validated generation 160. Other model-binding fields matched. The Task failed before the canonical model-operation claim. There is no cancellation receipt and this is **not successful cancellation evidence**. PR #6342 addresses this pricing-refresh refusal while retaining model/policy binding and original Task budget checks.

Independent settlement reads at 06:20 UTC found zero model operations and one empty transcript turn, terminal failure, confirmed child exit, no required recovery and native queue acknowledgement. EC2 `i-00000000000000010` was independently confirmed terminated. The unused $0.50 hold was reconciled without removing the failed attempt or changing the qualification's $5 ceiling.

Artifact `10899926481` SHA-256: `d8032d8185a531fc4160acaea732167e86ef87756f1e1fe110499d58dce21246`. Downloaded ZIP and extracted report were compared byte-for-byte. Report SHA-256: `8167afcc9d76b299555a2f9e5e9e863b616da97c3520e073f91af8cabaeea8e1`.
