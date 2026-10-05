# Knowledge lifecycle diagnostic preparation

The pure helper `tests/e2e/cli_uplift/remote/knowledge_lifecycle_plan.py` prepares an exact owned personal document identity for story #5632. It also validates the NDJSON emitted by `knowledge watch --json`. The explicit `login,knowledge-lifecycle` D04 harness uses this plan for add preview, registration, status/watch, same-key reindex replay, a new correlated successful run, and terminal soft deletion. It is excluded from full/nightly. Live lifecycle coverage remains outstanding; no upload or indexing has been dispatched.

A caller supplies the stable evaluation ID, gateway, selected tenant, canonical owner, login user, and explicitly allowed source bucket. The generated plan fixes the source key, content digest, registration UUID and reindex UUID before any upload. Persist this plan outside the worker before continuing. The `DOCUMENT` constant is the exact small Markdown payload.

Before enabling execution:

1. Confirm the deployed document indexing path and its GraphRAG/LiteLLM cost controls. Registration and one reindex can each call a model; queue redelivery and provider retries may add calls. A task inference budget does not establish a limit on the internal ingestion path.
2. Confirm current bucket admission and verify the selected canonical owner through the served CLI. Use an independently isolated fixture session; preserve live login stores.
3. Upload only the exact key using conditional creation, after persisting intent. Retain returned ETag/version externally before registration. Do not grant the remote worker new S3 write permissions.
4. Exercise served CLI add preview, add with the stable registration key, status, bounded watch, terminal reindex with its stable key, exact reindex replay, and terminal watch. `watch_events` accepts NDJSON and retains a pending timeout as pending; only correlated successful run and stage evidence proves readiness.
5. Soft-delete only the exact registered asset after indexing is terminal. Verify its absence. Delete only the exact input object after checking its recorded ETag/version. Soft deletion retains indexing outputs and graph artifacts; it does not cancel ingestion. Preserve the input and recovery intent on pending or uncertain outcomes.

Registration generates a server asset ID. Its CLI key is a local receipt, so after complete worker receipt loss recovery must locate the exact owned source/scope; a display name is insufficient. The manifest must retain the server asset ID as soon as known. Do not automatically restart indexing after an uncertain response. There is no generic source-prefix cleanup or automatic full-instance recovery in this preparation.

## Required explicit fixture

`knowledge_lifecycle` requires the identity fields `login_user_id`, `canonical_user_id`, `tenant_id`, the exact admitted `bucket`, and all of:

- `owned_mutations_authorized`, `source_upload_verified`, `runtime_cost_verified`: true only after the caller verifies each condition.
- `max_attempts`: integer 2–6 bounding the two generations together, including deployed queue redelivery and provider retry behavior.
- `max_spend_usd`: positive and at most1; `verified_worst_case_usd`: nonnegative and no larger than that spend cap.
- `cost_evidence_sha256`: digest of retained current runtime proof establishing the above bounds. Terraform desired/state values alone do not qualify.
- `source_etag` and `source_version_id`: actual exact upload receipts (`"null"` for an unversioned object).

These are externally verified limits, not a billing enforcement mechanism in the script. With no verified fixture D04 remains unavailable. The caller manifest retains the source receipt and cost bounds before SSM; the worker refuses a changed plan. The caller must persist upload intent before S3 writes, and retain upload receipts before this dispatch.

The worker only removes the registered source after terminal status and a known mutation outcome. Pending indexing and uncertain reindex acknowledgements retain the source and input. Success reports `input_cleanup_ready`; the caller must perform the exact version/ETag input cleanup and record it separately. A successful D04 result proves registry cleanup, not input-object deletion or graph/output deletion.
