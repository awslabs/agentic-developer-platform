# GitLab CLI and human API

Source implementation for #5635. It supports administrator-selected, deployment-approved GitLab base URLs and operator-approved immutable project roots. It does not deploy GitLab, create OAuth consent, reuse workload credentials or send ADP tokens to a GitLab host.

## Existing integration inventory

- `src/auth/gitlab_sso.py`: authenticated `/auth/gitlab-sso` mints a one-minute JWT for the platform GitLab instance from SSM `/adp/<environment>/gitlab/url`; `/.well-known/jwks.json` publishes its verification key. Neither endpoint manages project connections.
- `frontend/src/services/gitlabSso.ts`: browser SSO entry point. Source-control infrastructure in `modules/source-control/gitlab/infra` supplies the URL, signing-key integration and separate test-project parameters.
- `webhook-ingress/lambda/gitlab/handler.py`: verifies GitLab delivery. Protected mode requires an instance/project-qualified webhook token registry and immutable numeric GitLab user ID. Shared legacy tokens cannot authenticate protected project roots.
- `src/agentauth/external_roots.py`: operator-owned `ADP_MODEL_ROOT_BINDINGS` authorizes exact ingress producer role, tenant, GitLab instance, numeric project ID, repository path and personas. A verified `gitlab` identity is keyed as `https://instance#NUMERIC_USER_ID`. This producer/worker authority is not exposed to CLI callers.
- There was no human project lifecycle API. The new `/gitlab` adapters use existing tenant settings, vault metadata and identity rows; no second credential store or schema migration is introduced.

Platform base URLs come from the SSO SSM parameter. External base URLs are admitted only when already present in the deployment's protected root registrations. Each base URL receives a stable provider ID and revision. CLI flags accept those IDs, never arbitrary URLs. External GitLab SSO remains unverified by this adapter.

## Commands

```sh
adp gitlab status --json
adp admin gitlab status --json
adp admin gitlab configure --provider PROVIDER_ID --expect-provider-revision PROVIDER_REVISION \
  --operation-id UUID --dry-run
# First configuration omits --expected-revision. Later writes require the revision from status.
adp credential add --service gitlab --label project-access --type api_key \
  --value-file ./private-pat --operation-id UUID --yes
adp gitlab status --repo group/project --credential CREDENTIAL_ID --json
adp gitlab connect --repo group/project --project-id 42 --credential CREDENTIAL_ID \
  --expect-provider-revision PROVIDER_REVISION --expected-revision TENANT_REVISION \
  --operation-id UUID --dry-run
adp admin gitlab revalidate --repo group/project --credential CREDENTIAL_ID --json
adp gitlab disconnect --repo group/project --project-id 42 \
  --expect-provider-revision PROVIDER_REVISION --expected-revision TENANT_REVISION \
  --operation-id UUID --dry-run
```

All six commands support `--json`. Replace `--dry-run` with `--yes` after reviewing a mutation. Previews read ADP metadata only; they do not resolve secret values or contact GitLab. Configuration requires platform administrator privileges. Connect and disconnect require the signed-in human and selected tenant. Status can run with no GitLab provisioned and reports an empty approved-provider list.

Use an owned user-scope GitLab vault credential. The referenced token needs provider API read access (`read_api` where supported); the corresponding human must have GitLab Maintainer access to the project. Shared bot/org/workload credentials are refused. The gateway calls only the approved base URL's `/api/v4/user` and `/api/v4/projects/<encoded-id-or-path>`, with redirects disabled. It verifies numeric identity, project ID, exact namespace/path and provider permission before binding. Tokens are never returned or saved by this helper. The verified identity provenance is `credential_verified`, not fabricated OAuth approval.

Connect reuses an operator-approved project/root and existing webhook. If exact instance/project/tenant/namespace admission is absent, the server refuses with an operator-approval action. An operator must provision the intended webhook and protected ingress registry using the existing deployment procedures; this CLI cannot grant producer roles or mint webhook authority. There is no duplicate webhook creation path. For a rename, the operator first updates the root registration for the same numeric project. The owner then connects that ID with its newly verified path and current tenant revision; admission refuses a stale managed path until this reconciliation completes.

Disconnect leaves an owned association tombstone that denies new protected model roots for that project. It retains the external project, hooks, credential and identity. Already-running work is not canceled; use Activity remote controls separately. Untouched legacy integrations are unchanged. Only the association owner can disconnect it. A provider change requires disconnecting active managed projects first; older tombstones remain effective.

## Human HTTP contract and recovery

| Method/path | Purpose |
|---|---|
| `GET /gitlab/status?repo=...&credential_id=...` | Own metadata; optional live project readback using an owned vault reference |
| `GET /gitlab/admin/status` | Approved providers and selected tenant configuration |
| `POST /gitlab/admin/configure` | Select an approved provider; UUID operation and provider/tenant revision expectations |
| `GET /gitlab/admin/revalidate?repo=...&credential_id=...` | Read provider project state; no mutation |
| `POST /gitlab/connect` | Verify and bind exact `project_id`, `repo`, `credential_id` |
| `POST /gitlab/disconnect` | Tombstone exact owned `project_id` and `repo` |

Mutation requests contain `operation_id`, `expected_provider_revision`, and `expected_revision` (null only for the initial tenant configuration). Tenant-row locking serializes local changes. Exact operation replay returns the stored receipt and current revision; changed payloads or stale expectations are refused. Up to 256 operation receipts are retained without pruning; reaching the bound requires operator archival. A lost acknowledgement reports pending/unknown and never auto-replays. Inspect status and reuse the same UUID and input; a superseded receipt remains pending rather than claiming the old state is current.

Status reports `sso`, `identity_linked`, `project_access`, `webhook_delivery` and `agent_runtime` separately. A successful project API read establishes project access only. Webhook delivery and runtime remain `unverified` until independent live evidence is collected; no ping or local association marks a hosted task successful.

E30 joins the existing `story-reads`/default nightly harness. It checks served-CLI discovery, truthful unverified delivery/runtime, and invalid project refusal without provider writes. Full #5635 acceptance remains held for dedicated-project consent/connect, denied scope and rename drift, real delivered webhook/run/artifact, disconnect and verified cleanup. No shared GitLab changes are required for E30.

Self-hosted installations beneath a canonical path are supported, for example
`https://example.test/gitlab`. Provider identity retains that path, and API calls
use `/gitlab/api/v4/...`. Trailing slashes normalize to one provider; dot segments,
encoded paths, repeated slashes, userinfo, query/fragment and control characters
are refused. Paths never replace the approved authority; redirects remain disabled.
