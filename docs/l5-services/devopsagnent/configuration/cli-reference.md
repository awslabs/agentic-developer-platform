# CLI procedure and verification

Use with the [configuration manual](README.md). These are existing ADP read
commands and public AWS CLI operations, not a new ADP DevOps command set. Examples
were checked against ADP source and the `devops-agent` Botocore model distributed
with boto3 **1.43.118**. This is syntax/contract validation, not live execution.

AWS CLI v2 must include the `devops-agent` command group. Confirm with
`aws devops-agent help`; update the CLI through your normal tooling if it is
missing. Use [AWS's CLI onboarding guide][aws-cli] for the full IAM-role creation
commands and [the CLI reference][cli-reference] for current operation parameters.

## 1. Read the selected Product's Profile

Use the installed ADP CLI against the intended deployment:

```bash
adp product list --json
adp product show --product "$PRODUCT_ID" --project "$PROJECT_ID" --json
adp product source-bindings --product "$PRODUCT_ID" --project "$PROJECT_ID" --json
adp product sources --product "$PRODUCT_ID" --project "$PROJECT_ID" --json
```

`PRODUCT_ID` and `PROJECT_ID` identify the onboarded project being configured;
they are not fixed to the ADP Product or repository. Both must be existing
authorized IDs. Follow pagination
when responses include a continuation cursor. Profile and binding responses are
separate: use bindings for current environment/connection evidence and inspect
`status`, `observed_at`, `stale_after` and `verified_environment`.

The current CLI also supports `adp product project update` for typed
configuration changes, with revision/idempotency inputs. It does not provision a
DevOps Agent Space. See the [mapping](product-profile-mapping.md) before treating
any Profile field as provider configuration.

## 2. Verify AWS identity and inventory

Set these values from private, selected configuration. They are ordinary
configuration variables, not bearer credentials:

- `DEVOPS_AWS_PROFILE`: the operator profile for the intended monitoring account.
- `DEVOPS_REGION`: the supported Agent Space Region.
- `DEVOPS_SPACE_ID`: the chosen existing space ID, or the ID created in step 3.
- `DEVOPS_RESOURCE_ROLE_ARN`: the compatible resource-reading role in the primary account.
- `DEVOPS_OPERATOR_ROLE_ARN`: the chosen web-app operator role.

```bash
aws --profile "$DEVOPS_AWS_PROFILE" sts get-caller-identity
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent list-agent-spaces
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent list-services
```

Confirm the returned account before changing provider configuration. If reusing
an existing space, inspect it and its associations rather than repeating creation:

```bash
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent get-agent-space --agent-space-id "$DEVOPS_SPACE_ID"
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent list-associations --agent-space-id "$DEVOPS_SPACE_ID"
```

## 3. Create the selected space and compatible roles, if needed

Skip space creation when reusing the verified space. Set `DEVOPS_SPACE_NAME` and
`DEVOPS_REQUEST_ID` privately; the latter is a stable UUID idempotency token for
this creation request. Keep its result in the private setup record.

```bash
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent create-agent-space \
  --name "$DEVOPS_SPACE_NAME" \
  --description 'Operational investigation for the selected product and environment' \
  --client-token "$DEVOPS_REQUEST_ID" \
  --query 'agentSpace.agentSpaceId' --output text
```

Set `DEVOPS_SPACE_ID` to the returned ID. If customer-managed encryption is
required, select the KMS key at creation using the documented `--kms-key-arn`
option. Do not silently recreate an existing space to change setup choices.

Create or verify the resource and operator roles using the
[official role-creation procedure][aws-cli]. The resource-role trust allows
`aidevops.amazonaws.com` to assume it with source-account/space constraints.
The operator trust additionally supports `sts:TagSession`. Use the actual
selected space ARN where possible. The resource policy and operator policy have
different purposes:

| Role | AWS documented policy starting point | Additional work |
| --- | --- | --- |
| Resource-reading role | `AIDevOpsAgentAccessPolicy` | Selected resource/Region boundaries, required discovery permissions and any scoped S3/KMS access. |
| Web-app operator role | `AIDevOpsOperatorAppAccessPolicy` | Intended user's native app privileges and authentication path. Do not grant administration to every user just to remove an access error. |

A pre-existing ADP account connection role cannot be substituted without checking
its trust and effective permissions. See [manual steps 3–5](README.md#3-grant-aws-resource-access-and-associate-the-accounts)
for account, user and EKS distinctions.

## 4. Associate AWS accounts

Create a **private** file named `primary-association.json`. The following is a
fictional structural example; replace its account and role with verified inputs
before use:

```json
{
  "aws": {
    "assumableRoleArn": "arn:aws:iam::123456789012:role/ExampleDevOpsResourceRole",
    "accountId": "123456789012",
    "accountType": "monitor"
  }
}
```

```bash
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent associate-service \
  --agent-space-id "$DEVOPS_SPACE_ID" --service-id aws \
  --configuration file://primary-association.json
```

For an additional account, create/verify its resource role in that account first.
The trust's source-account and space constraints identify the **monitoring**
account/space. Create a private `source-association.json` using the distinct
`sourceAws` shape:

```json
{
  "sourceAws": {
    "accountId": "000000000002",
    "accountType": "source",
    "assumableRoleArn": "arn:aws:iam::000000000002:role/ExampleDevOpsSourceRole"
  }
}
```

```bash
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent associate-service \
  --agent-space-id "$DEVOPS_SPACE_ID" --service-id aws \
  --configuration file://source-association.json
```

Use one association for each selected account. Review existing associations and
use the applicable update operation when changing them. See the
[secondary-account guide][accounts] for its setup-time `iam:PassRole` requirement.
The CLI does not validate your Product ownership or decide account scope for you.

## 5. Enable and inspect the operator app

For the IAM path:

```bash
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent enable-operator-app \
  --agent-space-id "$DEVOPS_SPACE_ID" --auth-flow iam \
  --operator-app-role-arn "$DEVOPS_OPERATOR_ROLE_ARN"
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent get-operator-app --agent-space-id "$DEVOPS_SPACE_ID"
```

This changes the space's authentication configuration; do not overwrite a
working team's configuration as a troubleshooting shortcut. For Identity Center,
use the documented `idc` flow and instance. For external IdP, configure the
supported provider, client, callback and credentials using the guide; keep client
secrets out of shared command history and reports. Browser access remains a
separate acceptance test. CLI configuration success alone does not establish SSO.

## 6. Register GitHub, then associate repositories

Complete AWS's account-level GitHub registration described in
[manual step 6](README.md#6-register-github-and-connect-the-products-repositories).
The CLI onboarding guide requires the console registration workflow before the
repository association step. Other current registration modes have their own
provider verification requirements; do not assume an ADP installation satisfies them.

```bash
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent list-services --filter-service-type github
```

Set `DEVOPS_GITHUB_SERVICE_ID` to the matching AWS registration. Do not choose the
first registration when multiple organizations or permission levels are available.
Prepare a private `github-association.json` from authorized repository metadata:

```json
{
  "github": {
    "repoName": "example-service",
    "repoId": "100000001",
    "owner": "example-org",
    "ownerType": "organization"
  }
}
```

The following minimal mutation demonstrates repository association; it does
**not** disable native automatic review/testing. For observation-only setup,
create the association through the manual's console flow with those settings
explicitly disabled instead of running this minimal example. Use this example
only when its provider automation behavior is appropriate for the intended setup.
The reviewed SDK exposes a capabilities map, but this appendix does not invent
unqualified capability keys or claim that omitted flags disable execution.

```bash
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent associate-service \
  --agent-space-id "$DEVOPS_SPACE_ID" \
  --service-id "$DEVOPS_GITHUB_SERVICE_ID" \
  --configuration file://github-association.json
```

After association, verify its effective automation settings and repository scope
through the current AWS integration workflow.

## 7. Read back configuration and operating state

```bash
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent list-associations --agent-space-id "$DEVOPS_SPACE_ID"
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent list-triggers --agent-space-id "$DEVOPS_SPACE_ID"
aws --profile "$DEVOPS_AWS_PROFILE" --region "$DEVOPS_REGION" \
  devops-agent list-assets --agent-space-id "$DEVOPS_SPACE_ID"
```

Compare returned native IDs and configuration with the desired private record.
Follow pagination. A sample skill or an empty custom-trigger list does not prove
that product knowledge is configured or that all managed-agent automation is off.
Inspect native integration settings and the Improvements schedule as well.

For a selected asset, use `get-asset` / `get-asset-content` with its native
`--asset-id` and, where appropriate, `--asset-version`. Treat returned content,
links and exports as potentially sensitive; retain only the required evidence in
its authorized store. Do not publish raw provider responses in public docs.

Complete the [manual's operational acceptance tests](README.md#10-verify-operational-usefulness).
CloudWatch logs/metrics, known EKS reads, repository access and a controlled
investigation establish different parts of readiness.

## Console and integration work still required

This appendix does not claim a fully automated setup script. GitHub/provider
consent, EKS access, IdP assignment, one-time webhook secret capture, private
networking, knowledge publication, trigger choices and release-testing environment
configuration must follow the relevant guide steps. Some have public APIs/IaC;
they still need qualified inputs and readback rather than fabricated CLI flags.

Future ADP UI, CLI and MCP clients should call the same L5 configuration API.
Until the DevOps adapter is implemented, AWS CLI commands act directly on AWS
and do not automatically create an ADP L5 receipt, enforce ADP budgets or publish
results into Product Context.

[aws-cli]: https://docs.aws.amazon.com/devopsagent/latest/userguide/getting-started-with-aws-devops-agent-cli-onboarding-guide.html
[cli-reference]: https://docs.aws.amazon.com/cli/latest/reference/devops-agent/
[accounts]: https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-multiple-aws-accounts.html
