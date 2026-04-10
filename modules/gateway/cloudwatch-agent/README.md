# CloudWatch Agent

Automatically creates GitHub issues when errors are detected in CloudWatch Logs. The AI agent then picks up the issue and creates a fix PR.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     CloudWatch → GitHub Issue → Agent                        │
└─────────────────────────────────────────────────────────────────────────────┘

  Your Application                    AWS                           GitHub
  ────────────────                    ───                           ──────

  App logs error ────→ CloudWatch Logs
                              │
                              │ Subscription Filter
                              │ (pattern: ERROR, Exception, etc.)
                              ▼
                       Lambda Function
                       │
                       │ 1. Parse error details
                       │ 2. Fetch log context
                       │ 3. Check deduplication
                       │ 4. Create GitHub issue
                       │
                       ▼
                 GitHub Issue Created ────→ Agent Triggered
                 - Error details              │
                 - Stack trace                │
                 - Log context                ▼
                 - Agent label           Analyzes error
                                         Fixes code
                                         Creates PR
```

## Features

- **Automatic error detection**: Subscribes to CloudWatch Logs and triggers on errors
- **Smart deduplication**: Avoids creating duplicate issues for the same error
- **Configurable mapping**: Map log groups to GitHub repos via tags or config
- **Full context**: Includes error details, stack trace, and surrounding logs
- **Audit trail**: All errors tracked as GitHub issues with fix PRs

## Quick Start

### 1. Deploy Infrastructure

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values

terraform init
terraform apply
```

### 2. Add Subscription to a Log Group

```bash
cd ../scripts
chmod +x *.sh

# Add subscription with repo mapping
./add-subscription.sh /aws/lambda/my-app my-app

# Or just add subscription (uses tag or default repo)
./add-subscription.sh /aws/lambda/my-app
```

### 3. Test It

Generate an error in your application and watch for the GitHub issue.

## Configuration

### Terraform Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `github_org` | GitHub organization name | (required) |
| `github_secret_name` | Secrets Manager secret with GitHub PAT | `github-ccsdk-agent/github-pat` |
| `default_repo` | Default repo if no mapping found | `""` |
| `cooldown_seconds` | Min seconds between issues for same error | `300` |
| `log_group_repo_map` | Map of log groups to repos | `{}` |
| `log_groups` | Log groups to auto-subscribe | `[]` |

### Log Group to Repo Mapping

Three ways to map log groups to GitHub repos:

#### 1. Tags (Recommended)

Tag your log group with `agent:repo`:

```bash
aws logs tag-resource \
  --resource-arn "arn:aws:logs:us-east-1:123456789:log-group:/aws/lambda/my-app" \
  --tags "agent:repo=my-app"
```

Or use the script:

```bash
./add-subscription.sh /aws/lambda/my-app my-app
```

#### 2. Terraform Variable

```hcl
log_group_repo_map = {
  "/aws/lambda/my-app"     = "my-app"
  "/aws/lambda/api-server" = "api-server"
  "my-service"             = "my-service"  # Pattern match
}
```

#### 3. Default Repo

Set `default_repo` as fallback for unmapped log groups.

### Filter Patterns

Customize which log messages trigger issues:

```hcl
# In terraform.tfvars
log_groups = [
  {
    name           = "/aws/lambda/my-app"
    filter_pattern = "?ERROR ?Exception ?FATAL"
  },
  {
    name           = "/aws/lambda/critical-service"
    filter_pattern = "?CRITICAL ?FATAL"  # Only critical errors
  }
]
```

Or via CLI:

```bash
./add-subscription.sh /aws/lambda/my-app my-app "?ERROR ?CRITICAL"
```

## Deduplication

The Lambda uses DynamoDB to prevent duplicate issues:

- Same error within `cooldown_seconds` (default: 5 min) → skipped
- Error hash computed from normalized message (removes timestamps, line numbers)
- TTL of 24 hours on dedup records

## Example Issue Created

```markdown
## 🚨 Production Error Detected

### Error Details

| Field | Value |
|-------|-------|
| Log Group | `/aws/lambda/my-app` |
| Log Stream | `2024/01/15/[$LATEST]abc123` |
| Time | 2024-01-15T10:30:45 |
| Error Hash | `a1b2c3d4e5f6` |

### Error Messages

```
TypeError: Cannot read property 'name' of undefined
    at UserService.getUser (/var/task/src/services/user.js:45:23)
    at async handler (/var/task/src/index.js:12:18)
```

### Instructions for Agent

1. Analyze the error - Understand what went wrong
2. Find the root cause - Locate the problematic code
3. Implement a fix - Make the minimal change
4. Add error handling - Prevent similar errors
5. Add a test - Write a test case
6. Create a PR - Include clear explanation

### CloudWatch Link

[View in CloudWatch](https://console.aws.amazon.com/cloudwatch/...)
```

## Scripts

| Script | Purpose |
|--------|---------|
| `add-subscription.sh` | Add subscription filter to a log group |
| `remove-subscription.sh` | Remove subscription filter |

## Cost

| Component | Cost |
|-----------|------|
| Lambda | ~$0.20 per 1M invocations |
| DynamoDB | Pay per request (minimal) |
| CloudWatch | Subscription filters are free |

## Troubleshooting

### Lambda not triggering

1. Check subscription filter exists:
   ```bash
   aws logs describe-subscription-filters --log-group-name /aws/lambda/my-app
   ```

2. Check Lambda permissions:
   ```bash
   aws lambda get-policy --function-name cloudwatch-agent
   ```

### Issue not created

1. Check Lambda logs:
   ```bash
   aws logs tail /aws/lambda/cloudwatch-agent --follow
   ```

2. Verify GitHub PAT has `repo` scope

3. Check deduplication (same error within cooldown period)

### Wrong repo

1. Check log group tags:
   ```bash
   aws logs list-tags-for-resource --resource-arn "arn:aws:logs:REGION:ACCOUNT:log-group:LOG_GROUP"
   ```

2. Check `log_group_repo_map` in terraform.tfvars

## Integration with Existing Agent

This works with your existing GitHub agent infrastructure:

1. CloudWatch Agent creates issue with `<repo>-agent` label
2. Your existing agent workflow triggers
3. Agent analyzes error, fixes code, creates PR
4. Human reviews and merges

No changes needed to your existing agent code.
