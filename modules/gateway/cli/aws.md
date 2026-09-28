# Connect your own AWS account with the ADP CLI

Connect a personal AWS account so ADP can read it on your behalf:

```sh
adp aws connect --account 123456789012 --profile personal
adp aws list
adp aws verify personal-123456789012
```

`--profile` is optional and only selects credentials on **this machine** for
creating the IAM role. ADP login is separate, and your AWS credentials are never
sent to ADP — the gateway is given the account ID and the role ARN to assume,
nothing else. The CLI checks the real STS account before it creates anything, so
the wrong profile fails instead of planting a role in someone else's account.

These are the same connection records, the same personal role template and the
same connect/verify APIs as the Credentials page: a connection made here appears
and can be managed there, and the other way round.

**Connecting an account does not route anyone's Bedrock model calls to it.**
Shared inference routing is a separate, separately authorized decision made by an
administrator (`adp admin bedrock`, or Model Access in the UI). Nothing in this
document changes it.

## Register a role you already have

```sh
adp aws connect --account 123456789012 \
  --role-arn arn:aws:iam::123456789012:role/MyExistingRole \
  --external-id-file ~/.adp/private/external.json
```

Use this when the role exists already — no AWS CLI and no local AWS credentials
are needed. The ARN must be an IAM **role** in `--account`. ADP checks the trust
policy by assuming the role; registering is not the same as proving it works, so
the command only reports success after that check passes.

The ExternalId is read from a private `0600` file holding
`{"external_id": "…"}`, from the same JSON object on stdin with
`--external-id-stdin`, or from a hidden prompt on a terminal. There is
deliberately no flag that takes the value directly — command arguments are
visible to other processes and end up in shell history. If the role's trust
policy has no ExternalId condition, say so explicitly with `--no-external-id`.

## Someone else creates the role (administrator handoff)

```sh
adp aws connect --account 123456789012 --download ./aws-setup
# The AWS administrator applies template.yaml using parameters.json and README.md.
adp aws connect --resume ./aws-setup
```

Use this when you cannot create IAM resources yourself. The first command saves a
pending connection and writes a private `0700` directory; it provisions nothing.
`--resume` reads the account, gateway and connection back out of that directory
and asks ADP to assume the role. **Neither command needs AWS credentials on your
machine.**

Treat that directory as sensitive: `parameters.json` contains this connection's
ExternalId and the tag that pins the role to your ADP user. Share it only with the
administrator applying it. Pass no other setup options to `--resume` — the saved
values are the point. `adp aws list` shows a downloaded setup that is still
waiting, so an interrupted handoff is findable later without remembering the path.

## Checking, and disconnecting

```sh
adp aws verify personal-123456789012
adp aws disconnect personal-123456789012
```

`verify` re-assumes the role **now** rather than reporting a stored verdict, so a
role that was deleted or retagged last week shows as failed. `disconnect` removes
ADP's connection record and its stored role reference; it does **not** delete the
IAM role, its policies or the CloudFormation stack. Delete the stack yourself in
AWS if you want the role gone. Anything an administrator pointed at the connection
stops working once it is disconnected.

Both accept a connection name or its ADP ID.

## Automation and recovery

`--dry-run` shows the account, the role ARN, what the connection grants and
whether an existing connection would be reused, and performs only reads.
`--yes` approves without a prompt; non-interactive runs require it. `--json` uses
the [shared CLI contract](../../../docs/design-notes/5180-cli-command-contract.md):
`status`, `command`, `detail`, `next_action`, and `error` on failure. A download
exits **4** because provisioning is still pending; a verified connect exits **0**;
authentication and authorization failures exit **2** and **3**.

Rerun the same command after an interruption. A pending connection and an existing
`ADP-Agent-<name>` stack are reused rather than duplicated, and a repeated
`--role-arn` registration returns the same connection. A failed verification keeps
the pending connection so the next run resumes it. Reusing a `--name` for a
*different* account is refused; pick another name. The CLI never replaces a stack
that is in a rollback or otherwise unfinished state — resolve that in AWS first.

Nothing printed or written to state contains the ExternalId; AWS error text is
reduced to its error code for the same reason, since CloudFormation validation
errors quote parameter values.

## Repeatable acceptance test

Deterministic coverage runs in CI: `tests/cli/test_adp_aws.py` (provisioning,
existing-role import, handoff/resume with no AWS credentials, account mismatch,
repeat and interrupted runs, disconnect semantics, no secrets in arguments) and
`tests/auth/test_aws_connect.py` (setup re-read, import, fresh verification and
owner scoping).

Live personal-AWS provisioning has **not yet been validated**. The repeatable
EC2 adapter is tracked in [#5199](https://github.com/aws-e/adp/issues/5199),
cases E04/E05, using the named environment bindings on #5180.

The existing `test-cli-routing.py --provision direct|handoff` harness invokes
`adp admin bedrock connect`. It tests shared Bedrock destinations, so running it
cannot validate `adp aws connect` or the personal credential record. Reuse its
EC2 fixture, evidence and cleanup infrastructure when adding the personal-AWS
adapter; do not substitute a Bedrock run for personal-AWS acceptance.

The personal-AWS adapter must execute `adp aws connect` on EC2 for direct
provisioning, existing-role import and download/apply/resume. It must check
account mismatch, fresh verification, owner isolation, interrupted-run reuse,
and the canonical credential through the live API. For handoff, disable AWS
access in the ADP process and apply the downloaded template from a separate
AWS-admin process. Verify that disconnect removes the ADP credential while
preserving the role, then clean up only run-owned AWS resources.

A personal connection is expected to be **not** routing-capable: its role is
pinned to one ADP user. Shared inference routing requires its own destination
and routing rule.
