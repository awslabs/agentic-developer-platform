"""Offline Crew profile tests; no EC2 instance or systemd daemon is needed."""

import hashlib
import importlib.util
import io
import os
import stat
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "image" / "install_crew.py"
SPEC = importlib.util.spec_from_file_location("dev_box_install_crew", MODULE_PATH)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)

REQUIRED = ("venv/bin/kirocrew", "clients/node_modules/.bin/claude", "clients/node_modules/.bin/codex",
            "clients/node_modules/.bin/claude-agent-acp", "clients/node_modules/.bin/node", "default-agent.json", "host/crew-host-bootstrap.js",
            "models/qwen3-embedding-0.6b-q8_0.gguf", "host/node",
            "host/crew-host-dashboard.js", "host/crew-host-model.js", "host/adp/adp-mcp.py", "host/adp/adp",
            "host/adp/adp_common.py", "host/adp/adp_deployments.py", "host/adp/adp-flow.py", "host/adp/adp-product.py", "host/adp/adp-github.py",
            "host/adp/adp_skills.py", "host/adp/adp-skills.tar.gz", "host/adp/skills/MANIFEST.json")


def bundle(tmp_path, paths=REQUIRED):
    output = tmp_path / "crew.tar.gz"
    with tarfile.open(output, "w:gz") as archive:
        for path in paths:
            data = b"approved-crew-artifact"
            member = tarfile.TarInfo(path)
            member.size = len(data)
            member.mode = 0o755 if "/bin/" in path or "/.bin/" in path else 0o644
            archive.addfile(member, io.BytesIO(data))
    manifest = {"schema_version": 1, "revision": "crew-r1", "source_revision": "a" * 40,
                "crew_version": "0.11.2", "bundle_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    return output, manifest


def runner(calls):
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1 if command[:2] == ["id", "-u"] else 0)
    return run


def test_installs_owner_service_once_and_reruns_without_touching_workspace(tmp_path):
    archive, manifest = bundle(tmp_path)
    prefix, units = tmp_path / "opt", tmp_path / "units"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "memory.db").write_text("retained")
    calls = []
    first = INSTALLER.install(manifest, archive, prefix=prefix, unit_dir=units, run=runner(calls))
    assert first == {"profile_revision": "crew-r1", "crew_version": "0.11.2",
                     "bundle_sha256": manifest["bundle_sha256"], "service": "adp-crew.service",
                     "host_service": "adp-crew-host.service", "checkpoint_timer": "adp-crew-checkpoint.timer"}
    assert (prefix / "current").readlink() == Path("crew-r1")
    assert (prefix / "crew-r1" / "venv/bin/kirocrew").is_file()
    assert calls.count(["systemctl", "daemon-reload"]) == 1
    assert ["useradd", "-M", "-d", "/mnt/crew", "-s", "/usr/sbin/nologin", "agent"] in calls
    assert INSTALLER.install(manifest, archive, prefix=prefix, unit_dir=units, run=runner(calls)) == first
    assert calls.count(["systemctl", "daemon-reload"]) == 1
    assert (workspace / "memory.db").read_text() == "retained"
    unit = (units / "adp-crew.service").read_text()
    assert "User=agent" in unit and "ConditionPathExists=/run/adp/crew-ready" in unit
    assert "ConditionPathExists=/run/adp/crew-host.json" in unit
    assert "ConditionPathExists=/run/adp/crew-model.json" in unit
    # Both units share the host's mount namespace: Kiro Crew admits the owner-token mint only from
    # a peer in its own user AND mount namespace, and the custody lives in the host service (#8223).
    for directive in ("PrivateTmp=", "ProtectSystem=", "ReadWritePaths=", "ReadOnlyPaths="):
        assert directive not in unit, directive
    assert "NoNewPrivileges=false" in unit
    assert "ExecStopPost=+/usr/bin/python3 -I /opt/adp/crew/publish_shell_model.py --remove" in unit
    assert "ExecStart=/opt/adp/crew/current/host/node /opt/adp/crew/current/host/crew-host-model.js" in unit
    # #8223 cause 31: the Claude agent adapter is a "#!/usr/bin/env node" script; the guest has no
    # system Node, so the bundle's interpreter directory must lead PATH for Kiro Crew and its children.
    assert "Environment=PATH=/opt/adp/crew/current/host:/opt/adp/crew/current/clients/node_modules/.bin:" in unit
    host_unit = (units / "adp-crew-host.service").read_text()
    assert "Requires=adp-crew.service" in host_unit
    assert "ConditionPathExists=/run/adp/crew-host.json" in host_unit
    assert "ExecStartPre=+/usr/bin/python3 /opt/adp/crew/write_host_certificate.py" in host_unit
    for directive in ("PrivateTmp=", "ProtectSystem=", "ReadWritePaths=", "ReadOnlyPaths="):
        assert directive not in host_unit, directive
    assert "NoNewPrivileges=true" in host_unit
    assert "ExecStart=/opt/adp/crew/current/host/node /opt/adp/crew/current/host/crew-host-dashboard.js" in host_unit
    assert "Environment=PATH=/opt/adp/crew/current/host:/opt/adp/crew/current/clients/node_modules/.bin:" in host_unit
    # Checkpoints from the box (#8379): a periodic timer and a shutdown-time final capture, both
    # gated on the verified-host receipts and ordered so Crew is quiet and the network still up.
    checkpoint = (units / "adp-crew-checkpoint.service").read_text()
    assert "ExecStart=/usr/bin/python3 -I /opt/adp/crew/checkpoint_crew_host.py --mode periodic" in checkpoint
    assert "ConditionPathExists=/run/adp/crew-ready" in checkpoint and "Type=oneshot" in checkpoint
    timer = (units / "adp-crew-checkpoint.timer").read_text()
    assert "OnUnitActiveSec=15min" in timer and "Unit=adp-crew-checkpoint.service" in timer
    final = (units / "adp-crew-checkpoint-final.service").read_text()
    assert "ExecStop=/usr/bin/python3 -I /opt/adp/crew/checkpoint_crew_host.py --mode final" in final
    assert "Before=adp-crew.service" in final and "After=network-online.target" in final
    assert "RemainAfterExit=yes" in final and "TimeoutStopSec=150" in final
    for unit_text in (checkpoint, final):
        assert "User=" not in unit_text  # root: reads the root-only receipts and the agent's home


def test_interrupted_install_resumes_and_failed_bundle_preserves_current(tmp_path, monkeypatch):
    archive, manifest = bundle(tmp_path)
    prefix, units = tmp_path / "opt", tmp_path / "units"
    real_unpack = INSTALLER._unpack

    def interrupted(*args):
        raise OSError("interrupted extraction")

    monkeypatch.setattr(INSTALLER, "_unpack", interrupted)
    with pytest.raises(OSError, match="interrupted extraction"):
        INSTALLER.install(manifest, archive, prefix=prefix, unit_dir=units, run=runner([]))
    assert not (prefix / "current").exists() and not list(prefix.glob(".staging-*"))
    monkeypatch.setattr(INSTALLER, "_unpack", real_unpack)
    abandoned = prefix / ".staging-interrupted"
    abandoned.mkdir()
    (abandoned / "partial").write_text("unfinished")
    INSTALLER.install(manifest, archive, prefix=prefix, unit_dir=units, run=runner([]))
    archive.write_bytes(archive.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        INSTALLER.install({**manifest, "revision": "crew-r2"}, archive,
                          prefix=prefix, unit_dir=units, run=runner([]))
    assert (prefix / "current").readlink() == Path("crew-r1")
    assert not abandoned.exists()


def test_upgrade_refuses_unverified_workspace_migration_without_changing_profile_or_memory(tmp_path):
    archive, manifest = bundle(tmp_path)
    prefix, units, workspace = tmp_path / "opt", tmp_path / "units", tmp_path / "workspace"
    workspace.mkdir()
    calls = []
    INSTALLER.install(manifest, archive, prefix=prefix, unit_dir=units, workspace=workspace, run=runner(calls))
    (workspace / "memory.db").write_bytes(b"owner memory")
    (workspace / "workspace").mkdir()
    (workspace / "workspace" / "correction.md").write_bytes(b"acknowledged correction")
    prior_units = {unit.name: unit.read_bytes() for unit in units.iterdir()}
    upgraded = {**manifest, "revision": "crew-r2"}

    with pytest.raises(ValueError, match="crew_migration_blocked"):
        INSTALLER.install(upgraded, archive, prefix=prefix, unit_dir=units, workspace=workspace, run=runner(calls))

    assert (prefix / "current").readlink() == Path("crew-r1")
    assert not (prefix / "crew-r2").exists()
    assert (workspace / "memory.db").read_bytes() == b"owner memory"
    assert (workspace / "workspace" / "correction.md").read_bytes() == b"acknowledged correction"
    assert {unit.name: unit.read_bytes() for unit in units.iterdir()} == prior_units
    assert calls.count(["systemctl", "daemon-reload"]) == 1


def test_failed_daemon_reload_restores_previous_units_and_current_profile(tmp_path):
    archive, manifest = bundle(tmp_path)
    prefix, units, workspace = tmp_path / "opt", tmp_path / "units", tmp_path / "workspace"
    workspace.mkdir()
    INSTALLER.install(manifest, archive, prefix=prefix, unit_dir=units, workspace=workspace, run=runner([]))
    old_units = {unit.name: f"previous {unit.name}\n" for unit in units.iterdir()}
    for name, content in old_units.items():
        (units / name).write_text(content)
    calls = []

    def fail_first_reload(command, **kwargs):
        calls.append(command)
        if command == ["systemctl", "daemon-reload"] and calls.count(command) == 1:
            raise OSError("systemd reload failed")
        return SimpleNamespace(returncode=0)

    with pytest.raises(OSError, match="systemd reload failed"):
        INSTALLER.install({**manifest, "revision": "crew-r2"}, archive, prefix=prefix, unit_dir=units,
                          workspace=workspace, run=fail_first_reload)

    assert (prefix / "current").readlink() == Path("crew-r1")
    assert {name: (units / name).read_text() for name in old_units} == old_units
    assert calls.count(["systemctl", "daemon-reload"]) == 2
    assert not list(units.glob("*.tmp"))
    INSTALLER.install({**manifest, "revision": "crew-r2"}, archive, prefix=prefix, unit_dir=units,
                      workspace=workspace, run=runner([]))
    assert (prefix / "current").readlink() == Path("crew-r2")


@pytest.mark.parametrize("missing", ["host/node", "clients/node_modules/.bin/node", "host/crew-host-dashboard.js", "host/crew-host-model.js", "host/crew-host-bootstrap.js", "host/adp/adp-mcp.py", "host/adp/adp", "host/adp/adp-skills.tar.gz", "host/adp/skills/MANIFEST.json"])
def test_missing_pinned_host_transport_is_not_published(tmp_path, missing):
    archive, manifest = bundle(tmp_path, paths=tuple(path for path in REQUIRED if path != missing))
    with pytest.raises(ValueError, match="incomplete"):
        INSTALLER.install(manifest, archive, prefix=tmp_path / "opt", unit_dir=tmp_path / "units", run=runner([]))
    assert not (tmp_path / "opt/current").exists()


def test_incomplete_and_unsafe_archives_are_not_published(tmp_path):
    archive, manifest = bundle(tmp_path, paths=REQUIRED[:-1])
    with pytest.raises(ValueError, match="incomplete"):
        INSTALLER.install(manifest, archive, prefix=tmp_path / "opt", unit_dir=tmp_path / "units", run=runner([]))
    assert not (tmp_path / "opt/current").exists()
    archive, manifest = bundle(tmp_path, paths=["../../escape"])
    with pytest.raises(ValueError):
        INSTALLER.install(manifest, archive, prefix=tmp_path / "opt", unit_dir=tmp_path / "units", run=runner([]))
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("change", [
    {"revision": "../other"}, {"crew_version": "latest"}, {"schema_version": 2},
    {"bundle_sha256": "latest"}, {"model_api_key": "should-not-be-here"},
])
def test_rejects_unpinned_or_secret_profile_fields(tmp_path, change):
    archive, manifest = bundle(tmp_path)
    with pytest.raises(ValueError):
        INSTALLER.install({**manifest, **change}, archive, prefix=tmp_path / "opt",
                          unit_dir=tmp_path / "units", run=runner([]))
    assert not (tmp_path / "opt").exists()


def test_service_contains_no_static_authority_or_public_listener():
    unit = INSTALLER.UNIT.lower()
    for forbidden in ("token=", "api_key=", "secret=", "0.0.0.0", "bedrock", "aws_access_key"):
        assert forbidden not in unit
    assert "host/crew-host-model.js" in unit
    host = INSTALLER.HOST_UNIT.lower()
    for forbidden in ("token=", "api_key=", "secret=", "0.0.0.0", "bedrock", "aws_access_key"):
        assert forbidden not in host
    assert "conditionpathexists=/run/adp/crew-ready" in host


@pytest.mark.parametrize("pins", [
    {"expected_source_revision": "b" * 40},
    {"expected_profile_revision": "crew-r2"},
])
def test_profile_image_cannot_install_a_different_approved_revision(tmp_path, pins):
    archive, manifest = bundle(tmp_path)
    with pytest.raises(ValueError, match="approved image inputs"):
        INSTALLER.install(manifest, archive, prefix=tmp_path / "opt", unit_dir=tmp_path / "units",
                          run=runner([]), **pins)
    assert not (tmp_path / "opt").exists()
    assert not (tmp_path / "units").exists()


def test_profile_image_installs_matching_source_and_bundle(tmp_path):
    archive, manifest = bundle(tmp_path)
    receipt = INSTALLER.install(manifest, archive, prefix=tmp_path / "opt", unit_dir=tmp_path / "units",
                                run=runner([]), expected_source_revision="a" * 40,
                                expected_profile_revision="crew-r1")
    assert receipt["bundle_sha256"] == manifest["bundle_sha256"]
    assert (tmp_path / "opt/current/profile.json").is_file()


def test_image_provisioner_passes_release_pins_to_installer_cli(tmp_path, monkeypatch, capsys):
    import json
    import sys

    archive, manifest = bundle(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(manifest))
    seen = []

    def capture_install(approved, local_bundle, **pins):
        seen.append((approved, local_bundle, pins))
        return {"profile_revision": approved["revision"]}

    monkeypatch.setattr(INSTALLER, "install", capture_install)
    monkeypatch.setattr(sys, "argv", ["install_crew.py", "--manifest", str(profile), "--bundle", str(archive),
                                   "--expected-source-revision", "a" * 40,
                                   "--expected-profile-revision", "crew-r1"])
    INSTALLER.main()
    assert seen == [(manifest, archive, {"expected_source_revision": "a" * 40,
                                        "expected_profile_revision": "crew-r1"})]
    assert json.loads(capsys.readouterr().out) == {"profile_revision": "crew-r1"}


def test_install_under_the_first_boot_umask_leaves_the_bundle_usable_by_the_agent_user(tmp_path):
    """crew-bootstrap.sh runs with umask 077. Extracted that way every directory is 0700 and the
    agent user cannot execute node or kirocrew (dev 2026-10-19, #8223). The installer must
    normalise the tree: directories 0755, files world-readable, execute bits preserved, and the
    prefix and its parent traversable."""
    archive, manifest = bundle(tmp_path)
    prefix, units = tmp_path / "opt" / "crew", tmp_path / "units"
    previous = os.umask(0o077)
    try:
        INSTALLER.install(manifest, archive, prefix=prefix, unit_dir=units, run=runner([]))
    finally:
        os.umask(previous)
    revision = prefix / "crew-r1"
    for ancestor in (prefix.parent, prefix, revision):
        assert stat.S_IMODE(ancestor.stat().st_mode) == 0o755, ancestor
    for directory, _folders, files in os.walk(revision):
        assert stat.S_IMODE(Path(directory).stat().st_mode) == 0o755, directory
        for name in files:
            mode = stat.S_IMODE((Path(directory) / name).stat().st_mode)
            assert mode & 0o444 == 0o444, (directory, name, oct(mode))
    assert stat.S_IMODE((revision / "venv/bin/kirocrew").stat().st_mode) == 0o755
    assert stat.S_IMODE((revision / "default-agent.json").stat().st_mode) == 0o644
