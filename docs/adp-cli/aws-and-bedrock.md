# AWS accounts and Bedrock model access

[CLI guide](README.md) · [Command reference](command-reference.md)

There are two workflows. A personal AWS connection lets ADP access AWS resources
on your behalf. A Bedrock destination and routing rule determine which AWS
account serves model requests. Connecting a personal AWS account does not
automatically change model routing.

| Task | Command group | Authority |
|---|---|---|
| Connect an account for my ADP user | `adp aws` | Your ADP session; AWS provisioning authority if creating the role directly |
| Configure shared Bedrock destinations and routes | `adp admin bedrock` | Platform administrator; AWS provisioning authority for direct creation |
| Inspect my effective Bedrock destination | `adp bedrock status` | Your ADP session |

Account IDs below are examples. Use IAM role sessions for AWS access. A local
AWS profile selects those credentials; the CLI checks the actual STS account
before provisioning. ADP login and AWS sign-in remain separate.

## Connect your AWS account

```bash
adp aws connect --account 123456789012 --name research --profile aws-admin
adp aws list
adp aws verify research
```

The direct flow registers a connection, creates its role with CloudFormation
and asks ADP to verify it by assuming the role. The default personal template
grants read access and pins the role to your ADP user. It is not the shared
Bedrock-routing template.

Preview before applying, or automate explicit inputs:

```bash
adp aws connect --account 123456789012 --name research --profile aws-admin --dry-run
adp aws connect --account 123456789012 --name research --profile aws-admin --yes --json
```

### Reuse an existing IAM role

```bash
adp aws connect --account 123456789012 --name research \
  --role-arn arn:aws:iam::123456789012:role/ADPResearch \
  --external-id-file /private/research-external-id.json
```

The private file must be owned by you, have permissions `0600`, and contain
`{"external_id":"VALUE_FROM_THE_ROLE_OWNER"}`. You can use
`--external-id-stdin` with the same JSON or omit the input option for a hidden
interactive prompt. `--no-external-id` is for a role whose trust policy actually
has no ExternalId condition.

This flow needs no local AWS provisioning credentials. The role must already
trust the ADP deployment appropriately; ADP verifies it before reporting success.

### Have an AWS administrator create the role

```bash
adp aws connect --account 123456789012 --name research --download ./research-role
# Give the handoff files to the AWS administrator, then wait for them to apply it.
adp aws connect --resume ./research-role
adp aws verify research
```

Download creates a pending ADP connection and private handoff files, but no AWS
role. Share `template.yaml`, `parameters.json` and `README.md` privately with
the AWS administrator. Keep the full local directory for resume. Its parameters
contain an ExternalId. The administrator follows its generated README using
their own AWS role session; neither download nor resume needs your own AWS
credentials.

### Disconnect

```bash
adp aws disconnect research --dry-run
adp aws disconnect research
```

This removes the ADP connection and stored role reference. It leaves the AWS
role, policies and CloudFormation stack. Remove those separately in AWS if they
are no longer needed. Consumers using the connection can stop working after it
is disconnected.

## Configure Bedrock routing

Sign in as an ADP administrator first. Organization and team names can be used
when unambiguous; exact IDs resolve ambiguity.

Organization rule:

```bash
adp admin bedrock connect --account 123456789012 \
  --org example-org --profile aws-admin
```

Team rule within that organization:

```bash
adp admin bedrock connect --account 123456789012 \
  --org example-org --team Engineering --profile aws-admin
```

User rule:

```bash
adp admin bedrock connect --account 123456789012 \
  --org example-org --user developer@example.com --profile aws-admin
```

Choose the scope you intend to change; you do not need to run all three examples.
Verification must succeed before the command assigns the rule. Both personal
and user-owned cloud model requests use the routing hierarchy:

1. User rule.
2. Primary team rule.
3. Organization rule.
4. Platform default when no applicable rule exists.

The most local applicable rule wins. An administrator-pinned user rule takes
priority over a user's own selection. The command reports the proposed scope
and replacement before asking for confirmation. `--dry-run` previews it and
`--yes --json` supports explicit scripted changes.

### Reuse a registered destination

```bash
adp admin bedrock list --org example-org
adp admin bedrock connect --destination DESTINATION_ID \
  --org example-org --team Engineering
adp admin bedrock verify DESTINATION_ID
```

This re-verifies the destination and assigns the requested scope without local
AWS provisioning. The destination must be eligible for that organization and
scope. An AWS account number alone is insufficient: the destination must refer
to an assumable, verified role.

### Download, apply and resume

```bash
adp admin bedrock connect --account 123456789012 \
  --org example-org --team Engineering --download ./engineering-bedrock
# An AWS administrator applies the generated template and parameters.
adp admin bedrock connect --resume ./engineering-bedrock
```

The saved directory includes the account, gateway and routing scope. Resume
uses them; do not add a different account, team, user or profile. Download exits
with code 4 because provisioning remains pending. No rule is assigned until
verification succeeds.

### Check the result

```bash
adp bedrock status
adp admin bedrock status --user developer@example.com --json
```

Status reports the effective account and winning routing level. It does not
invoke a model or validate the account charged for an earlier request. Routing
changes may take about a minute to reach caches.

If setup is interrupted, rerun the same command or resume the same directory.
Existing pending destinations and stacks are reused. The CLI does not replace
a failed CloudFormation stack automatically; inspect and resolve that stack in
AWS. A failed verification leaves the existing routing rule unchanged.
