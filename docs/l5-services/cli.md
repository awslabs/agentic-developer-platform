# Installed L5 Security CLI

The installed `adp service` command calls the same scoped Product service API as
other clients. Sign in with `adp login` and use an authorized Product and Project.
Choose the target explicitly on every command; the examples below use fictional
identifiers and are not deployment targets. The server authorizes every request:
capability discovery never grants access or substitutes for an API check.

## Configuration and setup

`adp service list`, `options`, `check`, `config show`, `config history`,
`config version`, `config update` and `config refresh` accept `--product` and
`--project` (and optional `--service security`, the default). `config version`
requires `--revision`. Updates and refreshes require `--input` (a UTF-8 JSON
object), `--idempotency-key` and `--if-match` containing the **entire** opaque
ETag, including its quotation marks. A first save uses `--if-none-match`
instead of `--if-match`. Copy the `etag` from `config show --json` unchanged,
including quotation marks; do not substitute a numeric revision for an ETag.

```sh
adp service options --product example-product --project example-project --json
adp service config show --product example-product --project example-project --json
adp service config update --product example-product --project example-project \
  --input security-settings.json --if-match '"server-issued-etag"' \
  --idempotency-key example-request-0001 --json
adp service check --product example-product --project example-project --json
```

`security-settings.json` is a local configuration matching
[ConfigurationInput](contracts/v1/README.md). Choose source references from
`options` rather than guessing resource IDs. The same input supports explicit
trigger targets and the monitoring policy. The CLI forwards the complete JSON
to Product without filling in inherited values, evaluating secrets, or polling
AWS; the API owns validation, inheritance and the stored revision. A stale
`--if-match` fails; read the current state before editing again. For binding
setup, `adp service binding create|link|check|update|suspend|resume|unlink|cleanup`
accepts `--input` with a matching `action` and `--idempotency-key`; changes to
an existing binding additionally require `--if-match`. Read with `binding list`,
`binding show --binding-id` or `binding status --operation-id`, and use
`binding reconcile --operation-id --input` for a previously uncertain operation.

## Execution and evidence

Supply a complete `RunInput` JSON object with a pinned configuration revision,
binding, capability and source references; the Product API validates it. Use a
stable key when retrying an uncertain submission. A wait reads only the returned
run ID, for at most 3,600 seconds; it does not submit another provider job.

```sh
adp service run --product example-product --project example-project \
  --input security-run.json --idempotency-key example-run-request-0001 --json
adp service status --product example-product --project example-project \
  --run-id example-run --json
adp service wait --product example-product --project example-project \
  --run-id example-run --wait-seconds 30 --json
adp service results --product example-product --project example-project --json
```

Use `runs` for run history, `result show --result-id` and
`findings --result-id` for evidence. `cancel`, `reconcile` and `retry` require
`--run-id`, a JSON `--input` reason and an idempotency key. `compare` accepts
`--input` containing baseline/candidate result IDs. `triage history` accepts
`--result-id` and `--finding-id`; `triage set` also needs a JSON `--input`
decision and the quoted `--if-match` ETag. `publication show|retry` accepts
`--publication-id`, with `retry` requiring a JSON reason and an idempotency key.
`artifact show --artifact-id` reads its access record;
`artifact download --artifact-id --output PATH` authenticates the download,
checks the server-issued SHA-256 digest and writes a private file. It refuses
an existing output path and removes a file if the digest check fails. Downloads
are limited to 64 MiB. The currently installed gateway must implement each
operation: a listed CLI command does not imply that an older server supports it.
Before changing service state, the CLI refreshes capability discovery and refuses
an absent operation or an older server without a supported discovery route. No
mutation is sent in either case. Discovery does not authorize a target: the
service API still enforces Product permissions on every request.

## Machine output

`--json` emits one object with `status`, `command`, `detail` and `next_action`,
plus the verbatim HTTP `etag` when present. Non-JSON output prints the same ETag
separately from the stored resource body. Configuration, binding and triage
responses without a required ETag fail rather than encouraging an unconditional edit.
Successful `detail` is the server's stored response, including revisions and
monitoring projections when present; errors carry an `error.code` and no stored
state in `detail`. Exit 0 means a confirmed successful response, 2 invalid CLI
input, 3 access denied, 4 a conflict or uncertain outcome, 5 unavailable,
6 a failed terminal run, and 7 a bounded wait timeout with its receipt.
Never treat a dropped mutation response as proof the mutation did not happen:
read the stored configuration or operation receipt before deciding whether to
repeat a request. Use the **same** idempotency key for a replay, not a new one.
