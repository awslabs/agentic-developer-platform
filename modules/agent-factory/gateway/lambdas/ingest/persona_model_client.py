"""Producer-authenticated saved-model lookup before publication (no shared key).

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


class ModelSelectionError(Exception):
    """Nothing may be published after an unknown admission outcome."""


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def select_persona_model(envelope, *, user_id):
    """Add the initiating human's selected model to the existing queue contract."""
    started = time.monotonic()
    endpoint = os.environ.get("ADP_AGENT_CONTROL_ENDPOINT", "").rstrip("/")
    match = re.fullmatch(
        r"https://[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/[A-Za-z0-9_-]+(?:/agent)?/internal/v1/agent",
        endpoint,
    )
    if not match:
        raise ModelSelectionError("model selection endpoint unavailable")
    region = match.group(1)
    document = {
        "tenant_id": envelope["tenant_id"],
        "user_id": user_id,
        "persona": envelope.get("persona") or envelope["agent_type"],
        "direct_model": envelope.get("model_requested"),
    }
    data = _canonical(document)
    if len(data) > 4096:
        raise ModelSelectionError("model selection request too large")
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
        url = endpoint + "/persona-model/resolve"
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
        model = receipt["model"]
        if (
            receipt.get("persona") != document["persona"]
            or not receipt.get("principal_id")
            or (
                model is not None
                and (not isinstance(model, str) or not 1 <= len(model) <= 255)
            )
            or receipt.get("source")
            not in {
                "explicit-direct",
                "principal-mapping",
                "system-default",
                "runtime-default",
            }
        ):
            raise ValueError()
        result = {**envelope, "model_selection": receipt}
        if model is not None:
            result["model_resolved"] = model
        return result
    except Exception:
        # Provider bodies and credentials must never appear in errors/logs.
        raise ModelSelectionError(
            "Saved persona model could not be resolved; this run was not dispatched."
        ) from None
