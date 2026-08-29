# AWS Security Agent — CLI Runbook

A fully scriptable, console-free procedure for running **AWS Security Agent**
against this platform: static **code review** first (Phase 1), then on-demand
**penetration testing** (Phase 2). Every step below is a CLI call — the console
is only a wrapper over the same API.

> **Placeholders.** Replace every `<ANGLE_BRACKET>` token with your own value.
> `111122223333` is a stand-in AWS account ID. Nothing in this doc is tied to a
> specific deployment, region, bucket, or identity — supply your own.

---

## 0. Prerequisites

- **AWS CLI v2 that includes the `securityagent` service.** The service GA'd in
  2026; older CLI builds won't have it. Verify:
  ```bash
  aws --version
  aws securityagent help | head -5          # should print the service description
  ```
  If `securityagent` is an invalid choice, upgrade the CLI. On macOS installed
  via the official pkg (not Homebrew), re-run the official installer:
  ```bash
  curl -fsSL "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o /tmp/AWSCLIV2.pkg
  sudo installer -pkg /tmp/AWSCLIV2.pkg -target /
  ```
  If a corporate pin blocks the upgrade, drive the API with a current `boto3`
  (`boto3.client("securityagent")`) or raw SigV4 calls instead.
- **Credentials** for the target account with permission to call `securityagent:*`,
  create an IAM role, and read/write the staging S3 bucket.
- **Region** where AWS Security Agent is available (e.g. `us-east-1`).

Convenience exports used throughout:
```bash
export AWS_PROFILE=<PROFILE>
export AWS_REGION=<REGION>
export AWS_PAGER=""            # don't page JSON output
```

---

## 1. Model shape — commands & enums (2025-09-06 API)

The full lifecycle, in order of use:

| Stage | Command |
|---|---|
| Workspace | `create-agent-space`, `update-agent-space`, `list-agent-spaces` |
| Target (pentest) | `create-target-domain`, `verify-target-domain`, `list-target-domains` |
| Connect repos/docs | `initiate-provider-registration`, `create-integration`, `add-artifact` |
| Private networking | `create-private-connection`, `describe-private-connection` |
| Code review | `create-code-review`, `start-code-review-job`, `stop-code-review-job` |
| Pentest | `create-pentest`, `start-pentest-job`, `stop-pentest-job`, `update-pentest` |
| Results | `list-findings`, `batch-get-findings`, `update-finding` |
| Remediation | `start-code-remediation` |
| Revalidate | `start-pentest-job --job-type REVALIDATION` |

Key enum values (introspect any command with `--generate-cli-skeleton` or
`<command> help`):

- **`verificationMethod`** (target domain): `DNS_TXT` | `HTTP_ROUTE` | `PRIVATE_VPC`
- **actor `authentication.providerType`**: `SECRETS_MANAGER` | `AWS_LAMBDA` | `AWS_IAM_ROLE` | `AWS_INTERNAL`
- **`validationMode`** (code review): `DISABLED` (pure static) | `SIMULATED` (exercises findings against live endpoints)
- **`codeRemediationStrategy`**: `AUTOMATIC` (opens fix PRs — needs a connected repo) | `DISABLED`
- **`start-pentest-job --job-type`**: `FULL` | `REVALIDATION`
- **`excludeRiskTypes`** (denylist — leave empty to test everything): includes
  `PRIVILEGE_ESCALATION`, `INSECURE_DIRECT_OBJECT_REFERENCE`,
  `SERVER_SIDE_REQUEST_FORGERY`, `JSON_WEB_TOKEN_VULNERABILITIES`,
  `SQL_INJECTION`, `COMMAND_INJECTION`, `SERVER_SIDE_TEMPLATE_INJECTION`,
  `PATH_TRAVERSAL`, `INSECURE_DESERIALIZATION`, and more.

---

## 2. Gotchas (learned the hard way)

1. **`title`** accepts only letters, numbers, hyphens, and underscores (≤100
   chars). No spaces or colons.
2. **`serviceRole` is effectively required** for `create-code-review` (the
   synopsis shows it optional; the API rejects the call without it).
3. **The service role must be pre-registered on the agent-space** via
   `update-agent-space` → `awsResources.iamRoles`, or you get
   `"Service role ... not found in agent instance IAM roles"`. Register the
   staging bucket the same way (`awsResources.s3Buckets`).
4. **`update-agent-space` requires `name`** even when you're only changing
   `awsResources` — pass the existing name back.
5. **Source code must be a `.zip`**, not `.tar.gz`, despite the generic
   `s3Location` field name.
6. **Domain verification is mandatory for pentests** — you can't attack a target
   you haven't proven you own. `PRIVATE_VPC` avoids needing a public DNS record.

---

## 3. Service role (one-time)

Trust policy — principal is the service:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "securityagent.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
```

Permissions policy — read the source zip + write logs (add Secrets/Lambda/VPC
permissions for pentests that authenticate as actors):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ReadSource", "Effect": "Allow",
      "Action": ["s3:GetObject","s3:GetObjectVersion"],
      "Resource": "arn:aws:s3:::<SOURCE_BUCKET>/*" },
    { "Sid": "ListBucket", "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::<SOURCE_BUCKET>" },
    { "Sid": "Logs", "Effect": "Allow",
      "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams"],
      "Resource": "arn:aws:logs:<REGION>:111122223333:log-group:/aws/securityagent/*" }
  ]
}
```

```bash
aws iam create-role --role-name <SA_ROLE_NAME> \
  --assume-role-policy-document file://trust.json
aws iam put-role-policy --role-name <SA_ROLE_NAME> \
  --policy-name codereview-s3-logs --policy-document file://perms.json
```

---

## 4. Phase 1 — static code review (fastest, cheapest, no browser)

Feed code either by **S3 zip upload** (no GitHub handshake — used here) or by a
**GitHub integration** (enables branch/diff review + auto-remediation PRs; see
Phase 3).

```bash
# 4.1 Create the workspace (one-time). Save the returned agentSpaceId.
aws securityagent create-agent-space --name <SPACE_NAME>
# -> agentSpaceId: as-xxxxxxxx...

# 4.2 Register the role + staging bucket + enable code-review scanning.
#     NOTE: --name is required here too.
cat > update-space.json <<JSON
{
  "agentSpaceId": "<AGENT_SPACE_ID>",
  "name": "<SPACE_NAME>",
  "awsResources": {
    "iamRoles":  [ "arn:aws:iam::111122223333:role/<SA_ROLE_NAME>" ],
    "s3Buckets": [ "arn:aws:s3:::<SOURCE_BUCKET>" ]
  },
  "codeReviewSettings": { "controlsScanning": true, "generalPurposeScanning": true }
}
JSON
aws securityagent update-agent-space --cli-input-json file://update-space.json

# 4.3 Package the module as a ZIP (exclude deps/build artifacts) and upload.
cd <REPO_ROOT>/<PATH_TO_MODULE_PARENT>
zip -rq /tmp/src.zip <MODULE_DIR> \
  -x '<MODULE_DIR>/.venv/*' '<MODULE_DIR>/**/node_modules/*' \
     '<MODULE_DIR>/**/dist/*' '<MODULE_DIR>/**/__pycache__/*' \
     '<MODULE_DIR>/**/*.pyc' '<MODULE_DIR>/**/.terraform/*' \
     '<MODULE_DIR>/**/*.tfstate*' '<MODULE_DIR>/**/build/*'
aws s3 cp /tmp/src.zip s3://<SOURCE_BUCKET>/<KEY>.zip

# 4.4 Create the review. validationMode DISABLED = pure static (no live target).
cat > code-review.json <<JSON
{
  "title": "<TITLE_HYPHENS_ONLY>",
  "agentSpaceId": "<AGENT_SPACE_ID>",
  "assets": { "sourceCode": [ { "s3Location": "s3://<SOURCE_BUCKET>/<KEY>.zip" } ] },
  "serviceRole": "arn:aws:iam::111122223333:role/<SA_ROLE_NAME>",
  "validationMode": "DISABLED",
  "codeRemediationStrategy": "DISABLED"
}
JSON
aws securityagent create-code-review --cli-input-json file://code-review.json
# -> codeReviewId: cr-xxxxxxxx...

# 4.5 Start the job (metered). Omit --diff-source to review the whole zip.
aws securityagent start-code-review-job \
  --agent-space-id <AGENT_SPACE_ID> --code-review-id <CODE_REVIEW_ID>
# -> codeReviewJobId: cj-xxxxxxxx...
```

Poll until terminal (`COMPLETED` | `FAILED` | `STOPPED`):
```bash
aws securityagent batch-get-code-review-jobs \
  --code-review-job-ids <CODE_REVIEW_JOB_ID> \
  --query 'codeReviewJobs[0].status' --output text
```

Read findings:
```bash
aws securityagent list-findings \
  --query 'findings[].{id:findingId,risk:riskType,sev:severity,title:title}' --output table
aws securityagent batch-get-findings --finding-ids <ID> <ID>
```

Stop early if needed: `aws securityagent stop-code-review-job --code-review-job-id <ID>`.

---

## 5. Phase 2 — on-demand penetration test

Adds a **verified target** and a **multi-role credential matrix** (one actor per
role) so the agent can attempt horizontal (cross-tenant) and vertical
(privilege-escalation) attacks.

```bash
# 5.1 Register + verify the target. PRIVATE_VPC skips public DNS.
aws securityagent create-target-domain \
  --target-domain-name <TARGET_HOST> --verification-method PRIVATE_VPC
# -> targetDomainId (+ verification instructions)
aws securityagent verify-target-domain --target-domain-id <TARGET_DOMAIN_ID>

# 5.2 (If target is VPC-internal) stand up a private connection and reference it.
aws securityagent create-private-connection --cli-input-json file://private-conn.json

# 5.3 Create the pentest. actors[] is the role matrix; leave excludeRiskTypes empty.
aws securityagent create-pentest --cli-input-json file://pentest.json
# -> pentestId
aws securityagent start-pentest-job \
  --agent-space-id <AGENT_SPACE_ID> --pentest-id <PENTEST_ID> --job-type FULL
```

`pentest.json` (one actor per role — this is what proves scoping/escalation):
```json
{
  "title": "<TITLE_HYPHENS_ONLY>",
  "agentSpaceId": "<AGENT_SPACE_ID>",
  "assets": {
    "endpoints": [ { "uri": "https://<TARGET_HOST>/<BASE_PATH>" } ],
    "actors": [
      { "identifier": "platform_admin", "authentication": { "providerType": "AWS_LAMBDA", "value": "<TOKEN_LAMBDA_ARN>" } },
      { "identifier": "org_admin_a",     "authentication": { "providerType": "AWS_LAMBDA", "value": "<TOKEN_LAMBDA_ARN>" } },
      { "identifier": "org_admin_b",     "authentication": { "providerType": "AWS_LAMBDA", "value": "<TOKEN_LAMBDA_ARN>" } },
      { "identifier": "regular_user",    "authentication": { "providerType": "AWS_LAMBDA", "value": "<TOKEN_LAMBDA_ARN>" } }
    ]
  },
  "excludeRiskTypes": [],
  "serviceRole": "arn:aws:iam::111122223333:role/<SA_ROLE_NAME>",
  "maxTaskHours": 20
}
```

- **Two org admins in different orgs** → cross-tenant/IDOR coverage.
- **org_admin vs platform_admin** → privilege-escalation-ceiling coverage.
- **Actor auth**: `AWS_LAMBDA` points at a small function that mints a fresh
  bearer/ID token per request (the cleanest option for token-based auth with no
  static secret to rotate). `SECRETS_MANAGER` points at a secret holding
  credentials the agent replays through the login flow.

After a fix ships, recheck just the affected findings (first few revalidations
per finding are free):
```bash
aws securityagent start-pentest-job --agent-space-id <AGENT_SPACE_ID> \
  --pentest-id <PENTEST_ID> --job-type REVALIDATION --selected-finding-ids <ID>
```

---

## 6. Phase 3 — GitHub integration & auto-remediation (optional)

Enables branch/diff review and lets the agent open fix PRs
(`codeRemediationStrategy: AUTOMATIC`). Costs **one browser click** to authorize
the App install:

```bash
# Returns a redirect URL + CSRF state; open the URL to authorize the App install.
aws securityagent initiate-provider-registration --provider GITHUB

# Complete with the code/state/installationId GitHub hands back.
aws securityagent create-integration --cli-input-json file://integration.json
```
Then reference the connected repo via `assets.integratedRepositories[]`
(`integrationId` + `providerResourceId` + `branch`) on the code review or
pentest, and use `start-code-remediation` to open fix PRs from findings.

---

## 7. Cost & safety notes

- Pentest work is metered per task-hour (`maxTaskHours` sets the ceiling; there
  is a per-run minimum). Set `maxTaskHours` explicitly to bound spend. A launch
  free-trial window and a small number of free revalidations per finding may
  apply — confirm current pricing/terms before a large run.
- Any job can be aborted mid-run (`stop-code-review-job` / `stop-pentest-job`).
- `validationMode: DISABLED` and Phase-1 static review never touch a live
  endpoint. Live exercise only happens in a pentest or with
  `validationMode: SIMULATED`.
- Only run against systems you are authorized to test, and route any external
  disclosure of findings through your organization's security-outreach process.

---

## 8. Teardown

```bash
aws securityagent delete-agent-space --agent-space-id <AGENT_SPACE_ID>
aws iam delete-role-policy --role-name <SA_ROLE_NAME> --policy-name codereview-s3-logs
aws iam delete-role --role-name <SA_ROLE_NAME>
aws s3 rm s3://<SOURCE_BUCKET>/<KEY>.zip
```
