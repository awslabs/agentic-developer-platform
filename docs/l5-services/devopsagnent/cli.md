# Installed DevOps CLI

The installed `adp service` command uses the shared Product/project service API.
Sign in with `adp login` first, then supply `--service devops`, `--product` and
`--project` on **every** command. Select a Product and project you are currently
authorized to use; the CLI does not choose an ADP project, AWS account, space or
binding for you. The names below are fictional examples, not deployment targets.
The server authorizes each request independently of CLI capability discovery.

## Configuration and binding setup (SCN-01)

Read choices and the current configuration before saving. The following reads
require an installed CLI, a signed-in session and access to the chosen scope:

```sh
adp service options --service devops --product example-product --project example-project --json
adp service config show --service devops --product example-product --project example-project --json
adp service check --service devops --product example-product --project example-project --json
adp service binding list --service devops --product example-product --project example-project --json
```

`options` returns choices authorized for the scope; `check` reports readiness
without enabling paid work. `config show` includes the configuration revision in
`detail` and a quoted, opaque `etag` outside `detail`. Copy that entire ETag,
including quotation marks, for a conditional edit. `config history` and
`config version --revision 1` inspect earlier revisions in the same scope. A
new configuration uses `--if-none-match` instead of `--if-match`; using both is
an error. A stale ETag does not overwrite the newer configuration.

For a save, first prepare `devops-settings.json` as a UTF-8 JSON object matching
the current [shared ConfigurationInput contract](../contracts/v1/README.md),
using selected source references from `options`, not real credentials. Replace
the example ETag with the value just returned by `config show`. The key must be
16–128 letters, digits, underscores or hyphens; retain it when reconciling an
uncertain response, rather than minting another key:

```sh
adp service config update --service devops --product example-product --project example-project \
  --input devops-settings.json --if-match '"server-issued-etag"' \
  --idempotency-key example-update-key-0001 --json
```

`config refresh` has the same scope, ETag and key requirements, but its `--input`
must contain the selected `profile_ref` required by the shared refresh contract.
It proposes inherited changes without widening authority or enabling automatic
execution. Read the returned `etag` and revision before a subsequent edit.

For a new binding, prepare a JSON file with an explicit `action` of `create` and
the selected configuration revision. `binding create|link` require that file
and an idempotency key. `binding check|update|suspend|resume|unlink|cleanup`
also require the current binding's quoted `--if-match` ETag and an `--input`
action matching the command. Use `binding show --binding-id` for the ETag,
`binding status --operation-id` for the durable setup receipt and
`binding reconcile --operation-id --input` for an uncertain original operation;
do not issue another create. `binding events --binding-id` lists scoped native
event receipts separately from submitted runs. The CLI forwards selection and
conditions; the API checks authority, ownership and native readback.

With `--json`, each response is one object containing `status`, `command`,
`detail` and `next_action`; responses with a resource ETag also have `etag`.
An error has an `error` object with `code` and `message` and no stored resource
in `detail`. The server's private error text is not printed. The isolated
[installed-CLI configuration scenario](../../../modules/gateway/tests/cli/test_devops_service.py)
checks two separate non-ADP scopes and independent readback of their revisions;
it is not live qualification of either target.

## Runs and results (SCN-02)

Prepare `devops-run.json` using a selected binding, pinned configuration
revision and a capability-specific input from the
[shared run contract](../contracts/v1/README.md). It must explicitly contain
`"service_id": "devops"`; a source reference must belong to the selected
project. All input files in this section are operator-prepared JSON objects,
not files installed with the CLI. Replace the fictional IDs and revision below
with **authorized readbacks** in the same Product/project:

```sh
adp service run --service devops --product example-product --project example-project \
  --input devops-run.json --idempotency-key example-run-key-0001 --json
adp service status --service devops --product example-product --project example-project \
  --run-id example-run --json
adp service wait --service devops --product example-product --project example-project \
  --run-id example-run --wait-seconds 30 --poll-seconds 5 --json
adp service results --service devops --product example-product --project example-project --json
```

Use the `run_id` returned by the first call for `status` and `wait`. `wait`
only polls that ID for the requested bounded period; a timeout or an interrupted
watch does **not** cancel the hosted run or submit a replacement. `runs` lists
scoped run history; `runs` and `results` accept `--limit` (1–100) and an opaque
server-issued `--cursor`. Results may include native IDs and `origin` so that
observed native work remains distinguishable from CLI-submitted work. Read
`result show --result-id`, `findings --result-id`, and
`binding events --binding-id` to inspect results and native event receipts.
`artifact show --artifact-id` reads authorized metadata;
`artifact download --artifact-id --output PATH` verifies the server's SHA-256 receipt, writes a mode-0600 file,
refuses an existing output and removes a failed download. The limit is 64 MiB.

`cancel --run-id` requires an explicit reason JSON file and idempotency key;
`reconcile --run-id` checks a previously uncertain operation with the same
conditions. DevOps `retry --run-id` is unavailable: do not use a new run as a
substitute for reconciliation. An interrupted submission might already have
created a run; read status or the durable setup receipt before deciding what
to do next. A repeated publication request is **not** a run retry.

## Prevention and publication

`prevention status --binding-id` reads the selected native prevention goal and
its observed readiness; it does not infer readiness from an empty trigger list.
For `prevention configure --binding-id`, prepare a JSON object with a selected
`configuration_revision` and `action` of `enable`, `pause` or `resume`. Supply
the binding's current quoted `--if-match` ETag and an idempotency key. The
following example is conditional on an operator-prepared `prevention.json`,
an authorized binding named `example-binding`, and its actual server ETag:

```sh
adp service prevention status --service devops --product example-product --project example-project \
  --binding-id example-binding --json
adp service prevention configure --service devops --product example-product --project example-project \
  --binding-id example-binding --input prevention.json --if-match '"server-issued-etag"' \
  --idempotency-key example-prevention-key-0001 --json
```

`publication show --publication-id` reads publication status. If Context
publication needs a retry, `publication retry --publication-id --input reason.json --idempotency-key KEY`
sends a reason JSON object to the publication
endpoint only; it never resubmits an assessment. The installed
[operation scenario](../../../modules/gateway/tests/cli/test_devops_service.py)
checks scoped IDs, bounded reads, verified artifact bytes and retry isolation
against an isolated API fixture. The current checked-out L5 router does not
expose the publication routes from the shared operation matrix: those commands
need that interface deployed before live use. This offline check is **not**
evidence that publication retry succeeds on a deployed gateway.

Native browser SSO launch is intentionally a first-party UI action. The CLI
can inspect binding and readiness policy but cannot request a browser ticket.

## Errors and exit codes (SCN-03)

Use `--json` for automation. Every response is a single JSON object; a confirmed
receipt has `status: "ok"` and server data in `detail`. A successful submission
acknowledges the **run receipt**, not successful completion of hosted work. An
error has `status: "failed"`, an `error` with stable `code` and a safe `message`,
and an empty `detail`. A bounded or interrupted watch has `status: "pending"`,
its last known run receipt (or run ID) and a `next_action`. Do not parse stderr
for remote diagnostics or treat a 202 admission as a completed assessment.

| Exit | Meaning | Safe next step |
|---|---|---|
| 0 | Confirmed command response | Inspect returned revision, ID or state. |
| 2 | Invalid CLI input or server validation | Fix flags or the local JSON file; nothing is accepted as a successful result. |
| 3 | Access denied | Check membership and permission in the selected scope; do not switch accounts implicitly. |
| 4 | Stale revision, conflict, pending dependency or unknown mutation outcome | Read back the exact resource or original operation; never retry an uncertain submission with a fresh key. |
| 5 | Unavailable or unsupported operation, absent resource or invalid response | Check capability and deployment readiness, access and the selected ID. |
| 6 | Watched run ended without complete success | Inspect its returned run receipt and results. |
| 7 | Watch timed out or was interrupted | Hosted work continues; read status for the same run ID. |

The server can refuse a foreign Product/project or source with `not_found`
without revealing whether it exists. A conflicting `--if-match` and
`--if-none-match` fails locally before contacting the API, including when the
ETag is empty. Mutation transport loss yields `unknown_mutation_outcome` and
does **not** trigger an automatic replay. Retain the original key, read back
the resource, and use the corresponding reconciliation operation if needed;
never infer that no native work started from a disconnected client. The
[installed error scenario](../../../modules/gateway/tests/cli/test_devops_service.py)
checks controlled faults, refusal and redaction; it is not live evidence.
