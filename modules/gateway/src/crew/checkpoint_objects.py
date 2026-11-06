"""Presigned uploads and independent verification of exact S3 object versions."""

import base64
import hashlib
import json
import re
import tarfile
import tempfile
from contextlib import closing
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$")
COMPATIBLE_CREW_VERSION = "0.11.2"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
ALLOWED_FILES = frozenset({"memory.db", "memory_index.db", "lessons.jsonl", "notifications.jsonl", "telemetry_salt"})
# Mirrored by the host helper modules/agent-factory/agent/scripts/crew_checkpoint.py (FILES/TREES): a path
# the helper archives that these rules refuse makes the checkpoint "unsafe archive inventory" and it is never
# verified, so the two sets are kept identical (the helper's tests check parity). adp-crew-session-map/ is the
# ADP-written session map the Crew bridge keeps in the home (crew-kirocrew-bridge.ts); it is owner continuity.
ALLOWED_TREES = frozenset(
    {
        ".kiro",
        "workspace",
        "memory_stores",
        "sessions",
        "trust",
        "members",
        "artifacts",
        "uploads",
        "crew-teams",
        "plan_memory",
        "skills",
        "crew-log",
        "adp-crew-session-map",
        ".claude",
        ".codex",
    }
)
EXCLUDED_NAMES = frozenset(
    {
        "sel_hmac.key",
        ".env",
        ".aws",
        ".ssh",
        "credentials",
        "credentials.json",
        ".credentials.json",
        "tokens",
        "agents",
        ".adp",
        ".git-credentials",
        "hosts.yml",
        "auth.json",
        "model.env",
        "adp-dev-box-shell",
    }
)
EXCLUDED_SUFFIXES = ("-wal", "-shm", ".pid", ".lock", ".sock", ".tmp")


class InvalidCheckpointObjectsError(Exception):
    pass


def checkpoint_s3_client(region: str | None = None):
    """The S3 client that signs checkpoint grants: SigV4, pinned to a region.

    ``boto3.client("s3")`` in us-east-1 presigns with the legacy SigV2 scheme. S3 answers a SigV2
    PUT on the KMS-encrypted checkpoint bucket with 403 SignatureDoesNotMatch and ignores the
    checksum header the grant depends on, so every host upload failed as ``archive upload
    refused: 403`` (dev 2026-10-21, #8223 cause 45). SigV4 signs the checksum header too.
    """
    return boto3.client("s3", region_name=region or None, config=Config(signature_version="s3v4"))


def _require_sigv4(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    if query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"]:
        raise InvalidCheckpointObjectsError("checkpoint grant must be SigV4")
    return url


def valid_path(name):
    parts = PurePosixPath(name).parts
    if (
        not parts
        or str(PurePosixPath(name)) != name
        or name.startswith("/")
        or "\\" in name
        or any(part in (".", "..") for part in parts)
        or parts[0] not in ALLOWED_FILES | ALLOWED_TREES
        or parts[0] in ALLOWED_FILES
        and len(parts) != 1
        or parts[0] == ".kiro"
        and parts[:3] != (".kiro", "sessions", "cli")
        or parts[-1].startswith("rollout-")
        and parts[-1].endswith(".jsonl")
        or any(part in EXCLUDED_NAMES or part.endswith(EXCLUDED_SUFFIXES) for part in parts)
    ):
        raise InvalidCheckpointObjectsError("unsafe archive inventory")


class VersionedCheckpointObjects:
    def __init__(self, s3, bucket: str):
        if not bucket or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", bucket):
            raise InvalidCheckpointObjectsError("checkpoint bucket unavailable")
        self.s3 = s3
        self.bucket = bucket

    def grant(self, checkpoint, objects):
        if self.s3.get_bucket_versioning(Bucket=self.bucket).get("Status") != "Enabled":
            raise InvalidCheckpointObjectsError("versioned checkpoint bucket required")
        result = {}
        for label in ("archive", "manifest"):
            item = objects[label]
            digest, size = item["sha256"], item["size"]
            limit = MAX_ARCHIVE_BYTES if label == "archive" else MAX_MANIFEST_BYTES
            if not HEX_SHA256.fullmatch(digest) or type(size) is not int or not 0 < size <= limit:
                raise InvalidCheckpointObjectsError("invalid checkpoint object")
            key = f"crew-checkpoints/{checkpoint.owner_key}/{checkpoint.id}/{label}"
            checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
            url = _require_sigv4(
                self.s3.generate_presigned_url(
                    "put_object", Params={"Bucket": self.bucket, "Key": key, "ChecksumSHA256": checksum}, ExpiresIn=300, HttpMethod="PUT"
                )
            )
            result[label] = {"bucket": self.bucket, "key": key, "url": url, "headers": {"x-amz-checksum-sha256": checksum}}
        return result

    def grant_read(self, checkpoint):
        result = {}
        for label in ("archive", "manifest"):
            bucket, key, version = (getattr(checkpoint, f"{label}_{field}") for field in ("bucket", "key", "version_id"))
            if bucket != self.bucket or key != f"crew-checkpoints/{checkpoint.owner_key}/{checkpoint.id}/{label}" or not version:
                raise InvalidCheckpointObjectsError("invalid restore receipt")
            result[label] = {
                "bucket": bucket,
                "key": key,
                "version_id": version,
                "sha256": getattr(checkpoint, f"{label}_sha256"),
                "size": getattr(checkpoint, f"{label}_size"),
                "url": _require_sigv4(
                    self.s3.generate_presigned_url(
                        "get_object", Params={"Bucket": bucket, "Key": key, "VersionId": version}, ExpiresIn=300, HttpMethod="GET"
                    )
                ),
            }
        return result

    def delete_recorded(self, checkpoint):
        for label in ("archive", "manifest"):
            bucket, key, version = (getattr(checkpoint, f"{label}_{field}") for field in ("bucket", "key", "version_id"))
            if bucket != self.bucket or key != f"crew-checkpoints/{checkpoint.owner_key}/{checkpoint.id}/{label}" or not version or version == "null":
                raise InvalidCheckpointObjectsError("invalid recorded object version")
            try:
                self.s3.head_object(Bucket=bucket, Key=key, VersionId=version)
            except ClientError as error:
                if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NoSuchVersion"):
                    continue
                raise
            self.s3.delete_object(Bucket=bucket, Key=key, VersionId=version)
            try:
                self.s3.head_object(Bucket=bucket, Key=key, VersionId=version)
            except ClientError as error:
                if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NoSuchVersion"):
                    continue
                raise
            raise InvalidCheckpointObjectsError("recorded version still exists")

    def _read_exact(self, item, *, limit):
        response = self.s3.get_object(Bucket=item["bucket"], Key=item["key"], VersionId=item["version_id"], ChecksumMode="ENABLED")
        if response.get("VersionId") != item["version_id"]:
            raise InvalidCheckpointObjectsError("S3 object version mismatch")
        checksum = hashlib.sha256()
        content = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        try:
            length = 0
            body = response["Body"]
            try:
                while chunk := body.read(1024 * 1024):
                    length += len(chunk)
                    if length > limit:
                        raise InvalidCheckpointObjectsError("checkpoint object too large")
                    checksum.update(chunk)
                    content.write(chunk)
            finally:
                body.close()
            if length != item["size"] or checksum.hexdigest() != item["sha256"]:
                raise InvalidCheckpointObjectsError("checkpoint content hash mismatch")
            if response.get("ChecksumSHA256") and response["ChecksumSHA256"] != base64.b64encode(checksum.digest()).decode():
                raise InvalidCheckpointObjectsError("S3 checksum mismatch")
            content.seek(0)
            return content
        except Exception:
            content.close()
            raise

    def verify(self, checkpoint, objects):
        for label in ("archive", "manifest"):
            item = objects[label]
            if (
                item["bucket"] != getattr(checkpoint, f"{label}_bucket")
                or item["key"] != getattr(checkpoint, f"{label}_key")
                or item["sha256"] != getattr(checkpoint, f"{label}_sha256")
                or item["size"] != getattr(checkpoint, f"{label}_size")
                or not isinstance(item.get("version_id"), str)
                or not item["version_id"]
                or item["version_id"] == "null"
            ):
                raise InvalidCheckpointObjectsError("unrecorded checkpoint object")
        with closing(self._read_exact(objects["archive"], limit=MAX_ARCHIVE_BYTES)) as archive:
            with closing(self._read_exact(objects["manifest"], limit=MAX_MANIFEST_BYTES)) as manifest_file:
                manifest_bytes = manifest_file.read()
            self._verify_archive(archive, manifest_bytes, objects["archive"]["sha256"])

    def conversation_turns(self, checkpoint, path, source, conversation_id, offset, limit):
        from src.crew.conversation_index import codex_turns_extract, read_turns

        valid_path(path)
        item = {
            "bucket": checkpoint.archive_bucket,
            "key": checkpoint.archive_key,
            "version_id": checkpoint.archive_version_id,
            "size": checkpoint.archive_size,
            "sha256": checkpoint.archive_sha256,
        }
        if item["bucket"] != self.bucket or item["key"] != f"crew-checkpoints/{checkpoint.owner_key}/{checkpoint.id}/archive":
            raise InvalidCheckpointObjectsError("invalid conversation archive")
        with closing(self._read_exact(item, limit=MAX_ARCHIVE_BYTES)) as archive:
            with tarfile.open(fileobj=archive, mode="r:") as source_archive:
                target = ".codex/owner-shell/history.jsonl" if source == "codex" and not codex_turns_extract(path) else path
                for member in source_archive:
                    if member.name != target:
                        continue
                    if not member.isfile() or member.size > 16 * 1024 * 1024:
                        raise InvalidCheckpointObjectsError("conversation too large")
                    with source_archive.extractfile(member) as data:
                        turns = read_turns(data, source, conversation_id)
                    return turns[offset : offset + limit], offset + limit < len(turns)
        raise InvalidCheckpointObjectsError("conversation missing")

    def conversation_headers(self, objects):
        from src.crew.conversation_index import extract_conversations

        with closing(self._read_exact(objects["archive"], limit=MAX_ARCHIVE_BYTES)) as archive:
            return extract_conversations(archive)

    @staticmethod
    def _verify_archive(archive, manifest_bytes, archive_sha256):
        try:
            manifest = json.loads(manifest_bytes)
            if (
                manifest["format"] != 1
                or manifest["crew_version"] != COMPATIBLE_CREW_VERSION
                or manifest["archive_sha256"] != archive_sha256
                or not isinstance(manifest["files"], dict)
                or not manifest["files"]
            ):
                raise InvalidCheckpointObjectsError("incompatible manifest")
            expected = manifest["files"]
            seen = set()
            with tarfile.open(fileobj=archive, mode="r:") as source:
                for member in source:
                    valid_path(member.name)
                    if not member.isfile() or member.name in seen or member.name not in expected:
                        raise InvalidCheckpointObjectsError("unexpected archive member")
                    seen.add(member.name)
                    checksum = hashlib.sha256()
                    with source.extractfile(member) as data:
                        while chunk := data.read(1024 * 1024):
                            checksum.update(chunk)
                    if expected[member.name] != {"sha256": checksum.hexdigest(), "size": member.size}:
                        raise InvalidCheckpointObjectsError("archive member checksum mismatch")
            if seen != set(expected):
                raise InvalidCheckpointObjectsError("incomplete archive")
        except (KeyError, TypeError, ValueError, tarfile.TarError, UnicodeDecodeError) as error:
            raise InvalidCheckpointObjectsError("invalid checkpoint archive") from error
