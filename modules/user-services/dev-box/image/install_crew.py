"""Install an approved, locally supplied Crew bundle without touching owner data."""

# The guest runs these helpers on AL2023's system Python (3.9) from systemd units and the
# controller's SSM commands; keep `X | None` annotations lazy so they never evaluate there.
from __future__ import annotations


import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path


UNIT = """[Unit]
Description=Owner Crew dashboard
After=network-online.target
ConditionPathExists=/run/adp/crew-ready
ConditionPathExists=/run/adp/crew-host.json
ConditionPathExists=/run/adp/crew-model.json

[Service]
# No PrivateTmp/ProtectSystem/Read*Paths here or on the host service: Kiro Crew admits the
# owner-token bootstrap only from a process in its own user AND mount namespace
# (local_owner_bootstrap_allowed), and the dashboard custody that mints the link runs in
# adp-crew-host.service. Separate sandboxes made every mint 403 (dev 2026-10-19, #8223).
# Filesystem exposure is bounded by User=agent, the bundle permissions and /run/adp modes.
User=agent
Group=agent
WorkingDirectory=/mnt/crew
RuntimeDirectory=adp-crew-aws
RuntimeDirectoryMode=0755
Environment=HOME=/mnt/crew
Environment=KIROCREW_HOME=/mnt/crew
# host/ first: the Claude agent adapter Kiro Crew spawns is a "#!/usr/bin/env node" script and the
# guest has no system Node (#8223 cause 31); the bundle also links clients/node_modules/.bin/node.
Environment=PATH=/opt/adp/crew/current/host:/opt/adp/crew/current/clients/node_modules/.bin:/opt/adp/crew/current/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/adp/crew/current/host/node /opt/adp/crew/current/host/crew-host-model.js
Restart=on-failure
ExecStopPost=+/usr/bin/python3 -I /opt/adp/crew/publish_shell_model.py --remove
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
"""

HOST_UNIT = """[Unit]
Description=Private owner Crew dashboard ingress
After=adp-crew.service
Requires=adp-crew.service
ConditionPathExists=/run/adp/crew-ready
ConditionPathExists=/run/adp/crew-host.json
ConditionPathExists=/run/adp/crew-host/client-ca.pem

[Service]
User=agent
Group=agent
WorkingDirectory=/mnt/crew
Environment=HOME=/mnt/crew
Environment=PATH=/opt/adp/crew/current/host:/opt/adp/crew/current/clients/node_modules/.bin:/opt/adp/crew/current/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStartPre=+/usr/bin/python3 /opt/adp/crew/write_host_certificate.py
ExecStart=/opt/adp/crew/current/host/node /opt/adp/crew/current/host/crew-host-dashboard.js
Restart=on-failure
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""

# Checkpoints from the box (#8379). The timer captures the allowlisted Crew home every
# CHECKPOINT_INTERVAL while the host is ready; the "final" unit is a placeholder whose ExecStop
# runs the quiesced last capture at shutdown. Both are started by restore_crew_host.py once the
# host is verified (ConditionPathExists keeps a never-verified boot inert). Ordering: the final
# unit is Before= the Crew service, so at shutdown systemd stops Crew first (the home is quiet)
# and runs this ExecStop while the network is still up (After=network-online.target).
CHECKPOINT_INTERVAL = "15min"
CHECKPOINT_FINAL_TIMEOUT = "150"
CHECKPOINT_UNIT = """[Unit]
Description=Owner Crew checkpoint capture
ConditionPathExists=/run/adp/crew-ready
ConditionPathExists=/run/adp/crew-host.json

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I /opt/adp/crew/checkpoint_crew_host.py --mode periodic
NoNewPrivileges=true
"""

CHECKPOINT_TIMER = f"""[Unit]
Description=Owner Crew checkpoint schedule

[Timer]
OnBootSec={CHECKPOINT_INTERVAL}
OnUnitActiveSec={CHECKPOINT_INTERVAL}
AccuracySec=1min
Unit=adp-crew-checkpoint.service

[Install]
WantedBy=timers.target
"""

CHECKPOINT_FINAL_UNIT = f"""[Unit]
Description=Owner Crew checkpoint before shutdown
After=network-online.target
Before=adp-crew.service
ConditionPathExists=/run/adp/crew-ready
ConditionPathExists=/run/adp/crew-host.json

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
ExecStop=/usr/bin/python3 -I /opt/adp/crew/checkpoint_crew_host.py --mode final
TimeoutStopSec={CHECKPOINT_FINAL_TIMEOUT}
NoNewPrivileges=true
"""

UNITS = (("adp-crew.service", UNIT), ("adp-crew-host.service", HOST_UNIT),
         ("adp-crew-checkpoint.service", CHECKPOINT_UNIT), ("adp-crew-checkpoint.timer", CHECKPOINT_TIMER),
         ("adp-crew-checkpoint-final.service", CHECKPOINT_FINAL_UNIT))


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _profile(manifest: dict) -> tuple[str, str]:
    if set(manifest) != {"schema_version", "revision", "source_revision", "crew_version", "bundle_sha256"}:
        raise ValueError("unexpected Crew profile fields")
    if manifest["schema_version"] != 1 or manifest["crew_version"] != "0.11.2":
        raise ValueError("unsupported Crew profile or runtime")
    revision = manifest["revision"]
    checksum = manifest["bundle_sha256"]
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", revision):
        raise ValueError("invalid Crew profile revision")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("invalid Crew bundle digest")
    if not isinstance(manifest["source_revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", manifest["source_revision"]):
        raise ValueError("Crew source must be pinned to a full revision")
    return revision, checksum


def _expose(root: Path) -> None:
    """Make an extracted tree usable by the unprivileged ``agent`` user.

    The first-boot script runs under ``umask 077`` (its inputs include secrets), so tar
    extraction and ``mkdir`` produce 0700 directories: on dev 2026-10-19 all 3113 bundle
    directories were root-only and ``runuser -u agent -- node`` failed with "Permission
    denied", so Crew could never start (#8223). Directories become 0755 and files gain
    world-read; execute bits and everything else stay as the bundle shipped them. Symlinks
    are left alone (their targets are normalised in the same walk).
    """
    for directory, folders, files in os.walk(root, followlinks=False):
        current = Path(directory)
        if not current.is_symlink():
            os.chmod(current, 0o755)
        for name in files:
            entry = current / name
            if entry.is_symlink():
                continue
            mode = stat.S_IMODE(entry.lstat().st_mode)
            if mode & 0o444 != 0o444:
                os.chmod(entry, mode | 0o444)


def _unpack(bundle: Path, destination: Path) -> None:
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        if any(not (member.isfile() or member.isdir() or member.issym()) for member in members):
            raise ValueError("Crew bundle cannot contain devices or hard links")
        try:
            archive.extractall(destination, members=members, filter="data")
        except tarfile.FilterError as error:
            raise ValueError("Crew bundle contains unsafe paths") from error
    _expose(destination)
    required = ("venv/bin/kirocrew", "clients/node_modules/.bin/claude", "clients/node_modules/.bin/codex",
                "clients/node_modules/.bin/claude-agent-acp", "clients/node_modules/.bin/node", "default-agent.json",
                "models/qwen3-embedding-0.6b-q8_0.gguf", "host/node",
                "host/crew-host-dashboard.js", "host/crew-host-model.js", "host/crew-host-bootstrap.js",
                "host/adp/adp-mcp.py", "host/adp/adp", "host/adp/adp_common.py", "host/adp/adp_deployments.py",
                "host/adp/adp-flow.py", "host/adp/adp-product.py", "host/adp/adp-github.py",
                "host/adp/adp_skills.py", "host/adp/adp-skills.tar.gz", "host/adp/skills/MANIFEST.json")
    if any(not (destination / path).is_file() for path in required):
        raise ValueError("Crew bundle is incomplete")


def install(manifest: dict, bundle: Path, *, prefix: Path = Path("/opt/adp/crew"),
            unit_dir: Path = Path("/etc/systemd/system"), workspace: Path = Path("/mnt/crew"), run=subprocess.run,
            expected_source_revision: str | None = None, expected_profile_revision: str | None = None) -> dict:
    revision, checksum = _profile(manifest)
    if (expected_source_revision is not None and manifest["source_revision"] != expected_source_revision
            or expected_profile_revision is not None and revision != expected_profile_revision):
        raise ValueError("Crew profile differs from approved image inputs")
    if not bundle.is_file() or _digest(bundle) != checksum:
        raise ValueError("Crew bundle checksum mismatch")
    if prefix.is_symlink() or unit_dir.is_symlink():
        raise ValueError("installation paths cannot be links")
    prefix.mkdir(parents=True, exist_ok=True)
    unit_dir.mkdir(parents=True, exist_ok=True)
    # Same umask: the prefix and its parent must stay traversable for the agent user.
    for ancestor in (prefix, prefix.parent):
        if not ancestor.is_symlink():
            os.chmod(ancestor, 0o755)
    with (prefix / ".install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = prefix / "current"
        if current.exists() and not current.is_symlink():
            raise ValueError("Crew current profile is not a link")
        if current.is_symlink() and os.readlink(current) != revision and (
                workspace.is_symlink() or not workspace.is_dir() or any(workspace.iterdir())):
            raise ValueError("crew_migration_blocked: workspace compatibility is not verified")
        for abandoned in prefix.glob(".staging-*"):
            if abandoned.is_dir() and not abandoned.is_symlink():
                shutil.rmtree(abandoned)
        target = prefix / revision
        if target.is_symlink():
            raise ValueError("Crew revision cannot be a link")
        if target.exists() and (target / "profile.json").read_text() != json.dumps(manifest, sort_keys=True) + "\n":
            raise ValueError("Crew revision already contains another bundle")
        if not target.exists():
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=prefix))
            try:
                _unpack(bundle, staging)
                (staging / "profile.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
                os.chmod(staging / "profile.json", 0o644)  # written under umask 077; not a secret
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        if run(["id", "-u", "agent"], check=False, capture_output=True).returncode:
            run(["useradd", "-M", "-d", "/mnt/crew", "-s", "/usr/sbin/nologin", "agent"], check=True)
        prior_units = []
        try:
            for name, content in UNITS:
                unit = unit_dir / name
                previous = unit.read_text() if unit.exists() else None
                if previous != content:
                    pending_unit = unit_dir / f"{name}.tmp"
                    pending_unit.write_text(content)
                    os.replace(pending_unit, unit)
                    prior_units.append((unit, previous))
            if prior_units:
                run(["systemctl", "daemon-reload"], check=True)
        except Exception:
            for unit, previous in reversed(prior_units):
                if previous is None:
                    unit.unlink()
                else:
                    pending_unit = unit_dir / f"{unit.name}.tmp"
                    pending_unit.write_text(previous)
                    os.replace(pending_unit, unit)
            if prior_units:
                run(["systemctl", "daemon-reload"], check=True)
            raise
        if not current.is_symlink() or os.readlink(current) != revision:
            pending = prefix / ".current.tmp"
            pending.unlink(missing_ok=True)
            pending.symlink_to(revision)
            os.replace(pending, current)
        return {"profile_revision": revision, "crew_version": manifest["crew_version"],
                "bundle_sha256": checksum, "service": "adp-crew.service",
                "host_service": "adp-crew-host.service", "checkpoint_timer": "adp-crew-checkpoint.timer"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-source-revision")
    parser.add_argument("--expected-profile-revision")
    arguments = parser.parse_args()
    print(json.dumps(install(json.loads(arguments.manifest.read_text()), arguments.bundle,
                             expected_source_revision=arguments.expected_source_revision,
                             expected_profile_revision=arguments.expected_profile_revision), sort_keys=True))


if __name__ == "__main__":
    main()
