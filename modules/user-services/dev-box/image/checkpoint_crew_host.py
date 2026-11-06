"""Capture the Crew home and publish it as a verified S3 checkpoint from the EC2 host (#8379).

Two modes, run as root by systemd (``adp-crew-checkpoint.timer`` every 15 minutes,
``adp-crew-checkpoint-final.service`` at shutdown) and by the controller's pre-stop SSM command:

* ``periodic`` — a background capture of the allowlisted Crew home (``crew_checkpoint.capture``:
  SQLite online backup per database, every other file byte-exact). Files are individually
  consistent; nothing is paused.
* ``final`` — the Crew units are stopped first (the machine is about to stop anyway), so the
  capture is a quiesced one and the manifest says so.

The host never holds a checkpoint credential: it signs ``begin``/``complete`` with its instance
role (SigV4 against the gateway REST API, credentials from IMDSv2) and uploads the archive and
manifest with the gateway-issued presigned PUTs. The gateway binds the instance to its owner,
verifies the exact S3 versions and advances the owner's pointer; the helper cannot publish by
uploading. An unchanged inventory (same files, digests and sizes as the last verified capture,
remembered root-only in ``/mnt/crew/.adp-host/checkpoint.json``) uploads nothing.

Output: one JSON line on stdout — ``{"status": "verified"|"unchanged"|"failed", ...}`` — and the
refusal class on stderr. Exit 0 for verified/unchanged, 1 otherwise. Stdlib only: the guest runs
helpers on AL2023's system Python (3.9); keep ``X | None`` annotations lazy.
"""

from __future__ import annotations


import argparse
import contextlib
import datetime
import hashlib
import hmac
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

# systemd runs this helper with ``python3 -I`` (isolated: no script directory on sys.path); the
# archive module ships beside it in /opt/adp/crew, so name that directory explicitly. Without
# this the shutdown capture died with ModuleNotFoundError (dev 2026-10-20, first real stop).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from crew_checkpoint import InvalidCheckpoint, capture  # noqa: E402


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class CheckpointRefused(InvalidCheckpoint):
    """The host could not prove or complete a capture; the class names why."""


CREW_VERSION = "0.11.2"
HOME = Path("/mnt/crew")
RUN_ROOT = Path("/run/adp")
STATE_DIRECTORY = ".adp-host"      # root-only marker directory (restore_crew_host.py); excluded from archives
STATE_FILE = "checkpoint.json"
IMDS = "http://169.254.169.254"
IMDS_TOKEN_TTL = "300"
REQUEST_TIMEOUT = 60
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
CREW_UNITS = ("adp-crew-host.service", "adp-crew.service")
GATEWAY_ARN = re.compile(r"^arn:aws:execute-api:([a-z0-9-]+):([0-9]{12}):([a-z0-9]+)/([A-Za-z0-9_-]+)$")
AUTHORITY_FIELDS = ("run_id", "tenant_id", "user_id", "box_id", "instance_ref", "lease_generation")


_lstat = os.lstat   # module hook so tests can present a home owned by another uid


def _trusted_directory(path: Path, uid: int) -> None:
    entry = path.lstat()
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != uid or entry.st_mode & 0o022:
        raise CheckpointRefused("untrusted Crew host directory")


def _agent_uid(agent_uid, uid: int) -> int:
    if agent_uid is not None:
        return agent_uid
    try:
        return pwd.getpwnam("agent").pw_uid
    except KeyError:
        return uid


def _trusted_home(home: Path, uid: int, agent_uid: int) -> None:
    """The Crew home belongs to ``agent`` once provisioned; root only until then.

    Twin of ``restore_crew_host._trusted_home`` (#8437): the restorer hands the whole tree to the
    agent user and Kiro Crew keeps writing to it, so a capture of a provisioned home must accept
    the agent owner. This helper kept its own root-only copy of the check, and on dev
    2026-10-20/21 every periodic and final capture on a real box was refused with "untrusted Crew
    host directory" — the bucket stayed empty (#8223 cause 35). Group/world write and symlinks are
    still refused; ``/run/adp``, its receipts and the ``.adp-host`` state directory keep requiring
    root. The refusal names the path and the owners it would accept so the controller log is
    actionable.
    """
    entry = _lstat(home)
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid not in (uid, agent_uid) or entry.st_mode & 0o022:
        raise CheckpointRefused(
            f"untrusted Crew host directory: {home} must be a directory owned by uid {uid} or {agent_uid} "
            f"without group/world write (found uid {entry.st_uid}, mode {stat.S_IMODE(entry.st_mode):04o})"
        )


def _trusted_file(path: Path, uid: int, *, limit: int = 16384) -> bytes:
    _trusted_directory(path.parent, uid)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        entry = os.fstat(descriptor)
        if not stat.S_ISREG(entry.st_mode) or entry.st_uid != uid or entry.st_mode & 0o022 or entry.st_size > limit:
            raise CheckpointRefused("untrusted Crew host file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


def read_receipts(root: Path = RUN_ROOT, uid: int = 0) -> dict:
    """The run-scoped receipts the bootstrap published for this boot; a stale or partial set is refused."""
    if not (root / "crew-ready").exists():
        raise CheckpointRefused("Crew host is not ready")
    authority = json.loads(_trusted_file(root / "crew-host.json", uid))
    model = json.loads(_trusted_file(root / "crew-model.json", uid))
    if (not isinstance(authority, dict) or not isinstance(model, dict)
            or any(not authority.get(field) for field in AUTHORITY_FIELDS)
            or type(authority["lease_generation"]) is not int
            or model.get("run_id") != authority["run_id"]
            or model.get("lease_generation") != authority["lease_generation"]
            or not isinstance(model.get("gateway_execute_arn"), str)
            or GATEWAY_ARN.fullmatch(model["gateway_execute_arn"]) is None):
        raise CheckpointRefused("Crew host receipts are stale or incomplete")
    return {**{field: authority[field] for field in AUTHORITY_FIELDS}, "gateway_execute_arn": model["gateway_execute_arn"]}


def gateway_endpoint(execute_arn: str) -> dict:
    region, _account, api, stage = GATEWAY_ARN.fullmatch(execute_arn).groups()
    host = f"{api}.execute-api.{region}.amazonaws.com"
    return {"region": region, "host": host, "base": f"https://{host}/{stage}"}


# --- IMDSv2 + SigV4 (stdlib) -------------------------------------------------

def imds_credentials(opener) -> dict:
    token_request = Request(f"{IMDS}/latest/api/token", data=b"", method="PUT",
                            headers={"X-aws-ec2-metadata-token-ttl-seconds": IMDS_TOKEN_TTL})
    with opener.open(token_request, timeout=5) as response:
        token = response.read(1024).decode("ascii").strip()
    if not token:
        raise CheckpointRefused("instance metadata token unavailable")
    headers = {"X-aws-ec2-metadata-token": token}
    with opener.open(Request(f"{IMDS}/latest/meta-data/iam/security-credentials/", headers=headers), timeout=5) as response:
        role = response.read(1024).decode("ascii").strip().splitlines()[0]
    if not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,128}", role):
        raise CheckpointRefused("instance role unavailable")
    with opener.open(Request(f"{IMDS}/latest/meta-data/iam/security-credentials/{role}", headers=headers), timeout=5) as response:
        credentials = json.loads(response.read(65536))
    if (credentials.get("Code") != "Success" or not all(isinstance(credentials.get(key), str) and credentials[key]
                                                           for key in ("AccessKeyId", "SecretAccessKey", "Token"))):
        raise CheckpointRefused("instance credentials unavailable")
    return {"access_key": credentials["AccessKeyId"], "secret_key": credentials["SecretAccessKey"], "token": credentials["Token"]}


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, body: bytes, credentials: dict, region: str, *, now=None,
                  headers=None, service: str = "execute-api") -> dict:
    """AWS Signature Version 4 for a single-chunk request; returns the headers to send."""
    moment = datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date = moment.strftime("%Y%m%d")
    target = urlsplit(url)
    canonical_uri = quote(target.path or "/", safe="/~")
    payload_hash = hashlib.sha256(body).hexdigest()
    signed = {"host": target.netloc, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash,
              "x-amz-security-token": credentials["token"]}
    for name, value in (headers or {}).items():
        signed[name.lower()] = " ".join(str(value).split())
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join([method, canonical_uri, target.query or "", canonical_headers, signed_headers, payload_hash])
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    signing_key = _hmac(_hmac(_hmac(_hmac(("AWS4" + credentials["secret_key"]).encode(), date), region), service), "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    result = {name: value for name, value in signed.items() if name != "host"}
    result["Authorization"] = f"AWS4-HMAC-SHA256 Credential={credentials['access_key']}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    return result


def _gateway_post(opener, endpoint: dict, path: str, body: dict, credentials: dict, run_id: str, *, now=None) -> dict:
    url = f"{endpoint['base']}{path}"
    data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    headers = sigv4_headers("POST", url, data, credentials, endpoint["region"], now=now,
                            headers={"content-type": "application/json", "x-agent-runid": run_id})
    headers["Content-Type"] = "application/json"
    try:
        with opener.open(Request(url, data=data, headers=headers, method="POST"), timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read(1024 * 1024))
    except HTTPError as error:
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(error.read(4096)).get("error", "")
        raise CheckpointRefused(f"gateway refused {path.rsplit('/', 1)[-1]}: {error.code} {detail}".strip()) from None


def _upload(opener, grant: dict, data: bytes, label: str) -> dict:
    url = grant.get("url", "")
    target = urlsplit(url)
    if (target.scheme != "https" or target.hostname is None
            or not (target.hostname.endswith(".amazonaws.com") or target.hostname.endswith(".amazonaws.com.cn"))
            or target.username or target.password or target.fragment or not target.query
            or not grant.get("key") or not grant.get("bucket")):
        raise CheckpointRefused("incomplete upload grant")
    headers = {str(name): str(value) for name, value in (grant.get("headers") or {}).items()}
    headers["Content-Length"] = str(len(data))
    try:
        with opener.open(Request(url, data=data, headers=headers, method="PUT"), timeout=REQUEST_TIMEOUT) as response:
            version = response.headers.get("x-amz-version-id")
    except HTTPError as error:
        raise CheckpointRefused(f"{label} upload refused: {error.code}") from None
    if not version or version == "null":
        raise CheckpointRefused("unversioned checkpoint upload")
    return {"bucket": grant["bucket"], "key": grant["key"], "version_id": version,
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


# --- local state -----------------------------------------------------------------

def files_digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest["files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_state(home: Path, uid: int):
    directory = home / STATE_DIRECTORY
    if directory.is_symlink() or not directory.is_dir():
        raise CheckpointRefused("Crew home was not provisioned by this box")
    entry = directory.lstat()
    if entry.st_uid != uid or entry.st_mode & 0o077:
        raise CheckpointRefused("untrusted Crew provisioning marker directory")
    path = directory / STATE_FILE
    if not path.exists() and not path.is_symlink():
        return None
    try:
        state = json.loads(_trusted_file(path, uid))
    except (InvalidCheckpoint, OSError, ValueError):
        return None   # an unreadable state only costs one redundant upload
    return state if isinstance(state, dict) and state.get("schema_version") == 1 else None


def write_state(home: Path, uid: int, state: dict) -> None:
    directory = home / STATE_DIRECTORY
    descriptor, pending = tempfile.mkstemp(prefix=".checkpoint-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as destination:
            os.fchmod(destination.fileno(), 0o600)
            json.dump({"schema_version": 1, **state}, destination, sort_keys=True)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(pending, directory / STATE_FILE)
    finally:
        Path(pending).unlink(missing_ok=True)


class StoppedCrewUnits:
    """Writer quiescence for the final capture: Crew is stopped before the home is read."""

    def __init__(self, run=subprocess.run):
        self.run = run

    def __enter__(self):
        for unit in CREW_UNITS:
            self.run(["systemctl", "stop", unit], check=False, capture_output=True)
        return self

    def __exit__(self, *exc):
        return False


# --- capture + publish -----------------------------------------------------------

def run_checkpoint(mode: str, *, home: Path = HOME, root: Path = RUN_ROOT, owner_home: Path = Path("/home/ec2-user"), uid: int = 0, opener=None,
                   run=subprocess.run, credentials=None, now=None, agent_uid=None) -> dict:
    if mode not in ("periodic", "final"):
        raise CheckpointRefused("unknown checkpoint mode")
    opener = opener or build_opener(NoRedirect, ProxyHandler({}))
    receipts = read_receipts(root, uid)
    _trusted_home(home, uid, _agent_uid(agent_uid, uid))
    previous = read_state(home, uid)
    endpoint = gateway_endpoint(receipts["gateway_execute_arn"])
    with tempfile.TemporaryDirectory(prefix="crew-checkpoint-", dir="/var/tmp" if Path("/var/tmp").is_dir() else None) as scratch:
        archive_path, manifest_path = Path(scratch) / "archive.tar", Path(scratch) / "manifest.json"
        quiesced = mode == "final"
        manifest = capture(home, archive_path, manifest_path, quiesced=quiesced,
                           quiescence=StoppedCrewUnits(run) if quiesced else None, owner_home=owner_home)
        digest = files_digest(manifest)
        if previous and previous.get("files_sha256") == digest and previous.get("checkpoint_id"):
            return {"status": "unchanged", "mode": mode, "checkpoint_id": previous["checkpoint_id"],
                    "sequence": previous.get("sequence"), "quiesced": quiesced}
        archive, manifest_bytes = archive_path.read_bytes(), manifest_path.read_bytes()
    if len(archive) > MAX_ARCHIVE_BYTES or len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise CheckpointRefused("Crew home exceeds the checkpoint size bound")
    credentials = credentials or imds_credentials(opener)
    key = f"host:{receipts['lease_generation']}:{digest[:48]}"
    begun = _gateway_post(opener, endpoint, "/agent/crew/host-checkpoint/begin", {
        "idempotency_key": key, "crew_version": manifest["crew_version"],
        "archive_sha256": manifest["archive_sha256"], "archive_size": len(archive),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "manifest_size": len(manifest_bytes),
    }, credentials, receipts["run_id"], now=now)
    checkpoint_id = begun.get("id", "")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,36}", checkpoint_id) or type(begun.get("sequence")) is not int:
        raise CheckpointRefused("invalid checkpoint admission")
    if begun.get("status") != "verified":
        if begun.get("status") != "uploading" or not isinstance(begun.get("archive"), dict) or not isinstance(begun.get("manifest"), dict):
            raise CheckpointRefused("invalid checkpoint admission")
        receipts_out = {"archive": _upload(opener, begun["archive"], archive, "archive"),
                        "manifest": _upload(opener, begun["manifest"], manifest_bytes, "manifest")}
        completed = _gateway_post(opener, endpoint, "/agent/crew/host-checkpoint/complete",
                                  {"checkpoint_id": checkpoint_id, "objects": receipts_out}, credentials, receipts["run_id"], now=now)
        if completed.get("id") != checkpoint_id or completed.get("status") != "verified":
            raise CheckpointRefused("checkpoint verification pending or failed")
    write_state(home, uid, {"checkpoint_id": checkpoint_id, "sequence": begun["sequence"], "files_sha256": digest,
                            "lease_generation": receipts["lease_generation"], "quiesced": quiesced,
                            "verified_at": int(time.time() if now is None else now)})
    return {"status": "verified", "mode": mode, "checkpoint_id": checkpoint_id, "sequence": begun["sequence"], "quiesced": quiesced,
            "archive_size": len(archive), "files": len(manifest["files"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("periodic", "final"), required=True)
    arguments = parser.parse_args(argv)
    if os.geteuid() != 0:
        print(json.dumps({"status": "failed", "mode": arguments.mode, "error": "NotRoot"}))
        return 1
    try:
        result = run_checkpoint(arguments.mode)
    except (InvalidCheckpoint, OSError, ValueError, TypeError, KeyError, URLError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "mode": arguments.mode, "error": type(error).__name__}))
        print(f"Crew host checkpoint failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
