"""Exchange an authorized native-space selection for a bounded AWS console handoff."""

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit

from fastapi import HTTPException

from l5_services.adapters.devops.spaces import SpaceSelection


def _role_account(role_arn: str, account_id: str) -> bool:
    return isinstance(role_arn, str) and bool(re.fullmatch(
        rf"arn:aws:iam::{account_id}:role/[A-Za-z0-9_+=,.@/-]{{1,128}}", role_arn,
    ))


def launcher_session_policy(selection: SpaceSelection, native_space_id: str, expires_at: datetime) -> dict:
    """Narrow even an overprivileged saved launcher role to this one handoff.

    These three actions support an exact agentspace ARN in the AWS service
    authorization reference (list_devops-agent.html). ListAgentSpaces requires
    Resource '*', so it is deliberately absent. The direct console launch must
    be qualified by a human with this policy; the POC's list-all permission is
    not evidence that a deep link needs broader access.
    """
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow",
        "Action": ["aidevops:CreateOneTimeLoginSession", "aidevops:GetAgentSpace", "aidevops:GetOperatorApp"],
        "Resource": f"arn:aws:aidevops:{selection.region}:{selection.account_id}:agentspace/{native_space_id}",
        "Condition": {"StringEquals": {"aws:RequestedRegion": selection.region},
                      "DateLessThan": {"aws:CurrentTime": expires_at.isoformat().replace("+00:00", "Z")}},
    }]}


async def verify_operator_app(operator_client, native_space_id: str, operator_role_arn: str):
    """Read the supported provider API; never provision or launch a native app."""
    try:
        async with asyncio.timeout(10):
            space = await asyncio.to_thread(operator_client.get_agent_space, agentSpaceId=native_space_id)
            app = await asyncio.to_thread(operator_client.get_operator_app, agentSpaceId=native_space_id)
        if space.get("agentSpace", {}).get("agentSpaceId") != native_space_id:
            raise ValueError("native space changed")
        iam = app.get("iam")
        if not isinstance(iam, dict) or iam.get("operatorAppRoleArn") != operator_role_arn:
            raise ValueError("native IAM operator role changed")
        # Compare the complete origin, not a hostname substring. Credentials,
        # ports, redirects, encoded paths and alternate native apps are refused.
        if app.get("operatorAppUrl") not in {
            f"https://{native_space_id}.aidevops.global.app.aws",
            f"https://{native_space_id}.aidevops.global.app.aws/",
        }:
            raise ValueError("native app origin changed")
    except Exception:
        raise HTTPException(409, detail={
            "code": "native_operator_app_unavailable",
            "message": "Verify that the selected DevOps Agent Space has IAM app access enabled with the saved operator role, then retry.",
        }) from None


async def mint_browser_launch(selection: SpaceSelection, *, native_space_id: str,
                              launcher_role_arn: str, operator_role_arn: str,
                              user_id: str, issuer: str, sts_client, federation_client,
                              operator_client, now: datetime | None = None,
                              expires_at: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    started = time.monotonic()
    try:
        selection.fingerprint()
        issuer_url = urlsplit(issuer)
        if now.tzinfo is None or (expires_at is not None and expires_at.tzinfo is None):
            raise ValueError("launch expiry needs a timezone")
        until = min(now + timedelta(minutes=15), expires_at) if expires_at is not None else now + timedelta(minutes=15)
        until = until.astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(503, detail={"code": "launch_configuration_unavailable"}) from None
    if (not isinstance(user_id, str) or not user_id or len(user_id) > 255
            or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", selection.region)
            or not isinstance(native_space_id, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,126}[a-z0-9])", native_space_id)
            or not _role_account(launcher_role_arn, selection.account_id)
            or not _role_account(operator_role_arn, selection.account_id)
            or launcher_role_arn == operator_role_arn
            or until <= now
            or not callable(getattr(operator_client, "get_agent_space", None))
            or not callable(getattr(operator_client, "get_operator_app", None))
            or issuer_url.scheme != "https" or not issuer_url.hostname
            or issuer_url.username or issuer_url.password
            or issuer_url.path not in {"", "/"} or issuer_url.query or issuer_url.fragment):
        raise HTTPException(503, detail={"code": "launch_configuration_unavailable"})

    await verify_operator_app(operator_client, native_space_id, operator_role_arn)
    subject = "adp-" + hashlib.sha256(f"{selection.tenant_id}:{user_id}".encode()).hexdigest()[:32]
    try:
        if until <= now + timedelta(seconds=time.monotonic() - started):
            raise ValueError("launch authorization expired")
        assumed = await asyncio.wait_for(asyncio.to_thread(sts_client.assume_role,
            RoleArn=launcher_role_arn, RoleSessionName=subject,
            SourceIdentity=subject, DurationSeconds=900,
            Policy=json.dumps(launcher_session_policy(selection, native_space_id, until), separators=(",", ":")),
        ), timeout=10)
        assumed_arn = assumed["AssumedRoleUser"]["Arn"]
        expected = f"arn:aws:sts::{selection.account_id}:assumed-role/{launcher_role_arn.rsplit('/', 1)[-1]}/{subject}"
        if assumed_arn != expected:
            raise ValueError("assumed role does not match selection")
        credentials = assumed["Credentials"]
        credential_expiry = credentials["Expiration"]
        if not isinstance(credential_expiry, datetime) or credential_expiry.tzinfo is None:
            raise ValueError("invalid launcher credential expiry")
        until = min(until, credential_expiry.astimezone(UTC))
        if until <= now + timedelta(seconds=time.monotonic() - started):
            raise ValueError("launcher credentials expired")
        session = json.dumps({"sessionId": credentials["AccessKeyId"],
                              "sessionKey": credentials["SecretAccessKey"],
                              "sessionToken": credentials["SessionToken"]}, separators=(",", ":"))
        response = await federation_client.post(
            "https://signin.aws.amazon.com/federation",
            data={"Action": "getSigninToken", "Session": session},
            follow_redirects=False, timeout=10,
        )
        if response.status_code != 200 or len(response.content) > 4096:
            raise ValueError("federation refused session")
        ticket = response.json()["SigninToken"]
        if not isinstance(ticket, str) or not 1 <= len(ticket) <= 2048:
            raise ValueError("invalid federation ticket")
        if until <= now + timedelta(seconds=time.monotonic() - started):
            raise ValueError("launch authorization expired")
    except Exception:
        raise HTTPException(503, detail={"code": "federation_unavailable"}) from None

    native = f"https://{native_space_id}.aidevops.global.app.aws/"
    encoded_native = quote(base64.b64encode(native.encode()).decode(), safe="")
    destination = (f"https://{selection.region}.console.aws.amazon.com/aidevops/home"
                   f"?region={selection.region}#/agent-spaces/{native_space_id}/launch?redirect={encoded_native}")
    launch_url = "https://signin.aws.amazon.com/federation?" + urlencode({
        "Action": "login", "Issuer": issuer, "Destination": destination, "SigninToken": ticket,
    })
    if len(launch_url) > 2048:
        raise HTTPException(503, detail={"code": "federation_unavailable"})
    return {"binding_id": selection.binding_id, "launch_url": launch_url,
            "expires_at": until.isoformat().replace("+00:00", "Z")}
