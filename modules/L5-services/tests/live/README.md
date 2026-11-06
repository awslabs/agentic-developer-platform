# Security live selectors

`.github/workflows/l5-security-live-acceptance.yml` is **manual and main-only**. It
runs exactly one selector per dispatch. Its repository environment must already
exist and restrict deployment to protected branches; the workflow checks that
rule before accessing a target. It does not create an environment, validation
service, or private fixture; assessment scenarios can create real paid provider jobs. Configure the existing `adp-deploy-dev` or
`adp-deploy-test` environment with:

- `ADP_L5_ENV_AUDIT_TOKEN`: a credential allowed to **read** that environment's
  protection rules. If the API denies this read, the workflow fails closed.
- `ADP_L5_SECURITY_LIVE_CONFIG_PATH`: absolute path to an owner-authorized,
  mode-0600 JSON fixture on the runner outside the checkout. The fixture uses
  the schema checked by `fixtures.py` and contains two distinct Products and
  tenants, separate client profiles, pinned sources and compatible release
  revisions, plus current attempt, time, and spend allocations.
- `ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON`: secret JSON object mapping the
  environment-variable *names* referenced in the fixture to their private
  values. Never pass credentials in workflow-dispatch inputs or commit them.
- `ADP_L5_SECURITY_PRIVATE_EVIDENCE_DIR`: path to an existing persistent,
  mode-0700 directory outside the checkout, available only to the operator.
  Every journal for the selected scenario must be fresh and inside this
  directory. The runner keeps test output in a mode-0600 file named for the
  Actions run and attempt. Do not publicly upload it.

The private fixture must also contain `authorization.source_sha` equal to the
reviewed main SHA dispatched, `authorization.scenarios` including the selected
scenario, and `authorization.expires_at` as a timezone-aware timestamp no more
than 24 hours ahead. Reissue it for each approval window. The runner refuses
reused or shared journals; reconcile uncertain native work against the private
record before retrying. The scenario's own fixture describes any additional
owner-approved provider, fault-boundary, Context, consumer, and independent
readback channels. Missing access is a failed prerequisite, not a reason to
create test infrastructure or reuse another account's credentials.

The environment-protection check is a dispatch safeguard, **not** proof that
API, CLI, UI, MCP, worker and provider revisions match the deployed release.
The qualification runner uses its current Product-authorized private client
session to read retained snapshots and request a Gateway-owned, read-only
native readback. Gateway verifies the selected binding, delegated connection,
account and role against the run before and after reading AWS; no AWS credentials
are handed to the runner. Do not automate an AWS management or native-agent
console. The portable release bundle can transport Product client sessions
without inventing source URLs or acquiring customer credentials in the runner.
Retain private evidence and publish only sanitized summaries and references.
An offline pass or a green workflow without independent readback cannot close
the issue's live acceptance criteria.
