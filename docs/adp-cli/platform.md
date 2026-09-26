# Platform lifecycle through canonical tooling

`adp deployment` selects local gateway contexts. `adp platform` provides a thin
facade over the [canonical agent deployment guide](../adp-platform-deployment/deploy-with-agent.md)
and [verified quickstart](../adp-platform-deployment/deploy-quickstart.md).
Those documents and scripts remain the deployment procedure.

```sh
adp platform status --environment dev --json
mkdir -m 700 /tmp/adp-reviewed-update
adp platform plan --source-checkout /path/to/clean/adp --source-revision FULL_COMMIT_SHA --environment dev --profile customer-admin --region us-east-1 --scope full --output /tmp/adp-reviewed-update/plan.json --json
# Review the returned actual STS account/ARN, source, scope and hash first:
adp platform apply --plan-file /tmp/adp-reviewed-update/plan.json --expect-plan-hash REVIEWED_HASH --confirm-account REVIEWED_ACCOUNT --json
```

Status uses the selected gateway's capabilities. It separates gateway, factory,
webhook, model and GitHub metadata but explicitly leaves AWS resources, environment
identity and placeholder artifacts unverified. Supplying `--environment prod`
does not retarget a dev gateway or certify prod. No URL or capability response
establishes that the factory is installed, workers are ready, or placeholder
artifacts have been replaced.

Plan produces an **invocation preview, not a saved Terraform plan**. It performs
read-only source and STS checks and writes the private review file outside the
source checkout. Apply delegates only to `deploy-all.sh --update`; initial full
provisioning remains in the canonical guide. The canonical tool still discovers
actual installed modules, requires the agent factory for a full update, builds
pinned artifacts, and owns saved Terraform plans, backend locking, destructive
change refusals, migrations and verification. The facade never passes
`--confirm-destructive`. `--scope gateway` is explicitly partial maintenance.

The source checkout must match the full reviewed commit and have no modified or
untracked inputs except canonical journals and the facade's private invocation
receipts. Local untracked deployment config/shell overlays are outside this
contract; use canonical tooling directly when they are required. Subprocesses
use the selected AWS profile and region, with inherited raw credentials,
shell/Python startup hooks, Terraform arguments and source/config/release overrides
removed. The optional `AGENT_CONTEXT_ENABLED` and `SUPERPLANE_ENABLED` values
must be literal true/false and are bound to the review.

ADP login, tenant and gateway selection do not grant AWS deployment authority.
Only status uses a gateway selection; plan/apply/resume use the explicitly
selected AWS profile, actual STS account/ARN and confirmed account. A plan expires
after one hour. Source, account, toggles and journal hashes are rechecked after
acquiring the invocation lock. Each reviewed plan starts at most once; a private
receipt is written before launching canonical tooling. An interrupted or repeated
invocation stays pending, and is not automatically replayed.

For recovery, inspect actual resources and the canonical evidence first. Prepare
a **new** reviewed plan against the current journal, then use:

```sh
adp platform resume --state-file /path/to/clean/adp/.adp-deploy-state.json --plan-file /tmp/adp-reviewed-update/new-plan.json --expect-plan-hash NEW_REVIEWED_HASH --confirm-account REVIEWED_ACCOUNT --json
```

Resume verifies journal ownership and delegates to the canonical idempotent
update discovery again. It does not invent phase skipping: `deploy-all.sh
--update` discovers live state and owns its own upgrade evidence. A copied or
committed `.adp-deploy-state.json` is never proof a phase completed. Keep the
journal and verify phases using the canonical guide. The facade reports pending
after a successful script exit until actual phase/artifact evidence is reviewed.

Teardown is separately reviewed:

```sh
adp platform teardown plan --source-checkout /path/to/clean/adp --source-revision FULL_COMMIT_SHA --environment dev --profile customer-admin --region us-east-1 --output /tmp/adp-reviewed-update/teardown.json --json
adp platform teardown apply --plan-file /tmp/adp-reviewed-update/teardown.json --expect-plan-hash REVIEWED_HASH --confirm-account REVIEWED_ACCOUNT --json
```

The preview runs canonical `undeploy.sh --dry-run`, retains the full bounded
inventory and data/resource impact, and rechecks that inventory before execution.
Apply retains the canonical terminal typed-account confirmation; noninteractive
teardown is unavailable because that script has no headless account-confirmation
contract. No `--yes`, legacy `deploy-all.sh --destroy`, or bootstrap destruction
bypass is offered. State backend and protected secrets survive by default under
the canonical procedure. GitHub wiring remains at the end of deployment.

E41 runs read-only status in the existing nightly evaluation. Real update,
interrupted resume, placeholder verification and teardown cleanup remain live
acceptance holds requiring a separately authorized disposable fixture. This
story adds no nightly full-stack deployment and does not claim live completion.
