# Customer AWS Setup

Connect your AWS account to the ADP hosted platform so agents can deploy, manage,
and inspect resources on your behalf. Three permission tiers are available.

## How It Works

ADP uses IAM cross-account role assumption with STS ExternalId for security:

1. You deploy a CloudFormation stack in your AWS account (one click)
2. The stack creates an IAM role that trusts the ADP platform
3. ADP agents assume that role with short-lived credentials (1-12h)
4. Every action is tagged in CloudTrail with the tenant, agent, run, and actor

No long-lived credentials are stored. No access keys are created.

## Prerequisites

- An AWS account where you want agents to operate
- Your **ADP External ID** (provided in your ADP dashboard under Settings > AWS Access)
- Your **ADP Platform Account ID** (provided alongside the External ID)

## Step 1: Choose a Permission Tier

| Tier | Use Case | Template |
|------|----------|----------|
| **Read-Only** | Agents can inspect infrastructure, read logs, describe resources | [Launch Stack][readonly-url] |
| **Scoped Write** | Agents can deploy to specific services/resources you define | [Launch Stack][scoped-url] |
| **Full Admin** | Agents have full access (requires explicit confirmation) | [Launch Stack][admin-url] |

[readonly-url]: https://console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?templateURL=https://adp-public-cfn.s3.amazonaws.com/readonly.cfn.yaml&stackName=adp-hosted-agent-readonly
[scoped-url]: https://console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?templateURL=https://adp-public-cfn.s3.amazonaws.com/scoped-write.cfn.yaml&stackName=adp-hosted-agent-scoped-write
[admin-url]: https://console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?templateURL=https://adp-public-cfn.s3.amazonaws.com/full-admin.cfn.yaml&stackName=adp-hosted-agent-full-admin

## Step 2: Deploy the Stack

1. Click the **Launch Stack** link for your chosen tier
2. Fill in the parameters:
   - **ExternalId**: Paste the 64-character ID from your ADP dashboard
   - **ADPPlatformAccountId**: Paste the 12-digit account ID from your dashboard
   - **SessionDurationSeconds**: Leave at 3600 (1 hour) unless you need longer runs
   - For **Scoped Write** only:
     - **AllowedServices**: Comma-separated service prefixes (e.g. `s3,lambda,ecs`)
     - **AllowedResourceArns**: Comma-separated ARNs the agent may modify
3. Check the box acknowledging IAM resource creation
4. Click **Create stack**

The stack deploys in under 60 seconds.

## Step 3: Confirm in ADP

1. Go to your ADP dashboard > Settings > AWS Access
2. Click **Verify Connection**
3. ADP will attempt a read-only API call to confirm the role is assumable
4. Once verified, agents can use your AWS account in subsequent runs

## Step 4 (deploy-test only): expand role permissions for full ADP deploy

If you plan to use the **ADP-managed deploy track** to deploy ADP itself into your linked account (not just have agents inspect or modify isolated resources), the default `ReadOnlyAccess` is too narrow — terraform's deploy steps need IAM, EKS, RDS, CloudFront, etc. write permissions.

**Today (manual)**:
1. Open IAM console → Roles → find `ADP-Agent-<your-label>`.
2. Click **Add permissions** → **Attach policies**.
3. Attach `AdministratorAccess` (AWS-managed policy).
4. Save.

This is a temporary requirement. A future PR will extend the CFN template with a `--tier deploy` option that grants scoped permissions on `adp-*` and `bedrockgw-*` resources only, eliminating the need for admin-equivalent access. Tracked in the ADP Platform Roadmap.

**Find your `user_id`**: the ADP-managed deploy needs your ADP `users.id` UUID (not your email or Cognito sub). It's shown on Settings → AWS Access alongside your ExternalId. Save it — you'll paste it into `config/deployment.yml` for deploy-instance issues.

## Changing Permissions

- **Upgrade/downgrade tier**: Delete the existing stack, deploy the new tier's template
- **Change scoped-write resources**: Update the existing stack with new parameter values
- **Revoke access**: Delete the CloudFormation stack (the IAM role is removed immediately)

## Security Details

### Session Tags (CloudTrail Audit)

Every API call made by an ADP agent in your account includes these session tags:

| Tag | Description |
|-----|-------------|
| `adp:tenant_id` | Your organization's unique tenant identifier |
| `adp:agent` | Agent persona that made the call (developer, ops, pm) |
| `adp:run_id` | Unique identifier for the specific agent run |
| `adp:github_issue` | The GitHub issue that triggered the run (e.g. org/repo#42) |
| `adp:actor` | GitHub login of the user who triggered the agent |

Filter CloudTrail events by these tags to audit agent activity:

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=ResourceType,AttributeValue=AWS::IAM::Role \
  --query 'Events[?contains(CloudTrailEvent, `adp:run_id`)].CloudTrailEvent'
```

### ExternalId Protection

The ExternalId prevents [confused deputy attacks](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html).
Your ExternalId is unique to your tenant and never shared with other customers.
If compromised, regenerate it from your ADP dashboard (you'll need to update the stack).

### Credential Lifetime

- Default session: 1 hour
- Maximum configurable: 12 hours
- Credentials are never cached or stored after the agent run completes
- Each run gets fresh credentials via STS AssumeRole

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Access Denied" on verify | ExternalId mismatch | Check the 64-char ID matches exactly |
| "Role does not exist" | Stack not yet complete | Wait for CREATE_COMPLETE status |
| Agent can't write resources | Using read-only tier | Deploy scoped-write or full-admin |
| Scoped-write too restrictive | Missing service/ARN in params | Update stack with additional services |
