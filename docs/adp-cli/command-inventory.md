# CLI command inventory

Parser inventory for the reviewed CLI integration on 25 September 2026. Task commands are merged in #6074; capabilities/doctor and their manifest are merged in #5716. This source inventory does not assert publication or live acceptance. See [master coverage](master-coverage.md) for Epic targets and API boundaries.

Options are parser options, excluding `--help`; shell launchers pass tool arguments through. `adp --deployment NAME` applies before the command.

| Command | Positional arguments | Options |
|---|---|---|
| `adp admin bedrock connect` |  | `--account`, `--destination`, `--download`, `--dry-run`, `--json`, `--name`, `--org`, `--profile`, `--region`, `--resume`, `--team`, `--user`, `--yes` |
| `adp admin bedrock list` |  | `--json`, `--org` |
| `adp admin bedrock status` |  | `--json`, `--user` |
| `adp admin bedrock verify` | `destination` | `--dry-run`, `--json`, `--yes` |
| `adp admin github revalidate` |  | `--json` |
| `adp admin github setup` |  | `--app-name`, `--credentials-file`, `--credentials-stdin`, `--dry-run`, `--existing`, `--github-org`, `--json`, `--new`, `--org`, `--owner`, `--visibility`, `--yes` |
| `adp admin github status` |  | `--json` |
| `adp admin login` |  | `--credentials-file`, `--credentials-stdin`, `--json` |
| `adp admin setup` |  | `--dry-run`, `--json`, `--org`, `--yes` |
| `adp agent list` |  | `--admin`, `--cursor`, `--json`, `--max-pages`, `--page-size` |
| `adp agent chain` | `chain_id` | `--admin`, `--json` |
| `adp agent detail` |  | `--admin`, `--json`, `--run` |
| `adp agent status` |  | `--admin`, `--json`, `--run` |
| `adp agent ping` |  | `--json`, `--run` |
| `adp agent state` |  | `--json`, `--run` |
| `adp agent logs` |  | `--admin`, `--follow`, `--json`, `--last-event-id`, `--run`, `--timeout` |
| `adp agent wait` |  | `--admin`, `--interval`, `--json`, `--run`, `--timeout` |
| `adp agent abort` |  | `--command-id`, `--dry-run`, `--expected-generation`, `--json`, `--reason`, `--run`, `--yes` |
| `adp agent pause` |  | `--command-id`, `--dry-run`, `--expected-generation`, `--json`, `--reason`, `--run`, `--yes` |
| `adp agent resume` |  | `--command-id`, `--dry-run`, `--expected-generation`, `--json`, `--reason`, `--run`, `--yes` |
| `adp agent steer` |  | `--command-id`, `--dry-run`, `--expected-generation`, `--instruction`, `--json`, `--run`, `--yes` |
| `adp aws connect` |  | `--account`, `--download`, `--dry-run`, `--external-id-file`, `--external-id-stdin`, `--json`, `--name`, `--no-external-id`, `--profile`, `--region`, `--resume`, `--role-arn`, `--yes` |
| `adp aws disconnect` | `connection` | `--dry-run`, `--json`, `--yes` |
| `adp aws list` |  | `--json` |
| `adp aws verify` | `connection` | `--dry-run`, `--json`, `--yes` |
| `adp bedrock connect` |  | `--account`, `--destination`, `--download`, `--dry-run`, `--json`, `--name`, `--org`, `--profile`, `--region`, `--resume`, `--team`, `--user`, `--yes` |
| `adp bedrock list` |  | `--json`, `--org` |
| `adp bedrock status` |  | `--json`, `--user` |
| `adp bedrock verify` | `destination` | `--dry-run`, `--json`, `--yes` |
| `adp capabilities` |  | `--json`, `--operation`, `--refresh` |
| `adp claude` | `tool_args` |  |
| `adp claude setup` |  |  |
| `adp codex` | `tool_args` |  |
| `adp codex setup` |  |  |
| `adp daemon install` |  |  |
| `adp daemon uninstall` |  |  |
| `adp deployment add` | `name` | `--json`, `--url` |
| `adp deployment list` |  | `--json` |
| `adp deployment remove` | `name` | `--json` |
| `adp deployment use` | `name` | `--json` |
| `adp doctor` |  | `--checks`, `--json`, `--request-id` |
| `adp flow cost` | `flow_id` | `--json` |
| `adp flow create` |  | `--expect-plan-hash`, `--file`, `--json`, `--reason`, `--yes` |
| `adp flow decisions` | `flow_id` | `--json` |
| `adp flow draft preview` | `flow_id` | `--expect-plan-hash`, `--expect-plan-version`, `--file`, `--json`, `--reason` |
| `adp flow draft save` | `flow_id` | `--expect-plan-hash`, `--expect-plan-version`, `--expect-proposal-hash`, `--file`, `--json`, `--reason` |
| `adp flow gate approve` | `gate_id` | `--expect-plan-hash`, `--json`, `--reason`, `--yes` |
| `adp flow gate reject` | `gate_id` | `--expect-plan-hash`, `--json`, `--reason`, `--yes` |
| `adp flow list` |  | `--json`, `--limit`, `--needs-me`, `--offset`, `--status` |
| `adp flow plans` | `flow_id` | `--json` |
| `adp flow show` | `flow_id` | `--json` |
| `adp flow start` | `outcome` | `--answer`, `--expect-plan-hash`, `--issue`, `--json`, `--plan`, `--reason`, `--refine-only`, `--repo`, `--request-id`, `--resume`, `--yes` |
| `adp flow watch` | `flow_id` | `--json`, `--once` |
| `adp github connect` |  | `--dry-run`, `--json`, `--no-browser`, `--org`, `--repo`, `--yes` |
| `adp github status` |  | `--json`, `--org`, `--repo` |
| `adp import` |  | `--client-id`, `--gateway-url`, `--refresh-token`, `--region`, `--user-pool-id` |
| `adp kimi` | `tool_args` |  |
| `adp login` |  | `--gateway-url`, `--no-browser` |
| `adp logout` |  |  |
| `adp models catalog` |  | `--json`, `--persona`, `--service-principal` |
| `adp models explain` |  | `--json`, `--persona`, `--service-principal` |
| `adp models mappings list` |  | `--json`, `--service-principal` |
| `adp models mappings reset` |  | `--json`, `--persona`, `--service-principal`, `--yes` |
| `adp models mappings set` |  | `--dry-run`, `--json`, `--model`, `--persona`, `--service-principal`, `--yes` |
| `adp models service-principals list` |  | `--json` |
| `adp refresh` |  |  |
| `adp serve` |  | `--foreground`, `--port` |
| `adp status` |  | `--json` |
| `adp superplane account delete` | `account_id` | `--dry-run`, `--json`, `--yes` |
| `adp superplane account list` |  | `--json` |
| `adp superplane account onboard` |  | `--account-id`, `--credential-id`, `--dry-run`, `--json`, `--name`, `--provider`, `--yes` |
| `adp superplane aws-onboard register` |  | `--account-id`, `--credential-id`, `--dry-run`, `--json`, `--name`, `--yes` |
| `adp superplane cost` |  | `--end-date`, `--json`, `--org`, `--start-date`, `--workspace` |
| `adp superplane deploy create` |  | `--approval-id`, `--dry-run`, `--gpu-per-replica`, `--json`, `--max-model-len`, `--model`, `--name`, `--operation-id`, `--plan-revision`, `--precision`, `--profile-id`, `--replicas`, `--serving-framework`, `--tensor-parallel-size`, `--workspace`, `--yes` |
| `adp superplane deploy delete` |  | `--approval-id`, `--dry-run`, `--id`, `--json`, `--operation-id`, `--plan-revision`, `--workspace`, `--yes` |
| `adp superplane deploy list` |  | `--json`, `--workspace` |
| `adp superplane deploy preview` |  | `--dry-run`, `--gpu-per-replica`, `--json`, `--max-model-len`, `--model`, `--name`, `--operation-id`, `--plan-revision`, `--precision`, `--profile-id`, `--replicas`, `--request-approval`, `--serving-framework`, `--tensor-parallel-size`, `--workspace`, `--yes` |
| `adp superplane deploy teardown-preview` |  | `--dry-run`, `--id`, `--json`, `--operation-id`, `--plan-revision`, `--request-approval`, `--workspace`, `--yes` |
| `adp superplane events` |  | `--action`, `--end-time`, `--event-type`, `--json`, `--limit`, `--offset`, `--resource-type`, `--start-time`, `--user` |
| `adp superplane node` |  | `--json`, `--workspace` |
| `adp superplane onboarding` |  |  |
| `adp superplane onboarding adopt` |  | `--account`, `--approval-id`, `--budget-daily`, `--budget-gpus`, `--cluster`, `--dry-run`, `--isolation`, `--json`, `--name`, `--org`, `--plan-revision`, `--region`, `--yes` |
| `adp superplane onboarding approval decide` |  | `--approval-id`, `--dry-run`, `--json`, `--org`, `--result`, `--yes` |
| `adp superplane onboarding approval request` |  | `--account`, `--budget-daily`, `--budget-gpus`, `--cluster`, `--dry-run`, `--isolation`, `--json`, `--name`, `--org`, `--plan-revision`, `--region`, `--yes` |
| `adp superplane onboarding approval show` |  | `--approval-id`, `--json`, `--org` |
| `adp superplane onboarding capabilities` |  | `--json`, `--org` |
| `adp superplane onboarding connection bind` |  | `--credential-id`, `--dry-run`, `--json`, `--org`, `--provider`, `--workspace`, `--yes` |
| `adp superplane onboarding connection credentials` |  | `--json`, `--org` |
| `adp superplane onboarding connection revoke` |  | `--connection-id`, `--dry-run`, `--json`, `--org`, `--workspace`, `--yes` |
| `adp superplane onboarding connection show` |  | `--connection-id`, `--json`, `--org`, `--workspace` |
| `adp superplane onboarding connection validate` |  | `--connection-id`, `--dry-run`, `--json`, `--org`, `--workspace`, `--yes` |
| `adp superplane onboarding create` |  | `--account`, `--approval-id`, `--budget-daily`, `--budget-gpus`, `--dry-run`, `--isolation`, `--json`, `--name`, `--org`, `--plan-revision`, `--region`, `--yes` |
| `adp superplane onboarding lifecycle continue` |  | `--approval-id`, `--artifact-id`, `--dry-run`, `--json`, `--org`, `--plan-revision`, `--workspace`, `--yes` |
| `adp superplane onboarding lifecycle list` |  | `--json`, `--org`, `--workspace` |
| `adp superplane onboarding lifecycle plan` |  | `--artifact-id`, `--json`, `--org`, `--workspace` |
| `adp superplane onboarding lifecycle request-approval` |  | `--artifact-id`, `--dry-run`, `--json`, `--org`, `--plan-revision`, `--workspace`, `--yes` |
| `adp superplane onboarding operation list` |  | `--json`, `--org` |
| `adp superplane onboarding operation recover` |  | `--json`, `--key`, `--operation-id`, `--org` |
| `adp superplane onboarding operation show` |  | `--json`, `--key`, `--operation-id`, `--org` |
| `adp superplane onboarding plan` |  | `--account`, `--budget-daily`, `--budget-gpus`, `--cluster`, `--isolation`, `--json`, `--name`, `--org`, `--region` |
| `adp superplane onboarding readiness` |  | `--connection-id`, `--json`, `--org`, `--workspace` |
| `adp superplane org` |  |  |
| `adp superplane provider add` |  | `--dry-run`, `--json`, `--name`, `--provider`, `--recover`, `--stdin`, `--type`, `--yes` |
| `adp superplane provider delete` | `credential_id` | `--dry-run`, `--json`, `--yes` |
| `adp superplane provider list` |  | `--json` |
| `adp superplane quota set` |  | `--allowed-clouds`, `--dry-run`, `--json`, `--max-cost-per-day`, `--max-gpus`, `--max-nodes`, `--workspace`, `--yes` |
| `adp superplane quota show` |  | `--json`, `--workspace` |
| `adp superplane user` |  |  |
| `adp superplane workspace create` |  | `--account`, `--budget-daily`, `--budget-gpus`, `--dry-run`, `--isolation`, `--json`, `--name`, `--yes` |
| `adp superplane workspace describe` |  | `--json`, `--workspace` |
| `adp superplane workspace kubeconfig` |  | `--json`, `--workspace` |
| `adp superplane workspace list` |  | `--json` |
| `adp superplane workspace use` | `name` | `--json` |
| `adp task abort` | `task_id` | `--command-id`, `--credentials`, `--cursor`, `--cursor-file`, `--json`, `--max-events`, `--reason`, `--seconds`, `--timeout`, `--token-file`, `--wait`, `--yes` |
| `adp task monitor` | `task_id` | `--credentials`, `--cursor`, `--cursor-file`, `--json`, `--max-events`, `--seconds`, `--timeout`, `--token-file` |
| `adp task status` | `task_id` | `--credentials`, `--json`, `--seconds`, `--timeout`, `--token-file` |
| `adp task submit` | `request_file` | `--credentials`, `--cursor`, `--cursor-file`, `--json`, `--key`, `--max-events`, `--seconds`, `--timeout`, `--token-file`, `--wait` |
| `adp token` |  |  |
| `adp update` |  | `--rollback`, `--to` |
| `adp version` |  |  |

Usage and metadata commands added by #5628 (source implementation; live acceptance held):

| Command | Positional arguments | Options |
|---|---|---|
| `adp usage summary` |  | `--end`, `--json`, `--request-id`, `--run`, `--start` |
| `adp usage timeline` |  | `--end`, `--json`, `--request-id`, `--run`, `--start` |
| `adp usage models` |  | `--end`, `--json`, `--request-id`, `--run`, `--start` |
| `adp usage requests` |  | `--cursor`, `--end`, `--json`, `--max-pages`, `--page-size`, `--request-id`, `--run`, `--start` |
| `adp usage request` | `request_id` | `--cursor`, `--end`, `--json`, `--max-pages`, `--page-size`, `--run`, `--start` |
| `adp logs list` |  | `--cursor`, `--end`, `--json`, `--max-pages`, `--page-size`, `--request-id`, `--run`, `--start` |
| `adp logs show` |  | `--cursor`, `--end`, `--json`, `--max-pages`, `--page-size`, `--request-id`, `--run`, `--start` |
| `adp logs export` |  | `--cursor`, `--end`, `--format`, `--json`, `--max-pages`, `--page-size`, `--request-id`, `--run`, `--start` |
| `adp admin usage summary` |  | `--end`, `--json`, `--org`, `--request-id`, `--run`, `--start` |
| `adp admin usage users` |  | `--end`, `--json`, `--org`, `--request-id`, `--run`, `--start` |
| `adp admin usage departments` |  | `--end`, `--json`, `--org`, `--request-id`, `--run`, `--start` |
| `adp admin usage requests` |  | `--cursor`, `--end`, `--json`, `--max-pages`, `--org`, `--page-size`, `--request-id`, `--run`, `--start` |

Research #5639 adds 11 forms: findings list/show, sources, stats, proposal list/show/create/generate/approve/reject, and scan. Scan/generate currently return unavailable without dispatch; [research contracts](research.md) document remaining acceptance.

| Command | Positional arguments | Options |
|---|---|---|
| `adp superplane research findings list` |  | `--end`, `--json`, `--max-pages`, `--page`, `--page-size`, `--source`, `--start`, `--workspace` |
| `adp superplane research findings show` | `id` | `--json` |
| `adp superplane research proposal list` |  | `--end`, `--json`, `--max-pages`, `--page`, `--page-size`, `--start`, `--status`, `--workspace` |
| `adp superplane research proposal show` | `id` | `--json` |
| `adp superplane research proposal create` |  | `--dry-run`, `--json`, `--request-file`, `--request-id`, `--yes` |
| `adp superplane research proposal generate` |  | `--dry-run`, `--json`, `--request-file`, `--request-id`, `--yes` |
| `adp superplane research proposal approve` | `id` | `--dry-run`, `--expect-revision`, `--json`, `--yes` |
| `adp superplane research proposal reject` | `id` | `--dry-run`, `--expect-revision`, `--json`, `--reason`, `--yes` |
| `adp superplane research sources` |  | `--json` |
| `adp superplane research stats` |  | `--json` |
| `adp superplane research scan` |  | `--dry-run`, `--json`, `--request-file`, `--request-id`, `--yes` |

Tenant selection (#5622 source implementation; live acceptance held). Global `--tenant TENANT_ID` precedes the command; `ADP_TENANT` selects per terminal.

| Command | Positional arguments | Options |
|---|---|---|
| `adp tenant list` |  | `--json` |
| `adp tenant current` |  | `--json` |
| `adp tenant use` | `tenant` | `--dry-run`, `--json` |
Credential and identity additions ([usage](vault.md)); source implemented, live acceptance pending:

| Command | Positional arguments | Options |
|---|---|---|
| `adp credential list` |  | `--json`, `--scope` |
| `adp credential show` | `id` | `--json` |
| `adp credential add` |  | `--domain-app-id`, `--dry-run`, `--expires-at`, `--json`, `--label`, `--operation-id`, `--scope`, `--service`, `--strict`, `--type`, `--value-file`, `--value-stdin`, `--yes` |
| `adp credential update` | `id` | `--dry-run`, `--expected-revision`, `--expires-at`, `--json`, `--label`, `--strict`, `--yes` |
| `adp credential delete` | `id` | `--dry-run`, `--json`, `--yes` |
| `adp identity list` |  | `--json`, `--provider` |
| `adp identity link` |  | `--dry-run`, `--json`, `--provider`, `--provider-user-id`, `--resume`, `--yes` |
| `adp identity unlink` | `id` | `--dry-run`, `--json`, `--provider`, `--yes` |

GitHub maintenance (#5634) adds six leaf commands: `github disconnect`, `admin github rotate-key`, `admin github disconnect`, and `admin github org-binding list|add|remove`. `admin github status --maintenance` reads the revision needed for reviewed App writes. See [GitHub maintenance](github-maintenance.md) for exact flags, staged key recovery and live acceptance holds. The checked manifest is the source inventory; deployed availability and functioning OAuth/webhook consumers require separate evidence.

GitLab (#5635) adds `gitlab status|connect|disconnect` and `admin gitlab status|configure|revalidate`. The [GitLab human API contract](gitlab.md) records approved-host discovery, vault references, exact project/root admission, preserved external hooks, recovery and live acceptance holds. These are source command forms; deployment and real hosted delivery require separate evidence.

## Knowledge and indexing (#5632)

Source implementation; dedicated live indexing/retrieval acceptance remains held. See [knowledge](knowledge.md).

| Command | Positional arguments | Options |
|---|---|---|
| `adp knowledge add` |  | `--dry-run`, `--file`, `--json`, `--key`, `--yes` |
| `adp knowledge list` |  | `--json`, `--page`, `--page-size`, `--scope`, `--status`, `--type` |
| `adp knowledge show` | `asset_id` | `--json` |
| `adp knowledge delete` | `asset_id` | `--dry-run`, `--json`, `--yes` |
| `adp knowledge status` | `asset_id` | `--json` |
| `adp knowledge watch` | `asset_id` | `--interval`, `--json`, `--timeout` |
| `adp knowledge reindex` | `asset_id` | `--dry-run`, `--json`, `--key`, `--yes` |
| `adp knowledge bulk preview` |  | `--file`, `--json` |
| `adp knowledge bulk commit` |  | `--dry-run`, `--expect-hash`, `--json`, `--preview-id`, `--yes` |
| `adp admin indexing list` |  | `--json`, `--page`, `--page-size` |
| `adp admin indexing show` |  | `--json`, `--run` |

CLI-11 #5624 adds [machine identity lifecycle commands](machine-identities.md) and E31 to the existing nightly story reads. E31 reads explicit SQL IAM, IAM registry and Cognito metadata under the selected tenant; it does not read secrets or establish live mutation/retirement acceptance.

Access/session story #5625 adds seven forms: `access status|request`,
`admin access-request list|show|approve|deny`, and `admin session revoke-user`.
See [access and sessions](access-and-sessions.md) for exact review flags and the
limited gateway-token revocation effect. Nightly E33 covers safe reads.


Hierarchy administration: `adp admin org`, `department`, `team`, `member`, `team members`, and `tenant org-links`; see [exact flags, examples and revocation semantics](hierarchy.md). The checked manifest records all 25 leaf forms. Live lifecycle qualification remains pending.
Budget additions ([contract and examples](budgets.md)); source implemented, live enforcement acceptance pending:

| Command | Purpose |
|---|---|
| `adp budget me` | Existing own-budget envelope for one explicit period |
| `adp admin budget list` | Bounded managed cap pages |
| `adp admin budget show` | Exact tenant/target/ledger/period configuration and revision |
| `adp admin budget status` | Exact cap's spend/headroom; no ancestor or real-enforcement claim |
| `adp admin budget set` | Create-if-absent or revision-guarded update; preserves usage |
| `adp admin budget delete` | Revision-guarded removal of one period cap; preserves usage |

Rate-limit additions ([contract and examples](rate-limits.md)); live enforcement acceptance held:

| Command | Purpose |
|---|---|
| `adp ratelimit me` | Token-derived applicable limits, per-dimension source and backend availability |
| `adp admin ratelimit list` | Bounded organization-scoped configuration pages |
| `adp admin ratelimit show` | Saved configuration and revision for one verified target |
| `adp admin ratelimit status` | Defaults, storage scope and unknown worker convergence; TPM gap explicit |
| `adp admin ratelimit set` | Patch named dimensions or clear to default using a reviewed revision |
| `adp admin ratelimit delete` | Remove only the reviewed override, preserving usage and counters |

## Person limits (#5626)

See [authority and revision contract](person-budgets.md).

| Command | Options |
|---|---|
| `adp budget person-cap show` | `--json`, `--period` |
| `adp budget person-cap set` | `--amount-usd`, `--dry-run`, `--expected-revision`, `--json`, `--period`, `--yes` |
| `adp budget person-cap delete` | `--dry-run`, `--expected-revision`, `--json`, `--period`, `--yes` |
| `adp admin budget person-cap show` | `--json`, `--period`, `--person` |
| `adp admin budget person-cap set` | `--amount-usd`, `--dry-run`, `--expected-revision`, `--json`, `--period`, `--person`, `--yes` |
| `adp admin budget person-cap delete` | `--dry-run`, `--expected-revision`, `--json`, `--period`, `--person`, `--yes` |
| `adp admin budget person-default show` | `--json`, `--period`, `--scope` |
| `adp admin budget person-default set` | `--amount-usd`, `--dry-run`, `--expected-revision`, `--json`, `--period`, `--scope`, `--yes` |
| `adp admin budget person-default delete` | `--dry-run`, `--expected-revision`, `--json`, `--period`, `--scope`, `--yes` |
| `adp admin budget member-report` | `--json`, `--max-pages`, `--org`, `--page`, `--page-size`, `--period` |

## Model policy and costs (#5636)

See [policy and cost contract](model-policy.md).

| Command | Options |
|---|---|
| `adp admin models default show` | `--compatibility-class`, `--json` |
| `adp admin models default set` | `--compatibility-class`, `--dry-run`, `--expect-version`, `--json`, `--model`, `--operation-id`, `--reason`, `--yes` |
| `adp admin models posture show` | `--compatibility-class`, `--json` |
| `adp admin models posture set` | `--compatibility-class`, `--dry-run`, `--expect-version`, `--json`, `--operation-id`, `--posture`, `--reason`, `--yes` |
| `adp admin models posture rollback` | `--compatibility-class`, `--dry-run`, `--expect-version`, `--json`, `--operation-id`, `--reason`, `--to-version`, `--yes` |
| `adp models costs` | `--chain`, `--json`, `--persona`, `--service-principal` |

## Bedrock routing lifecycle (#5633)

Source implementation; live account-routing inference and restoration remain held. Both existing Bedrock entry points use the same helper; select/reset always target self. See [routing lifecycle](bedrock-routing.md).

| Command | Positional arguments | Options |
|---|---|---|
| `adp bedrock select` |  | `--connection`, `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--yes` |
| `adp bedrock reset` |  | `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--yes` |
| `adp bedrock mappings list` |  | `--json`, `--org`, `--page`, `--page-size`, `--scope`, `--target` |
| `adp bedrock mappings show` |  | `--json`, `--org`, `--scope`, `--target` |
| `adp bedrock mappings set` |  | `--destination`, `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--org`, `--scope`, `--target`, `--yes` |
| `adp bedrock mappings delete` |  | `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--org`, `--scope`, `--target`, `--yes` |
| `adp bedrock connection-link add` |  | `--connection`, `--destination`, `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--yes` |
| `adp bedrock connection-link remove` |  | `--connection`, `--destination`, `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--yes` |
| `adp admin bedrock select` |  | `--connection`, `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--yes` |
| `adp admin bedrock reset` |  | `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--yes` |
| `adp admin bedrock mappings list` |  | `--json`, `--org`, `--page`, `--page-size`, `--scope`, `--target` |
| `adp admin bedrock mappings show` |  | `--json`, `--org`, `--scope`, `--target` |
| `adp admin bedrock mappings set` |  | `--destination`, `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--org`, `--scope`, `--target`, `--yes` |
| `adp admin bedrock mappings delete` |  | `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--org`, `--scope`, `--target`, `--yes` |
| `adp admin bedrock connection-link add` |  | `--connection`, `--destination`, `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--yes` |
| `adp admin bedrock connection-link remove` |  | `--connection`, `--destination`, `--dry-run`, `--expect-revision`, `--json`, `--operation-id`, `--yes` |

### Engine node recovery (#5630)

`adp flow node resume NODE_ID --flow FLOW_ID --reason TEXT` and
`adp flow recover-pr FLOW_ID --node NODE_ID --request-file REQUEST.json` show
read-only previews by default. Both accept `--dry-run`, `--json`, and
`--yes --expect-revision REV --operation-id UUID`. The revision comes from the
preview. See [flow recovery](flow-recovery.md). Worker pause/resume remains under
`adp activity`; node recovery does not itself prove that a worker started.

## Platform lifecycle (#5641)

See [canonical platform facade](platform.md).

| Command | Options |
|---|---|
| `adp platform status` | `--environment`, `--json` |
| `adp platform plan` | `--environment`, `--json`, `--output`, `--profile`, `--region`, `--scope`, `--source-checkout`, `--source-revision` |
| `adp platform apply` | `--confirm-account`, `--expect-plan-hash`, `--json`, `--plan-file` |
| `adp platform resume` | `--confirm-account`, `--expect-plan-hash`, `--json`, `--plan-file`, `--state-file` |
| `adp platform teardown plan` | `--environment`, `--json`, `--output`, `--profile`, `--region`, `--source-checkout`, `--source-revision` |
| `adp platform teardown apply` | `--confirm-account`, `--expect-plan-hash`, `--json`, `--plan-file` |

Superplane lifecycle #5638 adds `workspace delete`, `provider-connection create|show|validate|rotate|revoke`, `cluster list --eligible-for workspace-sharing`, `deploy profiles --workspace`, and `events --workspace [--follow --after]`. Deployment create/preview accepts `--namespace` as an assertion. See [the lifecycle contract](superplane.md#lifecycle-review-and-scoped-events-5638) for required mutation flags and source-versus-live qualification.
| `adp chat status` |  | `--json` |
| `adp chat start` |  | `--dry-run`, `--json`, `--message-file`, `--persona`, `--request-id`, `--yes` |
| `adp chat resume` | `session_id` | `--answer-file`, `--dry-run`, `--json`, `--reply-to`, `--request-id`, `--yes` |
| `adp chat list` |  | `--json`, `--page`, `--page-size` |
| `adp chat show` |  | `--json`, `--session` |
| `adp chat watch` |  | `--interval`, `--json`, `--session`, `--task-id`, `--timeout` |
| `adp chat export` |  | `--json`, `--output`, `--session` |

Hosted coding (#5516) adds `adp agent trigger --repo --issue --persona
--snapshot-file --instructions-file --request-id [--dry-run|--yes] [--timeout]
[--json]`. Both Claude and Codex developer personas submit through the existing
Task API using the selected human login and standing repository enrollment.
`agent status|detail|state|ping|logs|wait|steer|abort` also accept canonical `tsk_`
handles; Task pause/resume are explicitly unavailable. See [hosted coding](hosted-coding.md)
for repository snapshots, deterministic patch results and E42 qualification limits.
