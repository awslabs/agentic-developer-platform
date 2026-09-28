"""IAM transport owned by one executor attempt, separate from the API singleton.

The executor composition supplies its admitted RunBinding and projected token
files. Tokens and AWS credentials are read on every request so rotation does not
leave a singleton holding expired identity. API Gateway verifies SigV4 and adds
X-Caller-Identity; this client never asserts that trusted header itself.
"""

import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

import botocore.auth
import botocore.awsrequest
import botocore.session
import httpx
from superplane_contracts.delivery import DeliveryRefused, RunBinding


def _read_token(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise DeliveryRefused("executor identity unavailable")
        raw = source.read(8194)
    token = raw.decode("ascii").rstrip("\r\n")
    if not 1 <= len(token) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise DeliveryRefused("executor identity unavailable")
    return token


class ExecutorVaultTransport:
    """Construct inside the trusted executor for one admitted attempt.

    ``endpoint`` is the API Gateway invoke URL, including its stage, not a
    ClusterIP or public UI URL. Custom domains need an explicit signing region.
    No runtime authority or resource is provisioned by this class.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        binding: RunBinding,
        run_credential_file: Path,
        workload_token_file: Path,
        region: str = "",
        session=None,
        client_factory=None,
    ):
        url = urlsplit(endpoint)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("executor delivery requires an HTTPS API Gateway endpoint")
        match = re.fullmatch(
            r"[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?",
            url.hostname,
        )
        self._region = region or (match.group(1) if match else "")
        if not self._region or not binding.job_id or not binding.attempt_id:
            raise ValueError(
                "executor delivery requires signing region and admitted job/attempt"
            )
        self._endpoint = endpoint.rstrip("/")
        self._binding = binding
        self._run_file = run_credential_file
        self._workload_file = workload_token_file
        self._session = session or botocore.session.get_session()
        self._client_factory = client_factory

    def post(self, lease, payload):
        return self._request(lease, payload, "/internal/v1/credential-delivery")

    def preflight(self, lease, payload):
        return self._request(
            lease, payload, "/internal/v1/credential-delivery/preflight"
        )

    def _request(self, lease, payload, path):
        if lease.binding != self._binding:
            raise DeliveryRefused("executor attempt mismatch")
        url = self._endpoint + path
        data = json.dumps(dict(payload), separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Adp-Run-Credential": _read_token(self._run_file),
            "X-Adp-Workload-Token": _read_token(self._workload_file),
        }
        credentials = self._session.get_credentials()
        if credentials is None:
            raise DeliveryRefused("executor IAM identity unavailable")
        request = botocore.awsrequest.AWSRequest(
            method="POST", url=url, data=data, headers=headers
        )
        botocore.auth.SigV4Auth(
            credentials.get_frozen_credentials(), "execute-api", self._region
        ).add_auth(request)
        client_cm = (
            self._client_factory()
            if self._client_factory
            else httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        )
        with client_cm as client:
            return client.post(url, content=data, headers=dict(request.headers))
