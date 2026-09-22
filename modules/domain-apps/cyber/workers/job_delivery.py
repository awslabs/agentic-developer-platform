"""Consume broker-only job manifests and exact-version download capabilities."""
import hashlib
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

import requests

from sample_access import AccessDenied, ObjectRef


def registered_job(body: dict, stage: str) -> None:
    now = int(time.time())
    if (body.get("registration_version") != 1 or body.get("stage") != stage
        or not re.fullmatch(r"cyber-[a-f0-9]{32}-[a-f0-9]{32}", str(body.get("artifact_id", "")))
        or not isinstance(body.get("issued_at"), int) or not isinstance(body.get("expires_at"), int)
        or not body["issued_at"] <= now < body["expires_at"] <= body["issued_at"] + 900):
        raise AccessDenied("job_registration_invalid")


def download_sample(body: dict, ref: ObjectRef, dest: Path) -> None:
    """The worker's ambient role cannot read the object or mint this capability."""
    cap = body.get("sample_download", {})
    try:
        parsed = urlsplit(cap["url"])
        query = parse_qs(parsed.query)
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port
            or not re.fullmatch(re.escape(ref.bucket) + r"\.s3(?:\.[a-z0-9-]+)?\.amazonaws\.com", parsed.hostname or "")
            or not cap["version"] or cap["version"] == "null" or query.get("versionId") != [cap["version"]]
            or not re.fullmatch(r"[a-f0-9]{64}", cap["sha256"])
            or not isinstance(cap["size"], int) or not 0 < cap["size"] <= 64 * 1024 * 1024):
            raise ValueError()
        from urllib.parse import unquote
        if unquote(parsed.path) != "/" + ref.key:
            raise ValueError()
        digest, size = hashlib.sha256(), 0
        with requests.Session() as session:
            session.trust_env = False
            with session.get(cap["url"], stream=True, allow_redirects=False, timeout=(3, 30)) as response:
                if response.status_code != 200:
                    raise ValueError()
                with dest.open("xb") as output:
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > cap["size"] or time.time() >= body["expires_at"]:
                            raise ValueError()
                        digest.update(chunk)
                        output.write(chunk)
        if size != cap["size"] or digest.hexdigest() != cap["sha256"]:
            raise ValueError()
    except (ValueError, KeyError, TypeError, OSError, requests.RequestException):
        dest.unlink(missing_ok=True)
        raise AccessDenied("sample_capability_invalid") from None
