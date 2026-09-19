"""Source-authenticated root registration before publication (no shared key).

Vendored unchanged into chat ingest; the packaging parity test guards the copy.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.request

import botocore.auth
import botocore.awsrequest
import botocore.session


class RootRegistrationRefusedError(Exception):
    """Nothing may be published after an unknown admission outcome."""


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def register_model_root(envelope, *, source, subject, instance="", project_id=0):
    """Return the gateway's final canonical queue bytes, never a local policy."""
    started = time.monotonic()
    endpoint = os.environ.get("ADP_AGENT_CONTROL_ENDPOINT", "").rstrip("/")
    match = re.fullmatch(
        r"https://[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/[A-Za-z0-9_-]+(?:/agent)?/internal/v1/agent",
        endpoint,
    )
    if not match:
        raise RootRegistrationRefusedError("model root endpoint unavailable")
    region = match.group(1)
    document = {
        "source": source,
        "envelope": envelope,
        "subject": subject,
        "instance": instance,
        "project_id": project_id,
    }
    data = _canonical(document)
    if len(data) > 65536:
        raise RootRegistrationRefusedError("model root request too large")
    try:
        credentials = botocore.session.get_session().get_credentials()
        if credentials is None:
            raise ValueError()
        frozen = credentials.get_frozen_credentials()
        if not frozen.token:
            raise ValueError()
        proof = botocore.awsrequest.AWSRequest(
            method="POST",
            url=f"https://sts.{region}.amazonaws.com/",
            data="Action=GetCallerIdentity&Version=2011-06-15",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "x-adp-work-invocation": hashlib.sha256(data).hexdigest(),
            },
        )
        botocore.auth.SigV4Auth(frozen, "sts", region).add_auth(proof)
        proof_header = base64.b64encode(
            _canonical({k.lower(): v for k, v in proof.headers.items()})
        ).decode()
        url = endpoint + "/roots/admit"
        signed = botocore.awsrequest.AWSRequest(
            method="POST",
            url=url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Adp-Producer-Proof": proof_header,
            },
        )
        botocore.auth.SigV4Auth(frozen, "execute-api", region).add_auth(signed)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        remaining = 6 - (time.monotonic() - started)
        if remaining <= 0:
            raise ValueError()
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        request = urllib.request.Request(
            url, data=data, headers=dict(signed.headers), method="POST"
        )
        with opener.open(request, timeout=remaining) as response:
            raw = response.read(262145)
            if (
                response.status != 200
                or len(raw) > 262144
                or time.monotonic() - started >= 6
            ):
                raise ValueError()
            receipt = json.loads(raw)
        final_bytes = receipt["envelope_json"]
        final = json.loads(final_bytes)
        if (
            not isinstance(final_bytes, str)
            or not isinstance(final, dict)
            or final != receipt["envelope"]
            or _canonical(final).decode() != final_bytes
            or final.get("message_id") != envelope.get("message_id")
            or not final.get("tenant_id")
            or not final.get("persona")
        ):
            raise ValueError()
        return final_bytes
    except Exception:
        # Provider bodies and credentials must never appear in errors/logs.
        raise RootRegistrationRefusedError(
            "model root registration unavailable"
        ) from None
