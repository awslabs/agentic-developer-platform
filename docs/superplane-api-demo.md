# Repeatable Superplane API demo

`modules/domain-apps/superplane/scripts/api_demo.py` drives the public authenticated
API through workspace preview, approval, creation, native lifecycle continuations,
active workspace access, recovery and approved retirement. It uses Python 3.10+
and its standard library on Linux/macOS. Run commands from the repository root.

The script targets the native API contracts inspected in Gateway candidate
`1f759b201f3627e84656472b3e41befac26ac7d1`. The application runtime and provider
configuration must already be installed and activated. It does not deploy the
runtime, enroll providers, create identities or change grants. `run` checks the
API's capabilities and stops if creation is unavailable.

## Authentication and initialization

Use a genuine ADP human **access token**, stored in a private file containing only
the raw token. Obtain it through your existing sign-in process. Do not use an ID
token, put the token in command arguments, or commit it. An authorized requester can approve their own plan under the default streamlined
policy. Installations opting into strict separation require another current human
with the server-side approval grant.
The script sends bearer tokens only over HTTPS and refuses redirects.

```bash
export SUPERPLANE_TOKEN_FILE="$HOME/.config/adp/requester-access-token"
chmod 600 "$SUPERPLANE_TOKEN_FILE"
export SUPERPLANE_API_URL='https://YOUR_ADP_HOST/api/superplane/v1'
export SUPERPLANE_DEMO_STATE="$HOME/.local/state/adp/superplane-demo-01"
demo_script=modules/domain-apps/superplane/scripts/api_demo.py
```

Create a `workspace-request.json` with the intended workspace configuration. For
example, replace the account selection below with your authorized configured
account. Mode, region and budgets must be supported by the installed policy.
Include any desired `budget_max_daily_usd`/`budget_max_gpus` limits in the request.

```json
{
  "name": "api-demo-01",
  "mode": "managed",
  "region": "us-east-1",
  "account": "REPLACE_WITH_AUTHORIZED_ACCOUNT_ID",
  "isolation_mode": "dedicated"
}
```

```bash
python3 "$demo_script" --state-dir "$SUPERPLANE_DEMO_STATE" \
  --base-url "$SUPERPLANE_API_URL" init --request workspace-request.json

python3 "$demo_script" --state-dir "$SUPERPLANE_DEMO_STATE" run --timeout 600
```

`init` authenticates and saves a newly generated request UUID before any
provisioning. This UUID is a client idempotency key; it is not evidence that an
operation was admitted. The real workspace and server operation IDs are recorded
only from API responses. `run` previews the plan and requests approval for its
exact parameters. It then exits with the approval ID and plan digest for review.

## Review and resume

In the approver's own authenticated session, use their private access-token file
and the same API URL. They can run this script without the requester's state
directory. Review the ticket's request, account, region, cost envelope and saved
plan references before deciding. The requester can also inspect the full preview
in the private state directory or with `status`.

```bash
export SUPERPLANE_TOKEN_FILE="$HOME/.config/adp/approver-access-token"
chmod 600 "$SUPERPLANE_TOKEN_FILE"

python3 "$demo_script" --base-url "$SUPERPLANE_API_URL" \
  review --approval-id APPROVAL_ID

python3 "$demo_script" --base-url "$SUPERPLANE_API_URL" \
  decide --approval-id APPROVAL_ID --plan-digest REVIEWED_PLAN_DIGEST \
  --decision allowed-once
```

Use `--decision rejected` to reject instead. The decision command checks the exact
digest and the server's `can_decide`; the server independently enforces identity,
membership, approval eligibility and expiry. By default the script waits for a decision. For an unattended authorized E2E,
`run --approve-own-plans` explicitly decides each saved stage plan using the
requester's own session only when the server returns `can_decide: true` and
`requires_distinct_approver: false`. The flag cannot override strict policy or
insufficient permissions. The script never provisions an approver.

Back in the requester's session, resume using the **same state directory**:

```bash
python3 "$demo_script" --state-dir "$SUPERPLANE_DEMO_STATE" run --timeout 600
```

Native provisioning can produce several subsequent plans, including the actual
infrastructure apply plan. Each gets a separate preview and approval ticket.
Repeat review/decision and resume until `run` reports an active workspace with
verified API re-entry, operation recovery and broker access.

Approval tickets currently expire after 15 minutes. The inspected API returns
the existing ticket for an identical request and has no renewal endpoint. An
expired/revoked ticket or changed plan stops execution; retain the state and
resolve the request with an operator. The script does not replace request IDs to
evade these checks.

## Recovery and verification

```bash
python3 "$demo_script" --state-dir "$SUPERPLANE_DEMO_STATE" status
python3 "$demo_script" --state-dir "$SUPERPLANE_DEMO_STATE" run --timeout 600
```

Refresh expired requester credentials through sign-in and replace the token file,
then rerun as the same requester. Every process checks the saved deployment,
organization and requester scope. A directory lock prevents concurrent runners.
Request bodies are written atomically before submission. If an admission response
is lost, the next run replays exactly that request, allowing the API to recover
the existing operation and finish workspace registration. It does not generate a
replacement operation. Completed admissions are recovered through
`GET /operations/by-idempotency/{key}`. An `unknown` outcome stops for reconciliation.

Ready verification requires the current operation to have succeeded and the
workspace to be `active`. The script re-fetches it, recovers its operation and
reads nodes through the authenticated Kubernetes broker. Run it again in a fresh
process to test durable re-entry. This is API acceptance, not browser UI coverage
or a workload execution benchmark. It does not restart shared management services
to simulate a crash.

Each run saves private JSON previews, tickets, admissions and observations next to
`state.json`. Keep this directory outside the repository; files are mode 600 and
the directory is mode 700. These are local resume/inspection records, not signed
deployment release receipts. Deployment context/release artifacts continue to use
the existing S3 transport. No raw bearer token or kubeconfig is written to evidence.

Exit code `0` means the requested command completed. Exit code `2` means approval,
runtime preparation, authentication, reconciliation, timeout or another error
requires attention; the message explains the stop. Initialization, status and
approval decisions succeeding do not mean the workspace demo succeeded.

## Ownership-aware retirement

Retirement is explicit so a successful creation demo remains available to inspect:

```bash
python3 "$demo_script" --state-dir "$SUPERPLANE_DEMO_STATE" retire --timeout 600
```

The native flow first previews and requests separate approval for cleanup-access
preparation. After that operation succeeds, it previews and requests approval for
the exact removal plan. Use the same reviewer commands and rerun `retire` after
each decision. Both stages preserve the original retirement request identity.
The server supplies the distinct cleanup-access idempotency key. Unsupported
retirement modes or missing cleanup composition stop with the preview retained.
The script never substitutes a legacy `DELETE /workspaces/{id}` call.

Retirement success requires both the real retirement operation's `succeeded`
state and the workspace's final `Deleted` projection. A submission acknowledgement,
`Teardown` or a missing workspace does not count. The final lifecycle snapshot is
retained, including its billing/provider-observation limitations. Independent
provider absence and billing checks are still needed for a full external cleanup
audit; the API's metadata response alone cannot establish those facts.

For a separate new demo, choose a new state directory and workspace name. Preserve
the old directory until its workspace and resources have been accounted for.

## Offline validation

```bash
python3 -m unittest discover -s modules/domain-apps/superplane/scripts/tests -v
```

Tests exercise the lifecycle protocol, cold-process resume after a lost admission
response, separate cleanup approval, scope changes, stale/rejected/expired
approvals, unavailable runtime, unknown outcomes, broker failures, concurrent
invocations and token transport protections. They use a local API double and do
not claim that the live workspace demo has passed.

The Ready check requires a nonempty broker node inventory and every reported node
to have `ready: "True"`. An empty inventory is not a passing full-flow check.
