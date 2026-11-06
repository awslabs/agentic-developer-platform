"""Role-free helper: capture a mounted Crew home and submit a scoped candidate."""

import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from crew_checkpoint import InvalidCheckpoint, capture, restore_empty_mount


CHECKPOINT_ID = re.compile(r"^[a-f0-9-]{36}$")
OWNER_KEY = re.compile(r"^[a-f0-9]{52}$")
GATEWAY_HOST = "chat-sandbox-gateway.adp-gateway.svc"


def _response(opener, request, *, context=None):
    with opener(request, timeout=60, context=context) as response:
        return response.read(), response.headers


def _post(opener, url, token, body, context):
    data, _ = _response(opener, Request(url, json.dumps(body).encode(), {
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
    }, method="POST"), context=context)
    return json.loads(data)


def run(env, *, opener=urlopen, home=None, quiesced=False, quiescence=None):
    checkpoint_id = env.get("ADP_CHECKPOINT_ID", "")
    owner_key = env.get("ADP_OWNER_KEY", "")
    gateway_url = env.get("ADP_CHAT_DATA_URL", "")
    parsed = urlparse(gateway_url)
    if not CHECKPOINT_ID.fullmatch(checkpoint_id) or not OWNER_KEY.fullmatch(owner_key):
        raise InvalidCheckpoint("invalid scoped checkpoint identity")
    if parsed.scheme != "https" or parsed.netloc != f"{GATEWAY_HOST}:8443" or parsed.path or parsed.query or parsed.fragment:
        raise InvalidCheckpoint("untrusted checkpoint gateway")
    mounted_home = env.get("KIROCREW_HOME", "")
    if mounted_home != "/mnt/crew":
        raise InvalidCheckpoint("unexpected Crew mount")
    token_path = env.get("ADP_WORKLOAD_TOKEN_FILE", "")
    ca_path = env.get("NODE_EXTRA_CA_CERTS", "")
    if token_path != "/var/run/adp-checkpoint/token" or ca_path != "/var/run/adp-chat-ca/ca.crt":
        raise InvalidCheckpoint("untrusted helper identity")
    token = Path(token_path).read_text(encoding="utf-8").strip()
    if not token:
        raise InvalidCheckpoint("missing helper identity")
    context = ssl.create_default_context(cafile=ca_path)
    s3_context = ssl.create_default_context()
    endpoint = f"{gateway_url}/internal/v1/agent/crew/checkpoints/{checkpoint_id}"
    with tempfile.TemporaryDirectory(prefix="checkpoint-", dir="/tmp") as directory:
        archive = Path(directory) / "archive.tar"
        manifest_path = Path(directory) / "manifest.json"
        metadata = capture(home or mounted_home, archive, manifest_path,
                           quiesced=quiesced, quiescence=quiescence)
        payloads = {"archive": archive.read_bytes(), "manifest": manifest_path.read_bytes()}
        grant = _post(opener, endpoint + "/upload-grant", token, {
            "owner_key": owner_key, "archive_sha256": metadata["archive_sha256"],
            "manifest_sha256": hashlib.sha256(payloads["manifest"]).hexdigest(),
            "archive_size": len(payloads["archive"]), "manifest_size": len(payloads["manifest"]),
            "crew_version": metadata["crew_version"],
        }, context)
        receipts = {}
        for label, data in payloads.items():
            target = grant.get(label, {})
            upload_url = target.get("url", "")
            if not upload_url.startswith("https://") or not target.get("key") or not target.get("bucket"):
                raise InvalidCheckpoint("incomplete upload grant")
            _, headers = _response(opener, Request(upload_url, data, headers=target.get("headers", {}), method="PUT"), context=s3_context)
            version = headers.get("x-amz-version-id")
            if not version:
                raise InvalidCheckpoint("unversioned checkpoint upload")
            receipts[label] = {"bucket": target["bucket"], "key": target["key"],
                               "version_id": version, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        result = _post(opener, endpoint + "/complete", token, {"owner_key": owner_key, "objects": receipts}, context)
        if result.get("id") != checkpoint_id or result.get("status") != "verified":
            raise InvalidCheckpoint("checkpoint verification pending or failed")
        return result


if __name__ == "__main__":
    try:
        run(os.environ)
    except Exception as error:
        print(f"checkpoint helper failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)


def run_restore(env, *, opener=urlopen, home=None):
    checkpoint_id = env.get("ADP_CHECKPOINT_ID", "")
    owner_key = env.get("ADP_OWNER_KEY", "")
    gateway_url = env.get("ADP_CHAT_DATA_URL", "")
    parsed = urlparse(gateway_url)
    if not CHECKPOINT_ID.fullmatch(checkpoint_id) or not OWNER_KEY.fullmatch(owner_key):
        raise InvalidCheckpoint("invalid scoped checkpoint identity")
    if parsed.scheme != "https" or parsed.netloc != f"{GATEWAY_HOST}:8443" or parsed.path or parsed.query or parsed.fragment:
        raise InvalidCheckpoint("untrusted checkpoint gateway")
    if env.get("KIROCREW_HOME") != "/mnt/crew":
        raise InvalidCheckpoint("unexpected Crew mount")
    token_path = env.get("ADP_WORKLOAD_TOKEN_FILE", "")
    ca_path = env.get("NODE_EXTRA_CA_CERTS", "")
    if token_path != "/var/run/adp-checkpoint/token" or ca_path != "/var/run/adp-chat-ca/ca.crt":
        raise InvalidCheckpoint("untrusted restore identity")
    mounted_home = Path(home or "/mnt/crew")
    if mounted_home.is_symlink() or not mounted_home.is_dir() or any(mounted_home.iterdir()):
        raise InvalidCheckpoint("restore requires an empty mounted home")
    token = Path(token_path).read_text(encoding="utf-8").strip()
    if not token:
        raise InvalidCheckpoint("missing restore identity")
    endpoint = f"{gateway_url}/internal/v1/agent/crew/checkpoints/{checkpoint_id}"
    context = ssl.create_default_context(cafile=ca_path)
    grant = _post(opener, endpoint + "/restore-grant", token, {}, context)
    pin = grant.get("restore_token", "")
    if not isinstance(pin, str) or len(pin) < 32:
        raise InvalidCheckpoint("invalid restore pin")
    try:
        if grant.get("id") != checkpoint_id or grant.get("crew_version") != "0.11.2" or grant.get("schema_version") != 1:
            raise InvalidCheckpoint("incompatible restore grant")
        objects = grant.get("objects", {})
        if set(objects) != {"archive", "manifest"}:
            raise InvalidCheckpoint("incomplete restore receipt")

        def fetch(bucket, key, version):
            label = next((name for name, item in objects.items()
                          if (item.get("bucket"), item.get("key"), item.get("version_id")) == (bucket, key, version)), None)
            if label is None:
                raise InvalidCheckpoint("unrecognized restore object")
            item = objects[label]
            url = item.get("url", "")
            target = urlparse(url)
            size = item.get("size")
            limit = 512 * 1024 * 1024 if label == "archive" else 1024 * 1024
            if (target.scheme != "https" or not target.netloc or target.username or target.password or target.fragment or
                    type(size) is not int or not 0 < size <= limit):
                raise InvalidCheckpoint("invalid restore object grant")
            with opener(Request(url, method="GET"), timeout=60, context=ssl.create_default_context()) as response:
                if response.headers.get("x-amz-version-id") != version:
                    raise InvalidCheckpoint("restore version mismatch")
                content = response.read(size + 1)
            if len(content) != size or hashlib.sha256(content).hexdigest() != item.get("sha256"):
                raise InvalidCheckpoint("restore checksum mismatch")
            return content

        restore_empty_mount(mounted_home, objects, fetch)
    finally:
        _post(opener, endpoint + "/restore-release", token, {"restore_token": pin}, context)
    return {"id": checkpoint_id, "status": "restored"}
