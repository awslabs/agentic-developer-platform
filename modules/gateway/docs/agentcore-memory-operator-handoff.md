# Shared AgentCore memory: operator verification handoff

This is a repository test and live-verification handoff for #7210. It does not authorize deployment, select a target, or prove that an AgentCore memory exists. #7217 owns live acceptance; obtain its approved target, two distinct owner fixtures, region, strategy, budgets, cleanup scope and deployment revision privately before running service calls. Do not publish target identifiers, URLs, receipts containing private identities, or customer content here.

## Repository checks

From `modules/gateway`, after `uv sync --extra dev`, run the local mounted-route and emulator tests with **test-only** credentials (they must not target AWS):

```bash
AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=us-east-1 \
  uv run --no-sync pytest tests/agentauth/test_agentcore_capture_status.py \
  tests/agentauth/test_agentcore_management_route.py tests/agentauth/test_agentcore_model_route.py \
  tests/agentauth/test_agentcore_migration.py -q
```

These checks use local DynamoDB emulation and mocked AgentCore responses. Passing tests do **not** prove service propagation, cloud pricing or deployed isolation.

## Provisioning the memory as code (#7901)

The memory target is declared in `modules/gateway/infra/agentcore-memory.tf` and is
created by the ordinary gateway-infra apply (`platform/scripts/deploy-all.sh` Step 3,
fresh or `--update`). No GitHub Actions environment and no console steps are involved.

1. In `environments/<env>/modules/gateway.tfvars`, uncomment `agentcore_memory_enabled = true`
   (optionally `agentcore_memory_event_expiry_days`, 3-365, default 90, and
   `agentcore_memory_strategy_type`, `SEMANTIC` default or `USER_PREFERENCE`). The
   committed dev file leaves the switch off; flipping it is the operator's decision
   and #7217's target selection, not a repository default.
2. Apply. Terraform creates, only while the switch is on:
   - AgentCore memory `adp_<env>_gateway_user_memory` with one long-term strategy whose
     namespace template is `/adp/owners/{actorId}`. This matches
     `AgentCoreRecall.owner_namespace()` (`/adp/owners/<hmac>`) and the `actorId` every
     `CreateEvent` sends, so extracted records land where recall and management look.
   - A memory execution role (`bedrock-agentcore.amazonaws.com`, `bedrock:InvokeModel*` on
     foundation models) for extraction.
   - `aws_iam_role_policy` on the gateway service role with exactly the five data-plane
     actions the code calls (`CreateEvent`, `RetrieveMemoryRecords`, `ListMemoryRecords`,
     `GetMemoryRecord`, `DeleteMemoryRecord`), scoped to that memory's ARN.
   - SSM parameters: `/adp/<env>/gateway/agentcore-memory-id`, `.../agentcore-memory-strategy-id`,
     `.../agentcore-memory-region` (String) and `.../agentcore-namespace-key`
     (SecureString, `alias/adp-<env>-gateway-secrets`, 32 random bytes as hex from
     `random_bytes`; never printed by Terraform outputs).
3. Record `terraform output agentcore_memory_id` / `agentcore_memory_strategy_id` privately
   in #7217's evidence. Do not publish them here.

Turning the switch off destroys the memory and every extracted record; turning it on again
mints a new namespace key, so earlier records become unreachable by design. Never rotate the
key by hand for the same reason. `modules/gateway/tests/infra/test_agentcore_memory_iac.py`
pins the gating, the namespace template, the SecureString/KMS requirement and the exact
action set (derived from the code's `client.<method>(` call sites).

### Renderer wiring (done)

Both renderers (`platform/scripts/deploy-all.sh` Step 4 and
`.github/workflows/gateway-deploy.yml`) read the four parameters on every gateway
deploy and pass them to the pod. With the switch off all four parameters are absent,
every variable renders empty and `agentcore_recall()` reports memory disabled (not an
outage). A partial set would be an outage (`memory target unconfigured`), so each
renderer counts the four values and **refuses to render** when some but not all are
present, and also refuses a memory region that differs from the gateway's
`AWS_REGION`. Flip `agentcore_memory_enabled`, apply gateway infra (Step 3), then run
the gateway deploy (Step 4 / the workflow); nothing is edited by hand.

| Env var | Source parameter | Where it lands |
|---|---|---|
| `ADP_AGENTCORE_MEMORY_ID` | `/adp/<env>/gateway/agentcore-memory-id` (`aws ssm get-parameter`) | ConfigMap `bedrockgateway-config` (`modules/gateway/k8s/configmap.yaml`, `__ADP_AGENTCORE_MEMORY_ID__` placeholder rendered by deploy-all.sh Step 4 and gateway-deploy.yml) |
| `ADP_AGENTCORE_MEMORY_STRATEGY_ID` | `/adp/<env>/gateway/agentcore-memory-strategy-id` | ConfigMap, same way |
| `ADP_AGENTCORE_MEMORY_REGION` | `/adp/<env>/gateway/agentcore-memory-region` | ConfigMap, same way; must equal the pod's `AWS_REGION` or the gateway refuses recall (the renderers check this before applying) |
| `ADP_AGENTCORE_NAMESPACE_KEY` | `/adp/<env>/gateway/agentcore-namespace-key` read with `--with-decryption` | Secret `bedrockgateway-secrets` (`--from-literal=agentcore-namespace-key=...` in deploy-all.sh's `SECRET_APPLY_RESULT` block and gateway-deploy.yml's equivalent), projected in `deployment.yaml` via `secretKeyRef` with `optional: true`; never the ConfigMap, never an inline `value:`, never echoed by either renderer |

Static guards: `platform/scripts/tests/test-configmap-render-parity.sh` (every
ConfigMap placeholder rendered by both renderers),
`modules/gateway/tests/features/test_chief_of_staff_flag_parity.py` (same parameter,
same default, same guard in both), `modules/gateway/tests/infra/test_gateway_secret_references.py`
(the key arrives by `secretKeyRef` from both paths, never inline) and
`platform/scripts/tests/test_gateway_deployment_render.py` (runs both renderers'
read-and-guard blocks with stubbed SSM: full set renders, empty set renders empty,
partial set and foreign region fail, key never printed).

The renderer runs with the deployer's credentials, which can decrypt the SecureString
through the account-root statement on `aws_kms_key.secrets`. If a future change makes
the gateway read the key at runtime instead, the gateway service role would additionally
need `ssm:GetParameter` on that parameter ARN and `kms:Decrypt` on `aws_kms_key.secrets`;
the shipped IaC does not grant either, on purpose.

## Live observation, only after #7217 approval

All requests below require the authenticated gateway session owned by the test user; callers do not provide AgentCore memory ID, namespace, actor or strategy. Record the deployed revision, time window, selected private target and cleanup receipt in #7217's protected evidence. Use fixture text without personal data.

1. Run an eligible, consented turn through each supported assistant. Keep the gateway capture `receipt_id` from the completion response, not the assistant's prose. Query `GET /chat/sessions/{session_id}/agentcore-memory/captures/{receipt_id}`. `phase: captured` or `event_accepted` is pending; `service_event_id` ties accepted work to the service but is **not** extraction. Only `status: available`, `coverage: verified` and a scoped `record_id` establish readback from a matching service record. Repeat with bounded polling. Record `X-ADP-Memory-Coverage` on both model responses and check that tool calls, streaming and cancellation retain their ordinary protocol.
2. On an owned record obtained from `GET /chat/sessions/{session_id}/agentcore-memory`, submit a correction using its `revision`. Save the returned `receipt_id` and `service_event_id`; query `GET /chat/sessions/{session_id}/agentcore-memory/{record_id}/operations/{receipt_id}`. `service_record_verified` requires matching owner namespace, configured strategy, receipt marker, current epoch and a separate extracted record. `extraction_pending` does not prove service visibility. Assert the old inference no longer wins even while extraction is delayed.
3. Delete the owned record with its current `revision`. Query the same operation-status path with the deletion receipt. `deletion_requested` means the original is still readable; `target_absent` is **partial** readback of that ID, not proof that other extracted records, queued work, checkpoints or unseen Crew-local files were erased. Check gateway recall filtering, known service records, delayed ingestion and the separate managed-checkpoint policy. Keep cleanup pending until the authorized operator independently verifies every required target.
4. Repeat the read, correction, deletion and status requests as the second owner and after revocation. Require indistinguishable non-enumerating denial for foreign and missing receipts/records; do not accept a model-generated statement as the oracle. Inject service failure and verify `unavailable`, never an empty store or a completed write.

Measure added recall latency and gateway model quotes/usage against the same model and turn without recall. The quote includes injected text, but AgentCore-managed extraction and retrieval costs are **separate** and not priced by this gateway response. Report private service billing/usage measurements and any missing attribution as unknown, not zero. Restore any modified test records and disable new admission during rollback; preserve receipt, owner epoch and migration journal so stale events cannot silently reappear. Do not claim full erasure of unseen local files.
