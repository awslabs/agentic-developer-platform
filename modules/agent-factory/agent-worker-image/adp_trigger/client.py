"""HTTP client for adp-trigger — SigV4-signed POST to /agent/trigger.

Reads lineage context from the pod environment and constructs the request
body expected by the agent_trigger Lambda handler (Issue #2152).

Environment variables (all required — set by entrypoint.py from the SQS envelope):
  ADP_CORRELATION_ID  — current chain's correlation ID
  ADP_MESSAGE_ID      — this run's invocation ID (becomes parent_invocation_id)
  ADP_CHAIN_DEPTH     — current depth in the chain (informational)
  ADP_TRIGGER_ENDPOINT — full URL of the /agent/trigger route

Optional:
  AWS_REGION — defaults to us-east-1
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def get_config() -> dict:
    """Read and validate required environment variables.

    Returns a dict with keys: correlation_id, message_id, chain_depth,
    trigger_endpoint.

    Exits with code 2 if any required variable is missing.
    """
    required = {
        "ADP_CORRELATION_ID": "lineage context (set by entrypoint from SQS envelope)",
        "ADP_MESSAGE_ID": "this run's invocation ID (set by entrypoint from SQS envelope)",
        "ADP_CHAIN_DEPTH": "chain depth (set by entrypoint from SQS envelope)",
        "ADP_TRIGGER_ENDPOINT": "trigger API URL (set by ScaledJob pod spec)",
    }

    missing = []
    for var, desc in required.items():
        if not os.environ.get(var):
            missing.append(f"{var} ({desc})")

    if missing:
        print(
            "error: adp-trigger must run inside an agent pod. "
            "Missing environment variables:\n  " + "\n  ".join(missing),
            file=sys.stderr,
        )
        sys.exit(2)

    return {
        "correlation_id": os.environ["ADP_CORRELATION_ID"],
        "message_id": os.environ["ADP_MESSAGE_ID"],
        "chain_depth": os.environ["ADP_CHAIN_DEPTH"],
        "trigger_endpoint": os.environ["ADP_TRIGGER_ENDPOINT"],
    }


def build_body(persona: str, issue: int, repo: str, reason: str | None = None) -> dict:
    """Construct the request body for POST /agent/trigger.

    Uses lineage context from the pod environment. The agent never handles
    trust values — those are server-resolved from the chain record.
    """
    config = get_config()

    body = {
        "correlation_id": config["correlation_id"],
        "parent_invocation_id": config["message_id"],
        "persona": persona,
        "target": {
            "repo": repo,
            "issue": issue,
        },
    }

    if reason:
        body["reason"] = reason

    return body


def send_trigger(body: dict) -> dict:
    """SigV4-sign and POST the trigger request.

    Returns the parsed JSON response body on success (202).
    Exits with a non-zero code on failure.
    """
    try:
        import botocore.auth
        import botocore.awsrequest
        import botocore.session
    except ImportError:
        print(
            "error: botocore is required for SigV4 auth. Install boto3.",
            file=sys.stderr,
        )
        sys.exit(1)

    endpoint = os.environ["ADP_TRIGGER_ENDPOINT"]
    region = os.environ.get("AWS_REGION", "us-east-1")

    # Get IRSA credentials from the pod's service account
    session = botocore.session.get_session()
    credentials = session.get_credentials()
    if credentials is None:
        print(
            "error: no AWS credentials available for SigV4 signing. "
            "Ensure the pod has an IRSA-annotated service account.",
            file=sys.stderr,
        )
        sys.exit(1)
    credentials = credentials.get_frozen_credentials()

    # Build and sign the request
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}

    aws_request = botocore.awsrequest.AWSRequest(
        method="POST",
        url=endpoint,
        headers=headers,
        data=data,
    )

    signer = botocore.auth.SigV4Auth(credentials, "execute-api", region)
    signer.add_auth(aws_request)

    # Execute the request
    signed_headers = dict(aws_request.headers)
    req = Request(endpoint, data=data, headers=signed_headers, method="POST")

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        error_body = exc.read().decode() if exc.fp else ""
        try:
            error_json = json.loads(error_body)
            detail = error_json.get("detail", error_json.get("error", error_body))
        except (json.JSONDecodeError, ValueError):
            detail = error_body
        print(f"error: trigger endpoint returned {exc.code}: {detail}", file=sys.stderr)
        sys.exit(1)
    except URLError as exc:
        print(f"error: cannot reach trigger endpoint: {exc.reason}", file=sys.stderr)
        sys.exit(1)
