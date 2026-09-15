# Model access with the ADP CLI

An ADP administrator can connect an AWS account and assign its Bedrock route:

```sh
adp admin bedrock connect --account 123456789012 --org SOPHOS-IT --profile sophos
adp admin bedrock list --org SOPHOS-IT
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
adp admin bedrock connect --destination DESTINATION_ID --org SOPHOS-IT --team Engineering
adp admin bedrock verify DESTINATION_ID
adp admin bedrock status --user developer@example.com
```

These commands need no AWS CLI or AWS credentials. `connect` verifies the role
before assigning it; `verify` does not change a routing rule. `status` reports the
configured account and winning hierarchy level. It does not run inference or
prove that a previous request reached that account.

## Separate AWS administrator

```sh
adp admin bedrock connect --account 123456789012 --org SOPHOS-IT --download ./sophos-role
# The AWS administrator applies template.yaml using parameters.json and README.md.
adp admin bedrock connect --resume ./sophos-role
```

The first command registers a pending destination and writes a private directory;
it does not provision or assign. Treat the parameters as sensitive. Resume uses
the saved gateway, account and scope and verifies the role before assigning.
Neither command requires AWS credentials locally. Do not add account/scope/profile
options to `--resume`. `adp admin setup --org SOPHOS-IT` also offers this handoff
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
`--maintenance-kubeconfig` retains the harness's scoped cleanup for gateways that
lack the destination delete API. Never delete another run's state or resources.
