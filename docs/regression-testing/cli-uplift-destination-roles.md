# CLI-uplift evaluation — destination account roles (ready to apply)

The broader CLI-uplift evaluation (#5256) cannot run its destination suites
(`personal-aws`, `routing`, `inference` — cases E04–E12) until two IAM roles
exist in the **Bedrock destination account `605440105851`**. This document is the
ready-to-apply artifact for whoever holds access to that account.

Nothing here needs to be invented: the role names, the trust principals and the
permissions are all determined by what the harness actually calls. This runbook
records them so the change can be applied and reviewed without reverse-engineering
the harness.

## Why an external request

Everything else in the evaluation runs in platform account `879318057152`, where
the evaluation already has what it needs. The destination account is a separate
trust boundary and the evaluation identity has no path into it:

```
$ aws sts assume-role --role-arn arn:aws:iam::605440105851:role/OrganizationAccountAccessRole ...
AccessDenied: User: arn:aws:sts::879318057152:assumed-role/adp-dev-agent-scaledjob-role/...
is not authorized to perform: sts:AssumeRole on resource:
arn:aws:iam::605440105851:role/OrganizationAccountAccessRole
```

The same result for `adp-cli-uplift-eval-destination` and
`adp-cli-uplift-eval-provisioner`. There is no self-service route, and there
should not be — creating one would mean granting the evaluation identity
cross-account role-creation rights, which is a larger grant than the thing being
requested.

**Deliberately not done:** no existing role in `605440105851` is borrowed or
modified. Existing roles there are not disposable fixtures, and repurposing one
would both change behaviour for its real owner and make CloudTrail attribution
ambiguous. No third account is involved.

## What is requested

Two roles in account `605440105851`, in the same region as the evaluation.

| Role | Assumed by | Purpose |
|---|---|---|
| `adp-cli-uplift-eval-destination` | `arn:aws:iam::879318057152:role/adp-cli-uplift-eval-orchestrator` | Prove cross-account identity and delete destination-account resources during cleanup |
| `adp-cli-uplift-eval-provisioner` | `arn:aws:iam::879318057152:role/adp-cli-uplift-eval-instance` | Run the CLI's CloudFormation role setup, as a real AWS admin would |

Both ARNs must name `605440105851`. `config.validate()` refuses an ARN in any
other account, because a cross-account test that silently ran against the
platform account would pass while proving nothing.

### Why two roles and not one

They are assumed by different principals for different reasons, and collapsing
them would weaken the test:

- The **provisioner** is assumed *from the EC2 instance*, because E07 asserts
  CloudTrail attributes the CloudFormation apply to the EC2 provisioner — that is
  the point of the case. It models the customer's own AWS administrator.
- The **destination** role is assumed *from the orchestrator*, which never runs
  product code. Cleanup must be able to delete a destination-account resource
  using a session that belongs to that account; the harness raises rather than
  falling back to platform credentials, precisely so a leak cannot report clean.

## Role 1 — `adp-cli-uplift-eval-destination`

Trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::879318057152:role/adp-cli-uplift-eval-orchestrator"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Permissions — identity evidence plus cleanup of what the run itself created:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "IdentityEvidence",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    },
    {
      "Sid": "CleanupRunOwnedStacks",
      "Effect": "Allow",
      "Action": [
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DeleteStack"
      ],
      "Resource": "arn:aws:cloudformation:*:605440105851:stack/adp-e2e-*/*"
    },
    {
      "Sid": "CleanupRunOwnedRoles",
      "Effect": "Allow",
      "Action": [
        "iam:GetRole",
        "iam:DeleteRole",
        "iam:ListRolePolicies",
        "iam:DeleteRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:DetachRolePolicy"
      ],
      "Resource": "arn:aws:iam::605440105851:role/adp-e2e-*"
    }
  ]
}
```

Note `sts:GetCallerIdentity` requires `"Resource": "*"` — the API takes no
resource. Everything else is bounded to the `adp-e2e-*` prefix the run owns.

## Role 2 — `adp-cli-uplift-eval-provisioner`

Trust policy — assumed by the **EC2 instance role**, not the orchestrator:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::879318057152:role/adp-cli-uplift-eval-instance"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Permissions — enough to apply and roll back the CLI's own CloudFormation
template, and nothing more:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "IdentityEvidence",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    },
    {
      "Sid": "RoleSetupStack",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack",
        "cloudformation:DeleteStack",
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStackResources",
        "cloudformation:GetTemplate"
      ],
      "Resource": "arn:aws:cloudformation:*:605440105851:stack/adp-e2e-*/*"
    },
    {
      "Sid": "RoleSetupRole",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PassRole",
        "iam:TagRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:ListRolePolicies",
        "iam:GetRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:ListAttachedRolePolicies"
      ],
      "Resource": "arn:aws:iam::605440105851:role/adp-e2e-*"
    }
  ]
}
```

### Bedrock access is deliberately absent from both

Neither role grants Bedrock. The role the CLI *creates* (`adp-e2e-*`, via the
product's own CloudFormation template) is what carries Bedrock access, and it is
created by the flow under test. Pre-granting Bedrock to the provisioner would let
a broken `adp aws connect` still show working inference — the exact false green
that E04–E06 exist to catch. This mirrors the instance profile, which has no
Bedrock permission for the same reason.

No `AdministratorAccess`, no wildcard resource outside the two no-resource
`sts:GetCallerIdentity` calls, no static access keys, and no widening of the
existing platform-account roles.

## Applying it

```bash
# In account 605440105851, with an identity that can create roles.
aws iam create-role --role-name adp-cli-uplift-eval-destination \
  --assume-role-policy-document file://trust-destination.json \
  --tags Key=adp:cli-uplift-eval,Value=adp-e2e-fixture Key=ManagedBy,Value=manual
aws iam put-role-policy --role-name adp-cli-uplift-eval-destination \
  --policy-name cli-uplift-eval-destination --policy-document file://policy-destination.json

aws iam create-role --role-name adp-cli-uplift-eval-provisioner \
  --assume-role-policy-document file://trust-provisioner.json \
  --tags Key=adp:cli-uplift-eval,Value=adp-e2e-fixture Key=ManagedBy,Value=manual
aws iam put-role-policy --role-name adp-cli-uplift-eval-provisioner \
  --policy-name cli-uplift-eval-provisioner --policy-document file://policy-provisioner.json
```

Then set the two repository variables so the workflow picks them up (they layer
over `bindings.dev.json`, which deliberately leaves both absent):

- `CLI_UPLIFT_EVAL_DESTINATION_ROLE_ARN` = `arn:aws:iam::605440105851:role/adp-cli-uplift-eval-destination`
- `CLI_UPLIFT_EVAL_PROVISIONER_ROLE_ARN` = `arn:aws:iam::605440105851:role/adp-cli-uplift-eval-provisioner`

## Verifying it worked

```bash
# From the orchestrator's session:
aws sts assume-role \
  --role-arn arn:aws:iam::605440105851:role/adp-cli-uplift-eval-destination \
  --role-session-name cli-uplift-eval-verify \
  --query 'AssumedRoleUser.Arn' --output text
# Expect an ARN naming 605440105851.
```

Then dispatch `eval-cli-uplift.yml` with `suites=personal-aws`. Preflight's
cross-account check proves the destination identity really is a different
account; if the ARN is wrong the run refuses to start rather than passing
against the platform account.

## What remains blocked without this

`config.require_bindings()` derives required bindings from the selected suites,
so the destination suites fail closed at config time:

```
ConfigError: Config is missing the bindings this suite needs:
destination_role_arn, provisioner_role_arn
```

That is the guard working. Cases E04–E12 stay `blocked` — they are graded
`blocked`, never `passed`, and the harness cannot be talked out of it — so
`full_acceptance=true` is unreachable until these roles exist. Suites needing
only `ec2` + `platform` (`install`, `admin`, `parity`, `harness`) are unaffected
and continue to run.
