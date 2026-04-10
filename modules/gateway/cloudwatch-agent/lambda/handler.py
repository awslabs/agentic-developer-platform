"""
CloudWatch Agent: Creates GitHub issues from CloudWatch log errors.

When an error is detected in CloudWatch Logs, this Lambda:
1. Parses the error details
2. Fetches surrounding log context
3. Creates a GitHub issue with the agent label
4. Deduplicates to avoid spam

Configuration via environment variables:
- GITHUB_SECRET_NAME: Secrets Manager secret with GitHub PAT
- GITHUB_ORG: GitHub organization name
- DEFAULT_REPO: Default repo if not mapped
- COOLDOWN_SECONDS: Minimum time between issues for same error (default: 300)
- LOG_GROUP_REPO_MAP: JSON mapping of log groups to repos (optional)
"""

import base64
import gzip
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Initialize clients
secrets_client = boto3.client('secretsmanager')
logs_client = boto3.client('logs')
dynamodb = boto3.resource('dynamodb')

# Environment variables
GITHUB_SECRET_NAME = os.environ.get('GITHUB_SECRET_NAME', 'github-ccsdk-agent/github-pat')
GITHUB_ORG = os.environ.get('GITHUB_ORG', '')
DEFAULT_REPO = os.environ.get('DEFAULT_REPO', '')
COOLDOWN_SECONDS = int(os.environ.get('COOLDOWN_SECONDS', '300'))
DEDUP_TABLE = os.environ.get('DEDUP_TABLE', '')
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')

# Log group to repo mapping (can be overridden by tags)
LOG_GROUP_REPO_MAP = json.loads(os.environ.get('LOG_GROUP_REPO_MAP', '{}'))


def get_github_token() -> str:
    """Retrieve GitHub PAT from Secrets Manager."""
    response = secrets_client.get_secret_value(SecretId=GITHUB_SECRET_NAME)
    secret = json.loads(response['SecretString'])
    return secret.get('token') or secret.get('pat') or response['SecretString']


def get_repo_for_log_group(log_group_name: str) -> str:
    """
    Determine which GitHub repo corresponds to a log group.

    Priority:
    1. Log group tags (agent:repo tag)
    2. LOG_GROUP_REPO_MAP environment variable
    3. DEFAULT_REPO
    """
    # Try to get from log group tags
    try:
        response = logs_client.list_tags_for_resource(
            resourceArn=f"arn:aws:logs:{AWS_REGION}:{get_account_id()}:log-group:{log_group_name}"
        )
        tags = response.get('tags', {})
        if 'agent:repo' in tags:
            return tags['agent:repo']
        if 'agent-repo' in tags:
            return tags['agent-repo']
    except ClientError as e:
        print(f"Could not get tags for log group: {e}")

    # Try environment variable mapping
    if log_group_name in LOG_GROUP_REPO_MAP:
        return LOG_GROUP_REPO_MAP[log_group_name]

    # Try pattern matching (e.g., /aws/lambda/my-app -> my-app)
    for pattern, repo in LOG_GROUP_REPO_MAP.items():
        if pattern in log_group_name:
            return repo

    return DEFAULT_REPO


def get_account_id() -> str:
    """Get AWS account ID."""
    return boto3.client('sts').get_caller_identity()['Account']


def get_log_context(
    log_group_name: str, log_stream_name: str, timestamp: int, context_lines: int = 50
) -> str:
    """Fetch log lines around the error for context."""
    try:
        response = logs_client.get_log_events(
            logGroupName=log_group_name,
            logStreamName=log_stream_name,
            startTime=timestamp - 60000,  # 1 minute before
            endTime=timestamp + 60000,  # 1 minute after
            limit=context_lines * 2,
        )
        events = response.get('events', [])
        return '\n'.join([e['message'] for e in events])
    except ClientError as e:
        print(f"Could not fetch log context: {e}")
        return "Could not fetch additional log context."


def extract_error_type(error_message: str) -> str:
    """Extract a short error type from the message."""
    # Common patterns
    patterns = [
        ('TypeError', 'TypeError'),
        ('ValueError', 'ValueError'),
        ('KeyError', 'KeyError'),
        ('AttributeError', 'AttributeError'),
        ('ImportError', 'ImportError'),
        ('RuntimeError', 'RuntimeError'),
        ('ConnectionError', 'ConnectionError'),
        ('TimeoutError', 'TimeoutError'),
        ('NullPointerException', 'NullPointerException'),
        ('OutOfMemoryError', 'OutOfMemoryError'),
        ('FATAL', 'Fatal Error'),
        ('CRITICAL', 'Critical Error'),
        ('Exception', 'Exception'),
        ('Error', 'Error'),
    ]

    for pattern, label in patterns:
        if pattern in error_message:
            return label

    # Return first 50 chars if no pattern matches
    return error_message[:50].replace('\n', ' ')


def compute_error_hash(error_message: str) -> str:
    """Compute a hash for deduplication."""
    # Normalize: remove timestamps, line numbers, memory addresses
    normalized = re.sub(r'\d{4}-\d{2}-\d{2}', '', error_message)
    normalized = re.sub(r'\d{2}:\d{2}:\d{2}', '', normalized)
    normalized = re.sub(r'0x[0-9a-fA-F]+', '', normalized)
    normalized = re.sub(r'line \d+', 'line X', normalized)

    return hashlib.sha256(normalized[:500].encode()).hexdigest()[:12]


def is_duplicate(error_hash: str, repo_name: str) -> bool:
    """Check if this error was recently reported."""
    if not DEDUP_TABLE:
        return False

    try:
        table = dynamodb.Table(DEDUP_TABLE)
        response = table.get_item(Key={'error_hash': error_hash, 'repo': repo_name})

        if 'Item' in response:
            last_time = response['Item'].get('timestamp', 0)
            if time.time() - last_time < COOLDOWN_SECONDS:
                return True

        # Update timestamp
        table.put_item(
            Item={
                'error_hash': error_hash,
                'repo': repo_name,
                'timestamp': int(time.time()),
                'ttl': int(time.time()) + 86400,  # 24 hour TTL
            }
        )
        return False
    except ClientError as e:
        print(f"Dedup check failed: {e}")
        return False


def create_github_issue(
    repo_name: str, title: str, body: str, labels: list[str]
) -> dict[str, Any]:
    """Create a GitHub issue using the REST API."""
    token = get_github_token()
    url = f"https://api.github.com/repos/{GITHUB_ORG}/{repo_name}/issues"

    data = json.dumps({'title': title, 'body': body, 'labels': labels}).encode('utf-8')

    request = urllib.request.Request(url, data=data, method='POST')
    request.add_header('Authorization', f'Bearer {token}')
    request.add_header('Accept', 'application/vnd.github+json')
    request.add_header('Content-Type', 'application/json')
    request.add_header('X-GitHub-Api-Version', '2022-11-28')

    try:
        with urllib.request.urlopen(request) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"GitHub API error: {e.code} - {error_body}")
        raise


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler for CloudWatch Logs subscription."""
    
    # Decode CloudWatch Logs data
    payload = base64.b64decode(event['awslogs']['data'])
    log_data = json.loads(gzip.decompress(payload))
    
    log_group = log_data['logGroup']
    log_stream = log_data['logStream']
    log_events = log_data['logEvents']
    
    print(f"Processing {len(log_events)} events from {log_group}/{log_stream}")
    
    # Get repo for this log group
    repo_name = get_repo_for_log_group(log_group)
    if not repo_name:
        print(f"No repo configured for log group: {log_group}")
        return {'statusCode': 200, 'body': 'No repo configured'}
    
    # Combine error messages
    errors = '\n'.join([e['message'] for e in log_events])
    first_timestamp = log_events[0]['timestamp']
    
    # Compute hash for deduplication
    error_hash = compute_error_hash(errors)
    
    # Check for duplicate
    if is_duplicate(error_hash, repo_name):
        print(f"Duplicate error (hash: {error_hash}), skipping")
        return {'statusCode': 200, 'body': 'Duplicate, skipped'}
    
    # Get log context
    log_context = get_log_context(log_group, log_stream, first_timestamp)
    
    # Format timestamp
    error_time = datetime.fromtimestamp(first_timestamp / 1000).isoformat()
    
    # Build CloudWatch URL
    encoded_log_group = urllib.parse.quote(log_group, safe='')
    encoded_log_stream = urllib.parse.quote(log_stream, safe='')
    cloudwatch_url = (
        f"https://{AWS_REGION}.console.aws.amazon.com/cloudwatch/home?"
        f"region={AWS_REGION}#logsV2:log-groups/log-group/{encoded_log_group}"
        f"/log-events/{encoded_log_stream}"
    )
    
    # Create issue body
    issue_body = f"""## 🚨 Production Error Detected

### Error Details

| Field | Value |
|-------|-------|
| Log Group | `{log_group}` |
| Log Stream | `{log_stream}` |
| Time | {error_time} |
| Error Hash | `{error_hash}` |

### Error Messages

```
{errors[:5000]}
```

### Log Context

```
{log_context[:10000]}
```

### Instructions for Agent

You are fixing a production error. Follow these steps:

1. **Analyze the error** - Understand what went wrong from the stack trace
2. **Find the root cause** - Locate the problematic code in the repository
3. **Implement a fix** - Make the minimal change to fix the error
4. **Add error handling** - Prevent similar errors with proper error handling
5. **Add a test** - Write a test case that would catch this error
6. **Create a PR** - Include clear explanation of the fix

### CloudWatch Link

[View in CloudWatch]({cloudwatch_url})

### Success Criteria

- [ ] Error is fixed and won't recur
- [ ] Proper error handling added
- [ ] Test case added to prevent regression
- [ ] No new errors introduced
- [ ] PR includes clear explanation
"""

    # Determine agent label
    repo_name_lower = repo_name.lower().replace('_', '-')
    agent_label = f"{repo_name_lower}-agent"
    
    # Extract error type for title
    error_type = extract_error_type(errors)
    title = f"🚨 Production Error: {error_type}"
    
    # Create the issue
    try:
        issue = create_github_issue(
            repo_name=repo_name,
            title=title,
            body=issue_body,
            labels=[agent_label, 'production-error', 'auto-generated'],
        )
        print(f"Created issue: {issue['html_url']}")
        return {'statusCode': 200, 'body': json.dumps({'issue_url': issue['html_url']})}
    except (urllib.error.HTTPError, urllib.error.URLError, ClientError) as e:
        print(f"Failed to create issue: {e}")
        return {'statusCode': 500, 'body': str(e)}
