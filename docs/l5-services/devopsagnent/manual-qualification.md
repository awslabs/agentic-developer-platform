# DevOps qualification with human UI observations

DevOps native access starts in ADP and uses SSO into the DevOps application. It
does not require AWS Console automation. Keep both protected automation policies
disabled when qualifying through the human path.

The full release still requires the original two-project portability, all
capabilities, recovery, Context, artifact and cleanup evidence. A human observation
is one attributed fact, never a release pass. The driver keeps its original five
selectors and the independent final evaluator.

## Select the original people and request

The private, source-bound DevOps fixture accepts `manual_ui` with these fields:

- `mode`: `authenticated-human`.
- `preparer_id`: the existing authorized release owner.
- `observer_ids`: the two distinct real people already selected in `recovery.users`.
- `ui_observer_id`: one of those people, who performs the UI run submission.
- `ui_receipt`: the original, unique idempotency receipt for that UI submission.
- `timeout_seconds`: 1–900, within the original release/selector window.
- `poll_seconds`: 1–30.

Do not transport browser session files in this mode. A `private_clients` bundle
contains only `version`, `environment`, `cli_config` and `cli_tokens`. Retain the
existing recovery ingress fields, omitting `recovery.browser.storage_states`.
`journey_driver.provider_hostname` must be the selected native DevOps application's
hostname, not the AWS federation endpoint.

Existing setup operations, their request keys and expired authority are separate
from UI evidence. Continue an unstarted setup only through its supported exact-scope
consent continuation. The release fixture must retain its original request and
receipt. Human observations cannot grant setup authority or replace native setup.

## Complete the human steps during the original release

Use the [bounded human observation API](../human-observation-foundation.md) to prepare
challenges against the active original dispatch, current configuration and binding.
The actual Product owner endorses the exact snapshot when required. Fixture or
service credentials cannot prepare or record a human observation.

For `ui-cli-mcp-parity`, prepare a DevOps `run_submission` challenge for the selected
UI observer. In the Product's DevOps operations, expand **Release qualification
observation**, attach the flow/challenge and original UI receipt, enter the exact
fixture input and use **Submit DevOps run** once. Then use **Record that I submitted
this run in ADP**. This explicit human assertion must match the canonical request's
durable before/after receipts, returned run, binding and configuration. An API
effect alone is not a UI assertion.

For each of the two users, prepare a `native_ui` challenge under `two-user-sso`.
Each person signs into ADP using their own login, attaches their challenge, uses
**Open native DevOps UI**, follows **Continue with issued SSO launch**, and returns to record the hostname they actually saw
after SSO. The gateway independently verifies that challenge's native launch chain.
A generated link or provider session alone cannot establish arrival.

The release discovers and imports these exact same-run observations through its
workflow OIDC identity. It still reads each submitted task from the native provider
and verifies both users' independently attributed sessions. Missing, mismatched,
foreign or stale observations remain unresolved. Negative ingress tests use actual
Gateway HTTP calls, with independent provider-call readback before and after each
request; they never follow a native URL.

## Preserve work after an interruption

Read the existing challenge's effect and launch receipts. A retained before marker
with no successful after receipt is an uncertain result, and a repeated observed
submission is refused. Do not generate another UI receipt or repeat federation to
make a test green. Missing observations are reported as
`devops_human_evidence_pending`; complete the original bounded human task or
reconcile its history before requesting a new release attempt.
