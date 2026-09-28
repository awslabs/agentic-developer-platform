# Model access with the ADP CLI

An ADP administrator can connect an AWS account and assign its Bedrock route:

```sh
adp admin bedrock connect --account 123456789012 --org example-org --profile aws-admin
adp admin bedrock list --org example-org
adp bedrock status
```

`--profile` selects credentials on this machine for AWS provisioning. ADP login
is separate. The CLI checks the actual STS account before creating a role and
uses the same CloudFormation role template and verification APIs as Model Access
in the UI. AWS credentials are never sent to ADP.

Add `--team Engineering` or `--user developer@example.com` to select a narrower
rule. Exact IDs work too; ambiguous names are rejected. The server resolves
**user → primary team → organization → platform default** for both personal and
user-owned cloud calls. An inherited route needs no personal AWS connection.
The current server authorization policy applies to every operation.

## Reuse an existing destination

```sh
adp admin bedrock connect --destination DESTINATION_ID --org example-org --team Engineering
adp admin bedrock verify DESTINATION_ID
adp admin bedrock status --user developer@example.com
```

These commands need no AWS CLI or AWS credentials. `connect` verifies the role
before assigning it; `verify` does not change a routing rule. `status` reports the
configured account and winning hierarchy level. It does not run inference or
prove that a previous request reached that account.

## Separate AWS administrator

```sh
adp admin bedrock connect --account 123456789012 --org example-org --download ./example-role
# The AWS administrator applies template.yaml using parameters.json and README.md.
adp admin bedrock connect --resume ./example-role
```

The first command registers a pending destination and writes a private directory;
it does not provision or assign. Treat the parameters as sensitive. Resume uses
the saved gateway, account and scope and verifies the role before assigning.
Neither command requires AWS credentials locally. Do not add account/scope/profile
options to `--resume`. `adp admin setup --org example-org` also offers this handoff
and can resume it on a later run.

## Automation and recovery

Use `--dry-run` to inspect the account, resolved scope and existing destination.
Use `--yes --json` to apply explicit inputs without prompts. JSON uses the
[shared CLI contract](../../../docs/design-notes/5180-cli-command-contract.md):
`status`, `command`, `detail`, `next_action`, and `error` on failure.
A download exits **4** because AWS provisioning remains pending; verified connect
exits **0**. Authentication and authorization failures exit 2 and 3 respectively.

Rerun the same command after interruption; existing pending destinations and
stacks are reused. A failed verification leaves the routing rule unchanged.
If another operator changed the rule during setup, inspect status and retry
with that configuration in view. New configuration can take about a minute to
reach routing caches. Legacy `adp bedrock connect|list` forms remain available.

## Repeatable acceptance test

Use the existing #5173 harness checkout; this adapter reuses its disposable
Cognito identities, EC2 runner, destination evidence collection and cleanup:

```sh
python modules/gateway/scripts/test-cli-routing.py \
  --harness-root /path/to/adp-routing-harness \
  --config /private/environment.json --state-dir /private/cli-routing-run --hosted
```

The config explicitly binds platform and destination profiles/accounts, gateway,
models and private subnet. `--hosted` additionally requires the real WebSocket,
queue and session-table configuration. Both accounts are checked before mutation.
Mapping writes use an isolated installation of this candidate CLI; the original
harness owns fixture role creation and EC2 model clients. This tests CLI assignment
and live hierarchy, not direct provisioning by the CLI or deployment of its files.
The report records candidate hashes and separate Claude, Codex and cloud outcomes.
Hosted/usage failures remain acceptance failures even when inference evidence exists.
Use the same arguments with `--cleanup-only` after interrupted cleanup. The optional
`--resume` reruns against retained fixtures without provisioning; earlier omitted
acceptance checks remain in the report. Run `--cleanup-only` afterward. It must
not be used to turn a failed cloud gate into a passing EC2-only report. The optional
`--maintenance-kubeconfig` retains the harness's scoped cleanup for gateways that
lack the destination delete API. Never delete another run's state or resources.

To validate creation of a **new** role through the CLI, use a fresh state directory
for each mode:

```sh
python modules/gateway/scripts/test-cli-routing.py \
  --harness-root /path/to/adp-routing-harness \
  --config /private/environment.json --state-dir /private/cli-direct --provision direct
python modules/gateway/scripts/test-cli-routing.py \
  --harness-root /path/to/adp-routing-harness \
  --config /private/environment.json --state-dir /private/cli-handoff --provision handoff
```

Both modes execute the candidate ADP CLI, AWS provisioning, Claude and Codex on
disposable private EC2 instances. The operator machine orchestrates fixtures and
collects evidence. Each instance assumes a temporary destination-account role
restricted to its single test stack and role; it cannot invoke Bedrock directly.
No operator AWS credentials are copied to EC2. The worker checks its EC2 instance
and account identity before accessing the private fixture session.

Direct mode checks account mismatch and dry-run safety, creates/verifies/assigns
the role, then repeats the command to prove reuse. Handoff mode downloads with
AWS access disabled in the ADP process, confirms premature resume cannot assign,
applies the files in a separate AWS-admin process, and resumes without AWS access.
Both check the CLI's effective route and correlate actual Claude/Codex requests
with destination-account invocation logs and ADP usage. This uses the deployed
gateway's routing APIs and a seeded Cognito session; it does not validate deployment
of the new native-login endpoint or the hosted-agent dispatch path.

Reports include the EC2 ID, provisioning caller ARN, candidate file hashes, stack
ID and separate provisioning/inference gates. Setup failures remain failures.
Cleanup removes only the fixture, including its temporary provisioner role. The
CLI-created stack has no test tag, so cleanup additionally requires recorded prior
absence, matching account/name/creation time, the original stack ID, and only the
expected IAM role resource. Interrupted runs use the same `--provision` mode and
state directory with `--cleanup-only`; never substitute another run's state.
