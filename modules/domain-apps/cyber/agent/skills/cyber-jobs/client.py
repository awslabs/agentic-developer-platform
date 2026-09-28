#!/usr/bin/env python3
"""Register/poll Cyber work through authenticated gateway admission, never SQS."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import requests


def call(operation: str, payload: dict) -> dict:
    base = os.environ.get("ADP_AGENT_CONTROL_ENDPOINT", "").rstrip("/")
    match = re.fullmatch(r"https://[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com/[A-Za-z0-9_-]+(?:/agent)?/internal/v1/agent/arc", base)
    if not match:
        raise ValueError("Cyber gateway registration is unavailable")
    region = match[1]
    endpoint = urlsplit(os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""))
    token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if (endpoint.scheme != "https" or endpoint.username or endpoint.password or endpoint.port
        or not re.fullmatch(r"[a-z0-9.-]+\.actions\.githubusercontent\.com", endpoint.hostname or "") or not token):
        raise ValueError("GitHub workflow identity is unavailable")
    query = dict(parse_qsl(endpoint.query))
    query["audience"] = "adp-agent-model-policy"
    endpoint = urlunsplit(endpoint._replace(query=urlencode(query)))
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(endpoint, headers={"Authorization": "Bearer " + token}, timeout=10, allow_redirects=False)
        if response.status_code != 200 or len(response.content) > 20000:
            raise ValueError("GitHub workflow identity refused")
        body = {**payload, "github_oidc_token": response.json()["value"]}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        credentials = boto3.Session().get_credentials().get_frozen_credentials()
        if not credentials.token:
            raise ValueError("Temporary runner identity is required")
        proof = AWSRequest(method="POST", url=f"https://sts.{region}.amazonaws.com/",
                           data="Action=GetCallerIdentity&Version=2011-06-15", headers={
                               "content-type": "application/x-www-form-urlencoded",
                               "x-adp-work-invocation": hashlib.sha256(canonical.encode()).hexdigest(),
                           })
        SigV4Auth(credentials, "sts", region).add_auth(proof)
        proof_headers = {k.lower(): v for k, v in proof.headers.items()}
        url = base + "/cyber/" + operation
        request = AWSRequest(method="POST", url=url, data=canonical, headers={
            "content-type": "application/json",
            "X-Adp-Producer-Proof": base64.b64encode(json.dumps(proof_headers).encode()).decode(),
        })
        SigV4Auth(credentials, "execute-api", region).add_auth(request)
        response = session.post(url, data=canonical, headers=dict(request.headers), timeout=120, allow_redirects=False)
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise ValueError(f"Cyber admission/result refused (HTTP {response.status_code})")
        return response.json()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--artifact-id", required=True)
    submit.add_argument("--sample-uri", required=True)
    submit.add_argument("--stage", choices=["triage", "static"], required=True)
    submit.add_argument("--script", type=Path)
    submit.add_argument("--focus", action="append", default=[])
    submit.add_argument("--yara-rule", action="append", default=[])
    wait = sub.add_parser("wait")
    wait.add_argument("--job-id", required=True)
    args = parser.parse_args()
    if args.command == "submit":
        script = args.script.read_bytes() if args.script else None
        if script is not None and len(script) > 32768:
            raise ValueError("Script exceeds 32 KiB")
        result = call("jobs", {"artifact_id": args.artifact_id, "sample_s3_uri": args.sample_uri, "stage": args.stage,
                               "script_base64": base64.b64encode(script).decode() if script is not None else None,
                               "focus": args.focus, "yara_rules": args.yara_rule})
    else:
        deadline = time.monotonic() + 900
        while True:
            result = call("result", {"job_id": args.job_id})
            if result.get("status") != "pending":
                break
            if time.monotonic() >= deadline:
                raise ValueError("Cyber analysis timed out")
            time.sleep(5)
    print(json.dumps(result))
    if result.get("status") == "failed":
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Request exceptions can contain presigned URLs and credentials.
        print("Cyber operation refused; check gateway registration and job status.", file=sys.stderr)
        sys.exit(1)
