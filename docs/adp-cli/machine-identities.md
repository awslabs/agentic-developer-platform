# Machine identities

`adp admin service-account`, `adp admin agent`, and `adp admin service-principal`
manage distinct objects. Each command requires `--org` matching the selected human
tenant. Select it with the global `adp --tenant TENANT ...` option. Existing
`adp models` mapping commands continue to use the canonical principal ID.

| CLI identity | Existing authoritative contract | Credentials / attribution |
|---|---|---|
| `service-account --identity-type sql-iam` | Auth and admin service-account APIs share the same SQL `ServiceAccount` row. Guarded CLI adapter calls `ServiceAccountService`. | Caller-owned IAM role; no secret. Explicit department/team; no `default/default` placeholders. Canonical alias source `sa_registration`. |
| `agent --identity-type iam-registry` | `/admin/registry/agents`, DynamoDB `AgentRegistryService` | IAM role registry; no secret. Canonical alias source `agent_registry`; alias ID is the agent UUID. |
| `agent --identity-type cognito-client` | `/admin/agents`, Cognito app client plus DynamoDB `AgentService` metadata | Client secret delivered only to a new private file. Canonical alias source `cognito_m2m`; alias ID is the client ID. |
| `service-principal` | Existing `/service-principals` persona-model lifecycle APIs | Canonical identity for mapping, preferences and attribution; registration mints no credential. Adding/removing an alias never transfers another principal's history. |

No command creates a second identity to synchronize these APIs. Register the
required authentication object, then explicitly register or link its canonical
alias. Use exact IDs; names are never used to select a mutation target.

## Examples

```bash
adp admin service-account create --org ORG --identity-type sql-iam \
  --name nightly --department DEPT --team TEAM \
  --role-arn arn:aws:iam::123456789012:role/nightly \
  --operation-id UUID --dry-run
# Repeat the reviewed command with --yes instead of --dry-run.

adp admin agent register --org ORG --identity-type iam-registry \
  --operation-id UUID --spec-file registry.json --yes

mkdir -m 700 machine-secrets
adp admin agent register --org ORG --identity-type cognito-client \
  --operation-id UUID --spec-file cognito.json \
  --credential-file machine-secrets/client.json --yes

adp admin service-principal register --org ORG --name nightly \
  --alias-source agent_registry --alias-id AGENT_UUID --operation-id UUID --yes
adp admin service-principal show CANONICAL_ID --org ORG --json
adp admin service-principal alias-add CANONICAL_ID --org ORG \
  --alias-source cognito_m2m --alias-id CLIENT_ID --expected-revision SHA256 --yes
adp admin service-principal alias-remove CANONICAL_ID --org ORG \
  --alias-row-id ALIAS_ROW_ID --expected-revision SHA256 --yes
adp admin service-principal status CANONICAL_ID --org ORG \
  --status retired --expected-revision SHA256 --yes

adp admin agent deregister CLIENT_ID --org ORG --identity-type cognito-client \
  --operation-id UUID --expected-revision SHA256 --yes
```

All leaves support `--json`; mutations support `--dry-run` and `--yes`.
`show` and mutation dry-runs return the revision required by updates/removals.
Confirmation does not grant permission or approve a newer revision. Creation
requires an operation UUID; retain the same UUID and inputs on retry.

SQL account `list` accepts `--page` and `--page-size 1..100`. Agent `list` accepts
`--page-size 1..100` and `--cursor`; it returns one bounded page and `next_cursor`.
Principal `show` returns up to 1000 aliases and refuses larger snapshots instead
of silently omitting dependencies. No list/show path requests credentials.

`registry.json` contains `agent_name`, `role_arn`, and `owner`; optional fields are
`team_id`, `scope` (`shared`/`personal`), `description`, `budget_config_id`,
`allowed_models`, `image_uri`, `code_repo`, and `workflow_name`. Use an existing
budget rather than creating a second resource during registration. Updates accept
these metadata fields and `status`; IAM role rebinding is refused because it
changes the authentication identity. Register a distinct identity explicitly.

`cognito.json` contains `name`, with optional `team_id`, `department_id`,
`description`, and `scopes`. Updates accept the existing API's `name`, `team_id`,
`department_id`, `description`, and `status` fields. Updating Cognito metadata
status alone does **not** revoke tokens. Use `deregister` for real retirement.

SQL account updates use `--name`, `--department`, `--team`, and `--role-arn`.
The server validates the department/team relationship and IAM uniqueness through
the existing auth domain service.

## Recovery and retirement

Canonical and SQL registration receipts commit with their identity rows. Replays
resolve the original identity; a later retirement/deletion cannot remint it.
Provider registration commits a durable admission record before calling Cognito
or DynamoDB, and a completion receipt afterward. Concurrent duplicate requests
cannot call the provider twice. A lost client response can recover the completed
registration with the same UUID; Cognito credential recovery reads the same
client secret into a **new** private output file. It never rotates or remints it.
If the provider outcome was lost before the completion receipt, the operation
remains pending for operator reconciliation; it is never automatically replayed.

Cognito output requires an owned `0700` directory and a new `0600` file. Existing
files and symlinks are refused before registration. JSON/stdout contains metadata
and delivery status only. An interrupted write may leave an incomplete private
file; inspect it and use a new output filename for same-operation recovery.

IAM deregistration sets `disabled` through a conditional revision update. Future
Lambda authorizer checks refuse it; cached authorizations and already-running
work are not terminated. Cognito deregistration first claims a terminal metadata
tombstone using the reviewed revision and operation UUID, then deletes exactly
that Cognito client. It prevents future token minting. Existing JWTs expire
normally, and existing runs are not stopped. Interrupted retirement may resume
only with the same UUID; updates cannot revive its tombstone. Metadata, audit,
usage and canonical preferences remain available.

Canonical suspension/retirement and alias revocation affect canonical identity
resolution; they do not terminate existing runs. Canonical retirement is terminal.
SQL deletion prevents future SQL service-account resolution and retains the
registration receipt and audit/usage; it does not force termination of existing
tokens or runs. Errors or incomplete acknowledgements require reconciliation.

## Qualification

E31 extends the existing nightly CLI regression with explicit SQL IAM, IAM registry,
and Cognito metadata reads under the selected tenant. It does not read secrets or
mutate identities. Offline tests cover real request serializers, SQL lifecycle,
concurrent/lost-response provider admission, private credential delivery, and
retirement guards. Full CLI-11 live acceptance still requires disposable owned
identities, credential delivery, wrong-tenant/role cases and verified cleanup on
the reference EC2 client; source merge and E31 reads alone do not close those ACs.

D05's default owned canonical lifecycle covers same-operation registration,
canonical mapping identity, alias collision/foreign refusal, suspension and
terminal retirement. It does not create an authentication provider identity.
The opt-in `machine_lifecycle.cognito_lifecycle: true` extension creates one
uniquely named disposable platform Cognito client through the installed CLI.
It uses the existing `bedrockgw/invoke` scope but never mints a token or invokes
a model. The caller retains its exact name, registration operation and retirement
operation in the external recovery manifest before SSM dispatch.

The extension verifies dry-run, protected `0600` credential delivery within a
private temporary directory, same-operation replay into a second private file,
unchanged identity/secret, existing-file refusal, ordinary/foreign read refusal,
and authoritative retired metadata. Replaying the original registration after
retirement must not deliver credentials or change the retained identity.
Credentials and their hashes are excluded from public evidence and journals;
all credential files are removed with the temporary session directory. This
checks the retirement contract; it does not claim live OAuth refusal or revocation
of already-issued tokens.

Cleanup records the reviewed client revision before deregistering the exact
owned client with the original retirement operation, and verifies `retired`
metadata even if the acknowledgement is lost. An uncertain provider creation
without a completed receipt remains pending with its original name/operation for
operator reconciliation; no replacement identity is created. Name, scope, tenant,
or team drift stops cleanup for review. This scenario must pass on a fresh
disposable EC2 before it supplies live acceptance evidence.

SQL IAM and IAM registry lifecycle qualification still require an explicitly
owned disposable IAM role fixture; shared worker/dev-box roles cannot substitute.
Cognito-to-canonical alias/mapping/attribution integration and actual concurrent
provider delivery remain separate acceptance work. The extension does not close
all CLI-11 criteria.
