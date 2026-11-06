"""Make Crew configured and running for this box: one idempotent ``ensure`` entry.

The bootstrap (crew-host-bootstrap.ts) asks for ``ensure`` on stdin and acts on one verdict:

* ``ready``       — this run's receipts are already published and Crew was started: nothing to do;
* ``resumed``     — a home this box provisioned before (root-only marker ``.adp-host/provision.json``)
  was re-pinned to the new run and Crew started on it, data untouched;
* ``needs_grant`` — the home is empty and the scope pins a checkpoint: call again with the grant;
* ``restored``    — the empty home was initialised fresh (#8171) or from the pinned checkpoint.

An occupied home without a trusted marker is refused by name (``CrewHomeUnknownProvenance``);
receipts for another run are refused rather than replaced. The older per-mode entries stay
callable for one release (``probe``/``restore``/``resume``) and are what ``ensure`` composes:

* ``probe``   — classify the mounted home: ``empty`` (first provision or a verified
  restore), ``resumable`` (a home this box provisioned before, proven by the root-only
  marker ``.adp-host/provision.json``), or ``unknown`` (occupied, no trusted marker).
* ``restore`` — the original path: an empty home is initialised fresh (#8171) or from the
  pinned checkpoint, then the marker is written so later boots can resume.
* ``resume``  — a same-box restart (#8223 causes 18/19): /run/adp is tmpfs, so a stop/start
  loses every receipt while the retained workspace disk keeps the Crew home. The resume
  re-renders only the run-scoped configuration, republishes the receipts for the new run
  and starts the services. It never fetches checkpoint objects and keeps the home's data.
  A Replace keeps the same disk: the marker is bound to tenant, user and box, not to the
  machine generation, so a replaced box resumes the workspace too (re-pinned to its run).
"""

# The guest runs these helpers on AL2023's system Python (3.9) from systemd units and the
# controller's SSM commands; keep `X | None` annotations lazy so they never evaluate there.
from __future__ import annotations


import hashlib
import json
import os
import pwd
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from crew_checkpoint import InvalidCheckpoint, restore_empty_mount


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class CrewHomeUnknownProvenance(InvalidCheckpoint):
    """The mounted home holds data this box did not provision: refuse rather than adopt it."""


CREW_VERSION = "0.11.2"
# Homes provisioned by these earlier releases resume under the current one: the marker proves
# tenant/user/box ownership, not a Crew version, and Kiro Crew migrates its own home data. The
# first upgrade (0.8.0 -> 0.11.2, dev 2026-11-04) refused every retained owner home as
# CrewHomeUnknownProvenance because the marker was compared against the exact version; a Replace
# onto the retained disk then never activated the host run. A resume rewrites the marker with the
# current version, so the list only has to name releases a box can still be running.
SUPPORTED_PREVIOUS_CREW_VERSIONS = ("0.8.0",)
MARKER_DIRECTORY = ".adp-host"   # excluded from checkpoints (crew_checkpoint.EXCLUDED_ROOTS)
MARKER_FILE = "provision.json"
MARKER_BOUND_FIELDS = ("tenant_id", "user_id", "box_id")
SCOPE_FIELDS = ("run_id", "tenant_id", "user_id", "box_id", "instance_ref", "private_ip",
                "profile_revision", "lease_generation", "restore_manifest_version")
DASHBOARD_ORIGIN_ENV = "ADP_CREW_DASHBOARD_ORIGIN"


_lstat = os.lstat   # module hook so tests can present a home owned by another uid


def _trusted_directory(path: Path, uid: int) -> None:
    entry = _lstat(path)
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != uid or entry.st_mode & 0o022:
        raise InvalidCheckpoint("untrusted Crew host directory")


def _agent_uid(agent_uid, uid: int) -> int:
    if agent_uid is not None:
        return agent_uid
    try:
        return pwd.getpwnam("agent").pw_uid
    except KeyError:
        return uid


def _trusted_home(home: Path, uid: int, agent_uid: int) -> None:
    """The Crew home is root-owned only until it is provisioned; afterwards it belongs to ``agent``.

    ``restore_host`` hands the whole tree to the agent user and Kiro Crew keeps writing to it, so a
    probe or resume of a provisioned home must accept the agent owner (dev 2026-10-20: every real
    same-generation restart was refused with "untrusted Crew host directory" because this check
    demanded root). Group/world write and symlinks are still refused; ``/run/adp`` and the marker
    directory keep requiring root.
    """
    entry = _lstat(home)
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid not in (uid, agent_uid) or entry.st_mode & 0o022:
        raise InvalidCheckpoint("untrusted Crew host directory")


def _publish(path: Path, value: dict, uid: int, group_id: int | None = None) -> None:
    _trusted_directory(path.parent, uid)
    if path.exists() or path.is_symlink():
        raise InvalidCheckpoint("Crew host receipt already exists")
    descriptor, pending = tempfile.mkstemp(prefix=".crew-receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as destination:
            if group_id is not None and os.fstat(destination.fileno()).st_gid != group_id:
                os.fchown(destination.fileno(), uid, group_id)
            os.fchmod(destination.fileno(), 0o640 if group_id is not None else 0o600)
            json.dump(value, destination, sort_keys=True)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(pending, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(pending).unlink(missing_ok=True)


def _trusted_file(path: Path, uid: int) -> bytes:
    _trusted_directory(path.parent, uid)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        entry = os.fstat(descriptor)
        if not stat.S_ISREG(entry.st_mode) or entry.st_uid != uid or entry.st_mode & 0o022 or entry.st_size > 16384:
            raise InvalidCheckpoint("untrusted Crew host file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


def _fetch_versioned(objects: dict, bucket: str, key: str, version: str, *, opener) -> bytes:
    matches = [item for item in objects.values() if (item.get("bucket"), item.get("key"), item.get("version_id")) ==
               (bucket, key, version)]
    if len(matches) != 1:
        raise InvalidCheckpoint("unrecognized restore object")
    item = matches[0]
    target = urlsplit(item.get("url", ""))
    size = item.get("size")
    limit = 1024 * 1024 if item is objects["manifest"] else 512 * 1024 * 1024
    if (target.scheme != "https" or not target.netloc or target.username or target.password
            or target.fragment or not target.query or target.hostname is None
            or not (target.hostname.endswith(".amazonaws.com") or target.hostname.endswith(".amazonaws.com.cn"))
            or type(size) is not int or not 0 < size <= limit
            or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))):
        raise InvalidCheckpoint("invalid restore object grant")
    with opener.open(Request(item["url"], method="GET"), timeout=60) as response:
        if response.headers.get("x-amz-version-id") != version:
            raise InvalidCheckpoint("restore version mismatch")
        content = response.read(size + 1)
    if len(content) != size or hashlib.sha256(content).hexdigest() != item["sha256"]:
        raise InvalidCheckpoint("restore checksum mismatch")
    return content


def _validate_scope(scope: dict, grant, *, allow_grantless: bool = False) -> bool:
    """Return whether the scope is fresh; raise when it is neither fresh nor a pinned restore."""
    fields = SCOPE_FIELDS
    if (set(scope) != set(fields) | {"checkpoint_id", "manifest_sha256"}
            or any(not isinstance(scope[field], str) or not scope[field] for field in fields[:7])
            or type(scope["lease_generation"]) is not int or scope["lease_generation"] < 1
            or type(scope["restore_manifest_version"]) is not int):
        raise InvalidCheckpoint("invalid host restore scope")
    # A first host has nothing to restore (#8171): a fresh scope carries no checkpoint pins,
    # manifest version 0 and no grant, and starts Crew on an empty, owner-scoped home.
    fresh = scope["checkpoint_id"] is None
    if fresh:
        if scope["manifest_sha256"] is not None or scope["restore_manifest_version"] != 0 or grant is not None:
            raise InvalidCheckpoint("invalid fresh host scope")
    elif ((grant is None and not allow_grantless) or scope["restore_manifest_version"] < 1
            or not isinstance(scope["checkpoint_id"], str) or not scope["checkpoint_id"]
            or not isinstance(scope["manifest_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", scope["manifest_sha256"])):
        raise InvalidCheckpoint("invalid host restore scope")
    return fresh


def _verify_authority(scope: dict, root: Path, uid: int) -> None:
    """The receipts the bootstrap published this boot must name exactly this scope."""
    authority = json.loads(_trusted_file(root / "crew-host.json", uid))
    model = json.loads(_trusted_file(root / "crew-model.json", uid))
    client_ca = _trusted_file(root / "crew-host" / "client-ca.pem", uid).decode("ascii")
    try:
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(client_ca)).hexdigest()
    except ValueError as error:
        raise InvalidCheckpoint("invalid gateway client certificate") from error
    if (any(authority.get(field) != scope[field] for field in SCOPE_FIELDS)
            or authority.get("gateway_fingerprint") != fingerprint
            or model.get("run_id") != scope["run_id"]
            or model.get("lease_generation") != scope["lease_generation"]):
        raise InvalidCheckpoint("host trust or model authority differs from restore scope")


def _require_unready(root: Path) -> None:
    if any((root / name).exists() or (root / name).is_symlink() for name in ("crew-ready", "crew-restore.json")):
        raise InvalidCheckpoint("host provisioning requires an unready box")


def _home_entries(home: Path) -> list:
    return [entry for entry in home.iterdir() if entry.name != MARKER_DIRECTORY]


def read_marker(home: Path, uid: int):
    """The trusted provisioning marker, ``None`` when absent; an untrusted marker is refused."""
    directory = home / MARKER_DIRECTORY
    if not directory.exists() and not directory.is_symlink():
        return None
    entry = directory.lstat()
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != uid or entry.st_mode & 0o077:
        raise CrewHomeUnknownProvenance("untrusted Crew provisioning marker directory")
    path = directory / MARKER_FILE
    if not path.exists() and not path.is_symlink():
        # The readiness chain creates the (trusted, root-only) state directory before the first
        # provisioning — SSH host keys, the owner's home — so a directory without a marker is
        # simply "not provisioned yet", the same as no directory at all.
        return None
    try:
        marker = json.loads(_trusted_file(path, uid))
    except (InvalidCheckpoint, OSError, ValueError) as error:  # a symlinked marker is an OSError (O_NOFOLLOW)
        raise CrewHomeUnknownProvenance("untrusted Crew provisioning marker") from error
    if (not isinstance(marker, dict) or marker.get("schema_version") != 1
            or any(not isinstance(marker.get(field), str) or not marker[field] for field in MARKER_BOUND_FIELDS)):
        raise CrewHomeUnknownProvenance("invalid Crew provisioning marker")
    return marker


def _marker_matches(marker: dict, scope: dict) -> bool:
    return (marker.get("crew_version") in (CREW_VERSION, *SUPPORTED_PREVIOUS_CREW_VERSIONS)
            and all(marker.get(field) == scope[field] for field in MARKER_BOUND_FIELDS))


def write_marker(home: Path, scope: dict, uid: int, *, previous=None, now=None) -> dict:
    """Record (root-only, atomically) that this box provisioned the home; replaces a previous marker."""
    now = int(time.time()) if now is None else now
    directory = home / MARKER_DIRECTORY
    if directory.is_symlink():
        raise CrewHomeUnknownProvenance("untrusted Crew provisioning marker directory")
    if not directory.exists():
        directory.mkdir(mode=0o700)
    os.chown(directory, uid, -1)
    os.chmod(directory, 0o700)
    marker = {
        "schema_version": 1, "crew_version": CREW_VERSION,
        "tenant_id": scope["tenant_id"], "user_id": scope["user_id"], "box_id": scope["box_id"],
        "lease_generation": scope["lease_generation"], "instance_ref": scope["instance_ref"],
        "profile_revision": scope["profile_revision"],
        "provisioned_from": {"checkpoint_id": scope["checkpoint_id"], "manifest_sha256": scope["manifest_sha256"]}
        if previous is None else previous.get("provisioned_from"),
        "provisioned_at": now if previous is None else previous.get("provisioned_at", now),
        "resumed_at": None if previous is None else now,
        "resume_count": 0 if previous is None else int(previous.get("resume_count", 0)) + 1,
        "run_id_sha256": hashlib.sha256(scope["run_id"].encode()).hexdigest(),
    }
    descriptor, pending = tempfile.mkstemp(prefix=".provision-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as destination:
            os.fchmod(destination.fileno(), 0o600)
            json.dump(marker, destination, sort_keys=True)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(pending, directory / MARKER_FILE)
    finally:
        Path(pending).unlink(missing_ok=True)
    return marker


def probe_home(scope: dict, *, home: Path = Path("/mnt/crew"), root: Path = Path("/run/adp"), uid: int = 0,
               agent_uid: int | None = None) -> str:
    """Classify the mounted home for the bootstrap: ``empty``, ``resumable`` or ``unknown``."""
    _validate_scope(scope, None, allow_grantless=True)
    _trusted_directory(root, uid)
    _trusted_home(home, uid, _agent_uid(agent_uid, uid))
    try:
        marker = read_marker(home, uid)
    except CrewHomeUnknownProvenance:
        return "unknown"
    if marker is None:
        return "empty" if not _home_entries(home) else "unknown"
    return "resumable" if _marker_matches(marker, scope) else "unknown"


def _activate(scope: dict, root: Path, uid: int, agent_gid: int, run) -> None:
    _publish(root / "crew-restore.json", {"checkpoint_id": scope["checkpoint_id"],
                                        "manifest_sha256": scope["manifest_sha256"]}, uid)
    ready = {field: scope[field] for field in SCOPE_FIELDS}
    _publish(root / "crew-ready", ready, uid, agent_gid)
    try:
        run(["systemctl", "start", "adp-crew.service"], check=True)
        run(["systemctl", "start", "adp-crew-host.service"], check=True)
        # Checkpoints from the box (#8379): the shutdown capture placeholder and the periodic
        # timer are started only once the host is verified; their units stay inert otherwise.
        run(["systemctl", "start", "adp-crew-checkpoint-final.service"], check=True)
        run(["systemctl", "start", "adp-crew-checkpoint.timer"], check=True)
    except Exception:
        (root / "crew-ready").unlink(missing_ok=True)
        for unit in ("adp-crew-checkpoint.timer", "adp-crew-checkpoint-final.service", "adp-crew-host.service", "adp-crew.service"):
            run(["systemctl", "stop", unit], check=False)
        raise


def resume_host(scope: dict, *, home: Path = Path("/mnt/crew"), root: Path = Path("/run/adp"),
                uid: int = 0, agent_uid: int | None = None, agent_gid: int | None = None,
                run=subprocess.run, configure=None, now=None) -> dict:
    """Re-pin a home this box provisioned before to the new run, then start Crew on it."""
    _validate_scope(scope, None, allow_grantless=True)
    if agent_uid is None:
        agent = pwd.getpwnam("agent")
        agent_uid, agent_gid = agent.pw_uid, agent.pw_gid
    if agent_gid is None:
        raise InvalidCheckpoint("Crew agent group unavailable")
    _trusted_directory(root, uid)
    _trusted_home(home, uid, agent_uid)
    _verify_authority(scope, root, uid)
    _require_unready(root)
    marker = read_marker(home, uid)
    if marker is None:
        raise CrewHomeUnknownProvenance("Crew home was never provisioned by this box")
    if not _marker_matches(marker, scope):
        raise CrewHomeUnknownProvenance("Crew home belongs to another owner or Crew version")
    # Only the run-scoped configuration is re-rendered; the home's memory, sessions and
    # artifacts are the owner's and stay exactly as the previous session left them.
    if configure is not None:
        configure(home)
    marker = write_marker(home, scope, uid, previous=marker, now=now)
    _activate(scope, root, uid, agent_gid, run)
    return marker


def restore_host(scope: dict, grant: dict, *, home: Path = Path("/mnt/crew"), root: Path = Path("/run/adp"),
                 uid: int = 0, agent_uid: int | None = None, agent_gid: int | None = None,
                 opener=None, run=subprocess.run, configure=None, now=None) -> None:
    fresh = _validate_scope(scope, grant)
    if agent_uid is None:
        agent = pwd.getpwnam("agent")
        agent_uid, agent_gid = agent.pw_uid, agent.pw_gid
    if agent_gid is None:
        raise InvalidCheckpoint("Crew agent group unavailable")
    _trusted_directory(root, uid)
    _trusted_home(home, uid, agent_uid)
    _verify_authority(scope, root, uid)
    _require_unready(root)
    if _home_entries(home):
        # An occupied home is never restored over. The bootstrap probes first and resumes a
        # home this box provisioned; anything else is refused with its provenance named. The
        # host's own root-only state directory (SSH host keys, the owner's home) is not content.
        raise (CrewHomeUnknownProvenance if read_marker(home, uid) is None else InvalidCheckpoint)(
            "host restore requires an empty, unready box")
    if not fresh:
        if (grant.get("id") != scope["checkpoint_id"] or grant.get("crew_version") != CREW_VERSION
                or grant.get("schema_version") != 1 or grant.get("manifest_version") != scope["restore_manifest_version"]
                or not isinstance(grant.get("restore_token"), str) or len(grant["restore_token"]) < 32):
            raise InvalidCheckpoint("incompatible host restore grant")
        objects = grant.get("objects")
        if (not isinstance(objects, dict) or set(objects) != {"archive", "manifest"}
                or any(not isinstance(item, dict) for item in objects.values())
                or objects["manifest"].get("sha256") != scope["manifest_sha256"]):
            raise InvalidCheckpoint("host restore pin differs from selected checkpoint")
    if opener is None:
        opener = build_opener(NoRedirect, ProxyHandler({}))

    if not fresh:
        restore_empty_mount(home, objects, lambda bucket, key, version: _fetch_versioned(
            objects, bucket, key, version, opener=opener))
    for directory, folders, files in os.walk(home, followlinks=False):
        for name in folders + files:
            entry = Path(directory) / name
            details = entry.lstat()
            if not (stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode)):
                raise InvalidCheckpoint("unsafe restored Crew entry")
            os.chown(entry, agent_uid, agent_gid)
        os.chown(directory, agent_uid, agent_gid)
    # The marker precedes configuration on purpose: a failed configure leaves a home the
    # next attempt resumes (and re-renders) instead of one it can neither restore nor adopt.
    write_marker(home, scope, uid, now=now)
    if configure is not None:
        configure(home)
    _activate(scope, root, uid, agent_gid, run)


def _receipt_run(root: Path, uid: int):
    """The run id the published readiness receipt names, or ``None`` when none is published."""
    ready = root / "crew-ready"
    if not (ready.exists() or ready.is_symlink()):
        return None
    try:
        value = json.loads(_trusted_file(ready, uid))
    except (InvalidCheckpoint, OSError, ValueError) as error:
        raise InvalidCheckpoint("untrusted Crew readiness receipt") from error
    return value.get("run_id") if isinstance(value, dict) else None


def ensure_host(scope: dict, grant, *, home: Path = Path("/mnt/crew"), root: Path = Path("/run/adp"),
                uid: int = 0, agent_uid: int | None = None, agent_gid: int | None = None,
                opener=None, run=subprocess.run, configure=None, now=None) -> str:
    """Make Crew configured and running for ``scope``; idempotent within a boot.

    Returns ``ready`` | ``resumed`` | ``needs_grant`` | ``restored``. The only decision the
    caller has to make is to fetch the restore grant when asked (#8459, epic #8456).
    """
    _validate_scope(scope, None, allow_grantless=True)
    _trusted_directory(root, uid)
    published = _receipt_run(root, uid)
    if published is not None:
        if published != scope["run_id"]:
            raise InvalidCheckpoint("host receipts belong to another run")
        return "ready"
    verdict = probe_home(scope, home=home, root=root, uid=uid)
    if verdict == "resumable":
        resume_host(scope, home=home, root=root, uid=uid, agent_uid=agent_uid, agent_gid=agent_gid,
                    run=run, configure=configure, now=now)
        return "resumed"
    if verdict != "empty":
        raise CrewHomeUnknownProvenance("Crew home was never provisioned by this box")
    if scope.get("checkpoint_id") is not None and grant is None:
        return "needs_grant"
    restore_host(scope, grant, home=home, root=root, uid=uid, agent_uid=agent_uid, agent_gid=agent_gid,
                 opener=opener, run=run, configure=configure, now=now)
    return "restored"


def configure_arguments(model_id: str, environment=os.environ) -> list:
    """``--configure`` arguments: the model, plus the Crew dashboard origin when the controller gave one.

    Kiro Crew's host validation admits only the Host header derived from ``dashboard.url``; the
    browser reaches the dashboard through the gateway's edge origin, not 127.0.0.1 (#8223 cause 20).
    """
    origin = environment.get(DASHBOARD_ORIGIN_ENV, "")
    if origin and re.fullmatch(r"https://[a-z0-9][a-z0-9.-]{0,252}", origin):
        return ["--configure", model_id, origin]
    return ["--configure", model_id]


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit(1)
    payload = sys.stdin.buffer.read(65_537)
    if len(payload) > 65_536:
        raise SystemExit(1)
    request = json.loads(payload)
    mode = request.pop("mode", "ensure")
    if (set(request) != {"scope", "grant", "model_id"} or mode not in ("ensure", "probe", "restore", "resume")
            or not isinstance(request["model_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", request["model_id"])):
        raise SystemExit(1)

    def configure(_home):
        subprocess.run([
            "runuser", "-u", "agent", "--", "/opt/adp/crew/current/host/node",
            "/opt/adp/crew/current/host/crew-host-bootstrap.js", *configure_arguments(request["model_id"]),
        ], check=True, capture_output=True)

    if mode == "ensure":
        print(ensure_host(request["scope"], request["grant"], configure=configure))
    elif mode == "probe":
        print(probe_home(request["scope"]))
    elif mode == "resume":
        resume_host(request["scope"], configure=configure)
    else:
        restore_host(request["scope"], request["grant"], configure=configure)


def report_refusal(error: BaseException) -> None:
    """One stderr line: class AND message. Messages are fixed constants (no paths, no secrets);
    the controller log and the box status are the only places an operator sees why (#8223)."""
    detail = str(error).replace("\n", " ")[:200]
    print(f"Crew host restore refused: {type(error).__name__}" + (f": {detail}" if detail else ""), file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        report_refusal(error)
        raise SystemExit(1) from None
