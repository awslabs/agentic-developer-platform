# Issue #488 — URL Analysis Smoke Test Learnings

**Date**: 2026-05-05
**Agent**: @agent-operations
**Issue**: Live URL analysis smoke test blocked by IAM + SDK method name bug

## Key Findings

### AWS Bedrock AgentCore Browser API Method Names

The actual boto3 `bedrock-agentcore` client methods for browser sessions are:
- `start_browser_session` (NOT `create_browser_session`)
- `invoke_browser`
- `stop_browser_session`
- `get_browser_session`
- `list_browser_sessions`
- `save_browser_session_profile`
- `update_browser_stream`

The corresponding IAM actions use PascalCase: `bedrock-agentcore:StartBrowserSession`, etc.

### Required Parameters for `start_browser_session`

```python
response = client.start_browser_session(
    browserIdentifier='aws.browser.v1',  # REQUIRED — identifies the browser runtime
    sessionTimeoutSeconds=300,
    browserSettings={
        'headless': True,
        'persistentProfile': False,
    },
)
```

### IAM Policy for AgentCore Browser

The correct actions to grant:
```json
{
  "Action": [
    "bedrock-agentcore:StartBrowserSession",
    "bedrock-agentcore:InvokeBrowser",
    "bedrock-agentcore:StopBrowserSession",
    "bedrock-agentcore:GetBrowserSession",
    "bedrock-agentcore:ListBrowserSessions"
  ],
  "Resource": "*"
}
```

### CLI usage for verification
```bash
aws bedrock-agentcore list-browser-sessions --browser-identifier aws.browser.v1
```

## Gotchas

1. **Method naming**: AgentCore uses `Start/Stop` not `Create/Delete` for session lifecycle. This is inconsistent with other AWS services (SageMaker uses Create/Delete, for example).

2. **`.claude/skills/` is gitignored** — tracked code lives at `modules/domain-apps/cyber/agent/skills/url-analysis/`. The `.claude/skills/` copy is a symlink or local override that won't be committed.

3. **IAM policy changes require `terraform apply`** — just merging the PR doesn't update the role. The webhook-ingress infra terraform must be applied (either via CI workflow or manually).

4. **The role `adp-dev-agent-scaledjob-role` cannot inspect its own IAM policies** — no `iam:GetRolePolicy` or `iam:ListRolePolicies` permission. Diagnosis must rely on the error messages from the actual service calls.

## What Worked

- Using the actual boto3 client to enumerate available methods (`dir(client)` filtering for `browser`/`session`) immediately revealed the correct method names.
- The `AccessDeniedException` error messages from AWS include the exact action name that was denied, which confirmed the correct IAM action.

## Next Steps After Merge

1. Apply Terraform: trigger `agent-factory-infra-apply` workflow or run manually
2. Re-run live tests: `pytest tests/test_smoke_corpus.py -m live -v`
3. If URLhaus URL (URL 3) has gone offline, fetch fresh from https://urlhaus.abuse.ch/downloads/text_recent/
