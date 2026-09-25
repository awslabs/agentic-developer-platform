"""IAM-authenticated platform calls with forwarded, verified Task workload proof."""

import base64
import hashlib
import json
import re
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from fastapi import HTTPException
import requests

from adp_tools.contracts import Authorization

PROOF_HEADERS = ("x-adp-run-credential", "x-adp-workload-token")


def require_worker(event, allowed_roles):
    """Only AWS_IAM REST API invocations; caller-supplied identity headers ignored.

    Lambda resource policy must restrict invocation to the configured API/method.
    No Function URL or alternate unauthenticated invocation path is supported.
    """
    arn = event.get("requestContext", {}).get("identity", {}).get("userArn", "")
    match = re.fullmatch(
        r"arn:([a-z0-9-]+):sts::([0-9]{12}):assumed-role/(.+)/([^/]+)", arn
    )
    if not match:
        raise HTTPException(403, "Worker transport refused")
    role = f"arn:{match[1]}:iam::{match[2]}:role/{match[3]}"
    if role not in allowed_roles:
        raise HTTPException(403, "Worker transport refused")


class TaskAuthorityClient:
    def __init__(self, endpoint, headers, *, region, session=None, credentials=None):
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except ValueError:
            raise HTTPException(503, "Task authority endpoint unavailable") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or port not in (None, 443)
            or not parsed.path.endswith("/internal/v1/agent/task")
            or "%" in parsed.path
            or any(p in {".", ".."} for p in parsed.path.split("/"))
        ):
            raise HTTPException(503, "Task authority endpoint unavailable")
        self.endpoint, self.region = endpoint, region
        lower = {k.lower(): v for k, v in headers.items()}
        self.headers = {}
        for name in PROOF_HEADERS:
            value = lower.get(name)
            if not isinstance(value, str) or not value or len(value) > 16384:
                raise HTTPException(403, "Task proof unavailable")
            self.headers[name] = value
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.credentials = credentials

    def close(self):
        self.session.close()

    def post(self, action, body, *, status=200):
        if action not in {"tool-authorize", "artifact"}:
            raise ValueError("Unsupported platform operation")
        url = self.endpoint + "/" + action
        data = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        credentials = self.credentials or boto3.Session().get_credentials()
        if credentials is None:
            raise HTTPException(503, "Tool service transport unavailable")
        request = AWSRequest(
            method="POST",
            url=url,
            data=data,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        SigV4Auth(
            credentials.get_frozen_credentials(), "execute-api", self.region
        ).add_auth(request)
        try:
            # Deliberately no retry adapter or redirects: a lost artifact receipt
            # must not be translated into a fresh untracked operation.
            with self.session.post(
                url,
                data=data,
                headers=dict(request.headers),
                timeout=(3, 8),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != status:
                    raise HTTPException(
                        403 if response.status_code in (401, 403, 404) else 503,
                        "Task authority refused operation",
                    )
                raw = response.raw.read(131073, decode_content=True)
                if len(raw) > 131072:
                    raise HTTPException(503, "Task authority response exceeds bound")
                return json.loads(raw)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Task authority outcome unavailable") from None

    def authorize(self, *, attempt, tool, cleanup=False):
        response = self.post(
            "tool-authorize",
            {
                "schema_version": "1.0",
                "attempt": attempt,
                "tool": tool,
                "cleanup": cleanup,
            },
        )
        try:
            verified = Authorization.model_validate(response)
            expected = {
                **attempt["run"],
                "runtime_attempt_id": attempt["runtime_attempt_id"],
            }
            if any(
                getattr(verified.identity, key) != value
                for key, value in expected.items()
            ):
                raise ValueError("Attempt mismatch")
            if verified.task.get("scope") != {
                "tenant": verified.identity.tenant,
                "canonical_principal": verified.identity.canonical_principal,
            }:
                raise ValueError("Scope mismatch")
            return verified
        except Exception:
            raise HTTPException(503, "Task authority binding unavailable") from None

    def put_run_artifact(self, *, attempt, content, content_type, digest):
        if digest != hashlib.sha256(content).hexdigest():
            raise HTTPException(422, "Artifact content digest differs")
        expected_id = "art_" + str(
            uuid.UUID(
                bytes=hashlib.sha256(
                    f"{attempt.task_id}:{content_type}:{digest}".encode()
                ).digest()[:16],
                version=4,
            )
        )
        response = self.post(
            "artifact",
            {
                "schema_version": "1.0",
                "run": {
                    k: getattr(attempt, k)
                    for k in ("task_id", "invocation_id", "generation")
                },
                "content_type": content_type,
                "content_sha256": digest,
                "content_base64": base64.b64encode(content).decode(),
            },
            status=201,
        )
        if (
            response.get("schema_version") != "1.0"
            or response.get("content_sha256") != hashlib.sha256(content).hexdigest()
            or response.get("content_type") != content_type
            or response.get("artifact_id") != expected_id
            or type(response.get("version")) is not int
            or response["version"] != 1
            or response.get("expires_at", "missing") is not None
        ):
            raise HTTPException(503, "Artifact binding unavailable")
        return SimpleNamespace(
            **{
                k: response[k]
                for k in ("artifact_id", "content_type", "content_sha256")
            }
        )


class TaskHostAuthority(TaskAuthorityClient):
    """Same authority/artifact contract using the trusted worker's transport."""

    def __init__(self, post):
        self.host_post = post

    def post(self, action, body, *, status=200):
        return self.host_post(action, body)

    def close(self):
        pass
