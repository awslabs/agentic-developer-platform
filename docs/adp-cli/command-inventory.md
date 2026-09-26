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
| `adp models catalog` |  | `--json`, `--persona` |
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

Budget additions ([contract and examples](budgets.md)); source implemented, live enforcement acceptance pending:

| Command | Purpose |
|---|---|
| `adp budget me` | Existing own-budget envelope for one explicit period |
| `adp admin budget list` | Bounded managed cap pages |
| `adp admin budget show` | Exact tenant/target/ledger/period configuration and revision |
| `adp admin budget status` | Exact cap's spend/headroom; no ancestor or real-enforcement claim |
| `adp admin budget set` | Create-if-absent or revision-guarded update; preserves usage |
| `adp admin budget delete` | Revision-guarded removal of one period cap; preserves usage |

Hosted coding (#5516) adds `adp agent trigger --repo --issue --persona
--snapshot-file --instructions-file --request-id [--dry-run|--yes] [--timeout]
[--json]`. Both Claude and Codex developer personas submit through the existing
Task API using the selected human login and standing repository enrollment.
`agent status|detail|state|ping|logs|wait|steer|abort` also accept canonical `tsk_`
handles; Task pause/resume are explicitly unavailable. See [hosted coding](hosted-coding.md)
for repository snapshots, deterministic patch results and E42 qualification limits.
