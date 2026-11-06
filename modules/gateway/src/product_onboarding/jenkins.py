"""Fixed Jenkins reads using an existing, version-bound ADP credential."""

import asyncio
import hashlib
import json
from datetime import timedelta
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException

from src.internal.connector_source_broker import bounded_read
from src.product_onboarding.source_discovery import safe_native
from src.shared.models.base import utcnow


def full_name(value):
    return safe_native(value) and all(part not in ("", ".", "..") and not any(c in part for c in "\\?#") for part in value.split("/"))


def selection(namespace, native_id):
    return full_name(native_id) and (native_id.rpartition("/")[0] or "/") == namespace


def job_path(name):
    return "/".join("job/" + quote(segment, safe="") for segment in name.split("/")) if name else ""


async def jenkins_credential(credential, secrets, *, expected_version=None):
    if credential is None or (credential.provider or credential.service) != "jenkins" or credential.credential_type != "basic_auth":
        raise HTTPException(409, detail={"code": "jenkins_source_unavailable"})
    try:
        version = await asyncio.to_thread(secrets.current_version_id, credential.secret_arn)
        if not version or expected_version is not None and version != expected_version:
            raise ValueError()
        payload, served = await asyncio.to_thread(secrets.get_secret_at_version, credential.secret_arn, version)
        secret = json.loads(payload)
        endpoint = secret["url"]
        parsed = urlsplit(endpoint)
        username, token = secret["username"], secret["token"]
        if (
            served != version
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(part in (".", "..") for part in parsed.path.split("/"))
            or any(c in endpoint for c in "\\\r\n")
            or not isinstance(username, str)
            or not username
            or not isinstance(token, str)
            or not token
        ):
            raise ValueError()
        return endpoint.rstrip("/"), httpx.BasicAuth(username, token), version
    except Exception:
        raise HTTPException(409, detail={"code": "jenkins_source_unavailable"}) from None


async def read_jenkins(credential, secrets, *, name, expected_version=None):
    """Read only the exact selected job, with no parent or sibling enumeration."""
    if not full_name(name):
        raise HTTPException(409, detail={"code": "jenkins_source_unavailable"})
    try:
        endpoint, auth, version = await jenkins_credential(credential, secrets, expected_version=expected_version)
        tree = "fullName,name,_class"
        path = job_path(name)
        target = endpoint + "/" + (path + "/" if path else "") + "api/json"
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            raw, _ = await bounded_read(client, target, auth=auth, params={"tree": tree})
        if len(raw) > 1024 * 1024 or await asyncio.to_thread(secrets.current_version_id, credential.secret_arn) != version:
            raise ValueError()
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or data.get("fullName") != name
            or data.get("name") != name.rsplit("/", 1)[-1]
            or data.get("_class") not in ("hudson.model.FreeStyleProject", "org.jenkinsci.plugins.workflow.job.WorkflowJob")
        ):
            raise ValueError()
        return {key: data[key] for key in ("fullName", "name", "_class")}, version
    except Exception:
        raise HTTPException(409, detail={"code": "jenkins_source_unavailable"}) from None


async def observe_jenkins(binding, credential, secrets, *, now=None):
    if (
        credential is None
        or credential.org_id != binding.tenant_id
        or credential.id != binding.credential_id
        or not selection(binding.namespace, binding.native_id)
    ):
        return {"coverage": "denied", "status": "revoked", "reason": "source_access_denied"}
    try:
        data, version = await read_jenkins(credential, secrets, name=binding.native_id)
        observed_at = now or utcnow()
        revision = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        return {
            "coverage": "complete",
            "status": "healthy",
            "observed": {
                "observed_at": observed_at.isoformat(),
                "stale_after": (observed_at + timedelta(minutes=15)).isoformat(),
                "source_revision": "observation-sha256:" + revision,
                "native_id": binding.native_id,
                "namespace": binding.namespace,
                "credential_version": version,
            },
        }
    except (HTTPException, ValueError):
        return {"coverage": "unavailable", "status": "unverified", "reason": "jenkins_source_unavailable"}
