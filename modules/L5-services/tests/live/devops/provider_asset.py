"""Read a versioned native knowledge asset using the selected broker session."""

import hashlib
import json
import re
import sys
from io import BytesIO
from zipfile import BadZipFile, ZipFile


MAX_CONTENT_BYTES = 1024 * 1024
ASSET_PATHS = {"agents_md": "AGENTS.md", "skill": "SKILL.md"}


def read_asset(account_id, role_arn, region, space_id, asset_id, *, client_factory=None):
    import boto3
    from botocore.config import Config

    client_factory = client_factory or boto3.client
    bounds = Config(connect_timeout=4, read_timeout=5, retries={"total_max_attempts": 1})
    identity = client_factory("sts", region_name=region, config=bounds).get_caller_identity()
    name = role_arn.split("/")[-1]
    expected = rf"arn:aws:sts::{re.escape(account_id)}:assumed-role/{re.escape(name)}/[^/]+"
    if identity.get("Account") != account_id or not re.fullmatch(expected, identity.get("Arn", "")):
        raise ValueError("selected AWS connection does not match native asset identity")
    client = client_factory("devops-agent", region_name=region, config=bounds)
    asset = client.get_asset(agentSpaceId=space_id, assetId=asset_id).get("asset")
    if (not isinstance(asset, dict) or asset.get("assetId") != asset_id
            or asset.get("assetType") not in ASSET_PATHS
            or type(asset.get("version")) is not int or asset["version"] < 1
            or not isinstance(asset.get("metadata"), dict)):
        raise ValueError("native knowledge asset identity or version is unavailable")
    content = client.get_asset_content(agentSpaceId=space_id, assetId=asset_id, assetVersion=asset["version"])
    archive_bytes = (content.get("content") or {}).get("zipFile")
    if (content.get("version") != asset["version"] or not isinstance(archive_bytes, bytes)
            or len(archive_bytes) > MAX_CONTENT_BYTES + 65536):
        raise ValueError("native knowledge content version or size differs")
    path = ASSET_PATHS[asset["assetType"]]
    content_limit = 25_000 if asset["assetType"] == "agents_md" else MAX_CONTENT_BYTES
    try:
        with ZipFile(BytesIO(archive_bytes)) as archive:
            if (archive.namelist() != [path] or archive.getinfo(path).file_size > content_limit
                    or archive.getinfo(path).flag_bits & 1):
                raise ValueError("native knowledge archive is not one bounded source")
            with archive.open(path) as source:
                contents = source.read(content_limit + 1)
            if len(contents) > content_limit:
                raise ValueError("native knowledge content exceeded its size bound")
    except (BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError("native knowledge archive is unreadable") from error
    return {"asset": {key: asset[key] for key in ("assetId", "assetType", "version", "metadata")},
            "content_sha256": hashlib.sha256(contents).hexdigest(), "content_bytes": len(contents),
            "content_version": content["version"]}


if __name__ == "__main__":
    if len(sys.argv) != 6:
        raise SystemExit("native asset readback needs one selected space and asset")
    try:
        print(json.dumps(read_asset(*sys.argv[1:])))
    except Exception:
        raise SystemExit("selected native knowledge readback unavailable") from None
