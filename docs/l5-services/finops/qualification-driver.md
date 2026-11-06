# FinOps Product qualification driver (not live acceptance)

## Current native-execution restriction

AWS management-console and native-agent-console browser automation is prohibited
following an operator-reported security ticket. Do not activate the native console
executor or initiate native qualification through that browser path, change its
browser/fingerprint/endpoints/credentials to bypass the restriction, or claim
browser-based native acceptance. The root coordinator owns the ticket review and
browser-dependency audit. A documented native inquiry API has not been qualified;
Cost Explorer reconciliation alone does not resolve that missing capability.
Offline mocked-browser tests, documented API integration and ADP-only UI checks
that never navigate to AWS consoles may continue. The release report records
this unresolved capability; the procedures below do not authorize activation.

`modules/L5-services/tests/live/finops/driver.py` submits **one** inquiry through the
existing scoped Product API and never repeats an uncertain submission. It reads
the original run, its native answer *artifact reference*, independent versioned
billing pages and publication receipts, then compares the same result as read by
the installed CLI, MCP and first-party browser. It does not call a provider API,
create a Product or agent, erase attempt history, or turn candidate policy
simulation into a qualified native answer. Owned cleanup cancels only an
acknowledged queued run; an uncertain outcome remains for read-only recovery.

Do not run against an AWS target until its owner has authorized the selected
Product and its dedicated agent/Billing View/data-role scope, the shared worker
and image have been installed and read back, and the private session policy and
console selectors have been qualified. A primary account-wide Billing View or
an account-wide operator role is **not** proof of Product attribution. The
Product result route reads the original answer text by its exact artifact version
and digest; the UI displays it beside independent billing. The evaluator rejects
CE-only, account-attributed and inconsistent observations. A successful offline
evaluation is `schema_only`, not native acceptance.

For existing-agent adoption, select `finops.agent_mode: adopt`, the explicit
`native_agent_ref`, `native_agent_display_name`, Billing View and data-role ARN
through Product configuration. The display name is a console-selection hint,
not proof: the private executor verifies the opened agent host and reads back its
label and data role using the protected `data_role_selector`. The native
application does not expose a Billing View field; adoption does not claim to
read one. The worker independently checks the selected Billing View's source
account, owner, healthy state and explicit account filter. It accepts the bounded
data-role policy shape that allows `ce:GetCostAndUsage` and
`ce:GetDimensionValues` only on that view and explicitly denies those actions
on other resources. IAM principal-policy simulation must also allow both reads
on the selected view, without missing context or failed boundary/organization
decisions. Denied, broader or inconclusive authority remains unqualified; this
is not a general IAM policy evaluator. The worker archives these observations
in a versioned snapshot before marking the binding ready. Account-level scope
checks do not establish whole-Product attribution or installed native acceptance.
After changing configuration, repeat the
read-only link operation at the new revision before admission. Creation remains
unsupported; adoption neither creates an agent nor submits a paid inquiry.

The private manifest is an existing mode-0600 JSON file outside the checkout;
the evidence directory already exists outside the checkout with mode 0700.
It supplies `authorized: true`, an authorization `expires_at` within 24 hours,
`source_sha` (or legacy `release_sha`; conflicting pins are rejected),
`gateway_url`, `frontend_url`, exact `scope` (tenant, Product,
project), `binding_id`, `configuration_revision`, versioned
`native_agent_ref`, `native_agent_display_name` verified against the current
binding readback, selected `billing_scope`, typed `input`, distinct
`receipt` and `cleanup_receipt`, and bounded `max_requests` (1–50) and
`deadline_seconds` (1–600). It also supplies `api_token_env`,
`readback_token_env` and `mcp_token_env` naming private environment variables.
The same authorized actor token may be used across surfaces; independence comes
from fresh readbacks, not different token values. It also supplies a reviewed `cli_executable`, `cli_sha256`,
`cli_home`, `cli_deployment` and `browser_storage_state`. The private manifest
uses environment references rather than token values in this local-file form.
The portable protected-secret form below transports actual session files instead.
Confirm the deployed release independently;
`release_sha` is a pin, not evidence of installation.

The shared release entrypoints are `scripts/qualify-finops-release.py
preflight|run SOURCE_SHA FIXTURE EVIDENCE_DIR` and
`scripts/emit-finops-qualification.py SOURCE_SHA FIXTURE EVIDENCE_DIR`, relative
to the L5 module. Uppercase arguments are private configured values. Preflight
is read-only and checks installation/access prerequisites, not binding readiness;
emission re-reads the original owned run and never submits again.
Gateway versioned-answer permission belongs to the operator bootstrap root, not
the service release role or the legacy Gateway artifact-bucket toggle.

The emitter writes mode-0600 `PRIVATE_EVIDENCE_DIR/qualification.json` only after
fresh authoritative readbacks match the retained observation. The shared
`release_identity` contract binds the report to the dispatched workflow, source
and definition revisions, repository, run/attempt, environment and engine
correlation; `L5_FIXTURE_DIGEST` must match the selected private fixture. Existing
reports, including symlinks, are never overwritten. The report contains hashes,
not answer text or session tokens. Shared evidence conversion and upload remain
the root-owned adapter's responsibility; this emitter does not produce delivery
evidence or publish anything.

The maintained F1 story catalog supplies requirement, scenario and manual
live-check IDs. Selected-run readbacks are marked verified at the `schema-only`
layer, never `independent-live`. Criteria not evaluated by those readbacks remain
explicitly incomplete and unresolved; `native_ready` stays false and installed
Gateway revision remains unknown. Cleanup records only the observed retention of
the original terminal run, not deletion of provider resources or completion of
the broader cleanup/reuse acceptance scenario.

From the L5 module with test dependencies installed, the offline check is
`./.venv/bin/pytest -q tests/live/finops/test_driver.py`. It mocks the
HTTP, subprocess and browser boundaries; it makes no paid provider calls.
After the prerequisites above and separate target authorization, invoke
`./.venv/bin/python tests/live/finops/driver.py --manifest /private/finops.json --evidence-dir /private/finops-evidence`.
These paths are placeholders, not installed locations. Do not place the journal
or any response in public documentation or a PR. If a request is uncertain,
retain the private journal and reconcile the original receipt; never rerun the
same manifest or issue a second paid inquiry automatically.

## First installation and portable private sessions

The shared workflow installs paused consumers, materializes the selected secret,
runs browser and access preflight, resumes the installed compatible consumer,
then invokes `run` and the emitter. The driver must not require a ready binding
before that resume. Access preflight checks the installed CLI's selected registry
and calls `GET .../services/finops/configuration/options` with the submission and
readback sessions. It requires the FinOps capability and authorized connection
options. Initial `native_unqualified`, `billing_scope_unverified` and
`region_selection_required` gaps do not pretend to be native readiness. Missing
access, a wrong connection/region or other unavailable prerequisites fail closed.
No Product writes or native inquiry occur in preflight.

For an empty installation, omit `binding_id` and `configuration_revision` both
at the manifest root and in `input`. Keep the other inquiry fields unchanged.
Add this `bootstrap` object:

- `configuration`: the complete normal `saveConfiguration` settings payload,
  with `service_id: finops`, `enabled: true`, `capabilities: [cost_inquiry]`,
  the authorized versioned `connection_ref`, selected `region`, explicit
  `overrides`, approved `limits`, and `finops` settings. Those settings select
  `agent_mode: adopt`, the native agent ID, its display label, Billing View/
  account scope and data-role ARN. They must match the manifest's native and
  billing selection. Start without triggers. Preserve the accepted $300 per
  invocation and concurrency 5; the existing $2,000 flow budget authority must
  already exist. The driver does not create budget policy or provider resources.
- `trigger_id`: the name of the single manual inquiry trigger to enable after
  adoption.
- `receipts`: four distinct 16–128-character idempotency keys, named `configure`,
  `link`, `trigger` and `readback`. They must also differ from the inquiry and
  cleanup receipts. Never reuse them for different input.

After resume, `run` performs the following normal Product operations, using
`Authorization: Bearer` from the protected Product session. Here `BASE` means
`/api/v1/products/{product_id}/projects/{project_id}/services/finops`:

| Operation | Request and concurrency control | Required observation |
|---|---|---|
| Create configuration | `PUT BASE/configuration`, body `bootstrap.configuration`, `If-None-Match: *`, key `configure` | HTTP 201 and the authoritative configuration revision; an existing configuration is not overwritten |
| Adopt existing native agent | `POST BASE/binding-operations`, body `{action: link, configuration_revision: <returned revision>, native_resource_ref: <manifest reference>}`, key `link` | HTTP 202 and original `operation_id`; poll `GET BASE/binding-operations/{operation_id}` until succeeded |
| Enable the manual trigger | `GET BASE/configuration`, then `PUT` with the same selected settings plus one manual `cost_inquiry` trigger using the returned `binding_id` and inquiry sources; `If-Match: "<current hash>"`, key `trigger` | HTTP 200 and the new configuration revision |
| Refresh native binding at that revision | Repeat the link request at the new revision with key `readback` | Poll only this acknowledged operation; the binding ID must remain unchanged |
| Admit the inquiry | Fresh configuration/binding verification, then the existing `POST BASE/runs` | Ready, matching scope/agent/view and an acknowledged run under the original inquiry receipt |

Equivalent installed CLI setup verbs are `adp --deployment NAME service config
update --service finops --product PRODUCT --project PROJECT --input SETTINGS
--if-none-match --idempotency-key KEY --json`, followed by `service binding link`
with the normal link input file and `service binding status --operation-id ID`.
For the trigger update use `config update --if-match '"HASH"'` instead of
`--if-none-match`; then link at the returned revision. These are operator
alternatives to the automated bootstrap, not additional calls to run alongside
it. Keep their JSON output private. Use the maintained CLI's adjacent
`adp_deployments.py resolve --deployment NAME --format json` helper for local
registry inspection; `adp deployment resolve` is not a supported front-door verb.

Setup records each mutation's receipt before dispatch and its acknowledged
operation ID afterward in a separate protected setup journal. Waiting polls the
same operation; it does not submit another link or native inquiry. A lost
acknowledgment, failed operation or expired bound stops the attempt and retains
the journal for the coordinator to reconcile through Product authority. An
unfinished setup journal cannot be replayed by rerunning the driver. Successful
setup writes a protected `finops-<receipt>-selection.json` containing only the
API-observed binding/revision and an authorization digest. Run and emitter use
that selection; it is not native acceptance evidence. A completed or uncertain
inquiry journal still prevents a second native submission.

### Portable client transport

The coordinator supplies `ADP_L5_FINOPS_LIVE_CONFIG_JSON` as protected JSON pinned
to `source_sha`. Besides the authorization, selection, inquiry and optional
bootstrap fields above, set `cli_deployment` to a named deployment and provide
`clients` with exactly these fields:

| Field | Transported value |
|---|---|
| `api_tokens` | Object with `api_token_env`, `readback_token_env`, `mcp_token_env` keys whose values are current authorized Product bearer tokens, not environment-variable names |
| `cli_registry` | Actual supported CLI `deployments.json`, restricted to the selected named deployment; preserve its stable `d` plus 16-hex-digit storage ID and exact gateway URL ending `/api`; no legacy record |
| `cli_config` | That deployment's existing `config.json`, including its exact matching gateway URL and any maintained login configuration |
| `cli_tokens` | That deployment's actual maintained `tokens.json`, including current access/ID tokens, expiry and refresh information as issued by the supported login flow |
| `browser_storage_state` | Playwright storage-state JSON from an authorized first-party UI session for the selected frontend |

These are transports of real authorized sessions, not permission to invent
tokens, inject binding readiness or use a worker lease as a user session. The
same authorized actor may serve multiple surfaces; independence still requires
fresh API/CLI/MCP/UI observations. Expired or unauthorized sessions fail normal
Product access. The release coordinator owns obtaining and refreshing sessions
through maintained login/session authorities before dispatch.

No developer-local absolute paths or extra token environment variables are
needed in this form. The normal release entrypoint materializes the client
bundle beside the private fixture, with mode-0700 directories and mode-0600
session files. It installs the maintained CLI files from the selected checkout,
pins the front door's digest, and uses its actual deployment resolver. Existing
materialized sessions are not overwritten, so CLI token refresh is retained.
The normalized in-memory manifest drops inline sessions and refers to private
files. Only paths are exported by shared dispatch; tokens, browser state and
session files must never be printed or uploaded with qualification evidence.

Client subprocesses retain system `HOME`. `ADP_HOME` selects the private CLI
registry (`deployments.json`); `BG_CONFIG_DIR` selects the named deployment's
session store, and `ADP_LEGACY_CONFIG_DIR` isolates legacy fallback. Other
deployment selectors, provider credentials and unrelated secrets are not
inherited. The workflow's absolute `PLAYWRIGHT_BROWSERS_PATH` remains shared
between installation, smoke and UI readback. The coordinator owns protected
fixture delivery and the shared resume sequencing; this driver owns the
service-specific bootstrap and observations.

Full evaluation remains mandatory after setup: genuine native answer identity,
independent CE reconciliation, fresh UI/CLI/MCP readback and verified publication
receipts. Missing Product Context or Engineering Insights integration is a
downstream evaluation prerequisite, not a native-provider failure or completed
reuse. Structural preflight, successful setup and offline tests never establish
installed native acceptance.

Provider-free regressions run through `python -m pytest
modules/L5-services/tests/deployment/test_finops_driver_offline.py -q` and are
included in the existing L5 module CI. They exercise empty-install ordering,
uncertain setup, session isolation and the installed CLI resolver; external
Product/provider responses remain mocked.
