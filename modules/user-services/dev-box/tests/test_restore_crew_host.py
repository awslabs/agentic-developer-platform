"""The box does not claim readiness without a matching, verified restore — or a proven resume (#8223)."""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from pathlib import Path

import pytest

IMAGE = Path(__file__).resolve().parents[1] / "image"
sys.path.insert(0, str(IMAGE.parents[2] / "agent-factory" / "agent" / "scripts"))
SPEC = importlib.util.spec_from_file_location("restore_crew_host", IMAGE / "restore_crew_host.py")
RESTORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESTORE)
VERIFY_SPEC = importlib.util.spec_from_file_location("verify_restored_crew_host", IMAGE / "verify_crew_ready.py")
VERIFY = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(VERIFY)


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    trust = root / "crew-host"
    trust.mkdir(mode=0o700)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    certificate = tmp_path / "client.pem"
    key = tmp_path / "client.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=test-client", "-keyout", str(key), "-out", str(certificate)],
                   capture_output=True, check=True)
    pem = certificate.read_text()
    (trust / "client-ca.pem").write_text(pem)
    scope = {"run_id": "example-run", "tenant_id": "tenant", "user_id": "owner", "box_id": "box",
             "instance_ref": "i-11111111", "private_ip": "10.2.3.4", "profile_revision": "revision",
             "lease_generation": 2, "restore_manifest_version": 7, "checkpoint_id": "checkpoint",
             "manifest_sha256": ""}
    authority = {name: value for name, value in scope.items() if name not in ("checkpoint_id", "manifest_sha256")}
    authority["gateway_fingerprint"] = hashlib.sha256(RESTORE.ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    (root / "crew-host.json").write_text(json.dumps(authority))
    (root / "crew-model.json").write_text(json.dumps({"run_id": scope["run_id"], "lease_generation": 2}))
    archive_stream = io.BytesIO()
    with tarfile.open(fileobj=archive_stream, mode="w") as archive:
        content = b"verified Crew state"
        entry = tarfile.TarInfo("memory.db")
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    archived = archive_stream.getvalue()
    manifest = json.dumps({"format": 1, "crew_version": "0.11.2", "archive_sha256": hashlib.sha256(archived).hexdigest(),
                           "files": {"memory.db": {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}}}).encode()
    scope["manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
    objects = {label: {"bucket": "example-bucket", "key": label, "version_id": "version-1",
                       "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                       "url": f"https://example-bucket.s3.amazonaws.com/{label}?versionId=version-1"}
               for label, data in (("archive", archived), ("manifest", manifest))}
    grant = {"id": "checkpoint", "crew_version": "0.11.2", "schema_version": 1,
             "manifest_version": 7, "restore_token": "a" * 32, "objects": objects}

    class Responses:
        def open(self, request, timeout):
            assert timeout == 60
            name = request.full_url.split("?", 1)[0].split("/")[-1]
            if name not in ("archive", "manifest"):
                raise ValueError("unrecognized restore object")
            return Response(archived if name == "archive" else manifest)

    class Response:
        def __init__(self, content):
            self.content = content
            self.headers = {"x-amz-version-id": "version-1"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, length):
            return self.content[:length]

    return root, home, scope, grant, Responses()


def invoke(host, **options):
    root, home, scope, grant, opener = host
    commands = []

    def service(command, *, check):
        commands.append(command)
        if options.get("fail_start") and command == ["systemctl", "start", "adp-crew-host.service"]:
            raise subprocess.CalledProcessError(1, command)

    RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(),
                         agent_uid=os.getuid(), agent_gid=os.getgid(), opener=opener, run=service)
    return commands


def test_verified_exact_version_writes_root_owned_scope_and_starts_services(host):
    root, home, scope, _, _ = host
    assert invoke(host) == [["systemctl", "start", "adp-crew.service"],
                            ["systemctl", "start", "adp-crew-host.service"],
                            ["systemctl", "start", "adp-crew-checkpoint-final.service"],
                            ["systemctl", "start", "adp-crew-checkpoint.timer"]]
    assert (home / "memory.db").read_bytes() == b"verified Crew state"
    assert json.loads((root / "crew-ready").read_text())["lease_generation"] == 2
    assert json.loads((root / "crew-restore.json").read_text()) == {
        "checkpoint_id": scope["checkpoint_id"], "manifest_sha256": scope["manifest_sha256"]}
    assert (root / "crew-ready").stat().st_mode & 0o777 == 0o640
    profile = root / "profile.json"
    profile.write_text(json.dumps({"revision": scope["profile_revision"]}))
    expected = {field: value for field, value in scope.items() if field != "run_id"}
    expected["run_id_sha256"] = hashlib.sha256(scope["run_id"].encode()).hexdigest()
    active = lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
    assert VERIFY.verify(expected, root=root, profile=profile, uid=os.getuid(), run=active)
    expected["run_id_sha256"] = hashlib.sha256(b"retired-run").hexdigest()
    assert not VERIFY.verify(expected, root=root, profile=profile, uid=os.getuid(), run=active)


def test_model_configuration_precedes_readiness_and_failed_configuration_keeps_host_fenced(host):
    root, home, scope, grant, opener = host

    def configure(mount):
        assert mount == home and (home / "memory.db").read_bytes() == b"verified Crew state"
        assert not (root / "crew-ready").exists()
        raise ValueError("model profile cannot start")

    with pytest.raises(ValueError, match="model profile"):
        RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(),
                             agent_uid=os.getuid(), agent_gid=os.getgid(), opener=opener, configure=configure)
    assert not (root / "crew-ready").exists()


@pytest.mark.parametrize("change", ["manifest", "generation", "client_cert", "existing_home", "symlink"])
def test_stale_or_untrusted_host_never_claims_readiness(host, change):
    root, home, scope, grant, _ = host
    if change == "manifest":
        grant["objects"]["manifest"]["sha256"] = "0" * 64
    elif change == "generation":
        scope["lease_generation"] = 3
    elif change == "client_cert":
        data = json.loads((root / "crew-host.json").read_text())
        data["gateway_fingerprint"] = "0" * 64
        (root / "crew-host.json").write_text(json.dumps(data))
    elif change == "existing_home":
        (home / "memory.db").write_bytes(b"old")
    else:
        (root / "crew-restore.json").symlink_to(root / "crew-host.json")
    with pytest.raises((ValueError, OSError)):
        invoke(host)
    assert not (root / "crew-ready").exists()


def test_failed_service_start_retracts_readiness(host):
    with pytest.raises(subprocess.CalledProcessError):
        invoke(host, fail_start=True)
    assert not (host[0] / "crew-ready").exists()


def _fresh(host):
    root, home, scope, _grant, opener = host
    fresh = {**scope, "checkpoint_id": None, "manifest_sha256": None, "restore_manifest_version": 0}
    for name in ("crew-host.json",):
        authority = json.loads((root / name).read_text())
        authority["restore_manifest_version"] = 0
        (root / name).write_text(json.dumps(authority))
    return root, home, fresh, opener


def test_fresh_first_host_starts_on_an_empty_owner_home_without_a_grant(host):
    """A first host (#8171) has nothing to restore: no grant, no fetch, receipts pin nothing."""
    root, home, scope, opener = _fresh(host)
    commands = []

    class NoFetch:
        def open(self, *_args, **_kwargs):
            raise AssertionError("a fresh host must not fetch checkpoint objects")

    configured = []
    RESTORE.restore_host(scope, None, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                         opener=NoFetch(), run=lambda command, *, check: commands.append(command), configure=configured.append)
    assert commands == [["systemctl", "start", "adp-crew.service"], ["systemctl", "start", "adp-crew-host.service"],
                        ["systemctl", "start", "adp-crew-checkpoint-final.service"], ["systemctl", "start", "adp-crew-checkpoint.timer"]]
    assert configured == [home] and [entry.name for entry in home.iterdir()] == [RESTORE.MARKER_DIRECTORY]
    assert json.loads((root / "crew-restore.json").read_text()) == {"checkpoint_id": None, "manifest_sha256": None}
    ready = json.loads((root / "crew-ready").read_text())
    assert ready["restore_manifest_version"] == 0 and ready["lease_generation"] == 2
    profile = root / "profile.json"
    profile.write_text(json.dumps({"revision": scope["profile_revision"]}))
    expected = {field: value for field, value in scope.items() if field != "run_id"}
    expected["run_id_sha256"] = hashlib.sha256(scope["run_id"].encode()).hexdigest()

    def active(*_args, **_kwargs):
        return SimpleNamespace(returncode=0)

    assert VERIFY.verify(expected, root=root, profile=profile, uid=os.getuid(), run=active)
    assert not VERIFY.verify({**expected, "restore_manifest_version": 7, "checkpoint_id": "checkpoint", "manifest_sha256": "a" * 64},
                             root=root, profile=profile, uid=os.getuid(), run=active)


def test_fresh_first_host_ignores_the_hosts_own_state_directory(host):
    """The readiness chain stores SSH host keys and the owner's home under .adp-host BEFORE the first restore
    (dev 2026-10-26); that root-only state is not Crew content and must not read as an occupied home."""
    root, home, scope, opener = _fresh(host)
    state = home / RESTORE.MARKER_DIRECTORY
    state.mkdir(mode=0o700)
    (state / "ssh-host-keys").mkdir(mode=0o700)
    (state / "ssh-host-keys" / "ssh_host_ed25519_key").write_bytes(b"key")
    (state / "home" / "ec2-user").mkdir(parents=True, mode=0o700)
    assert RESTORE.probe_home(scope, home=home, root=root, uid=os.getuid()) == "empty"
    RESTORE.restore_host(scope, None, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                         opener=opener, run=lambda *_args, **_kwargs: None, configure=lambda _home: None)
    assert (root / "crew-ready").exists()
    assert (state / "ssh-host-keys" / "ssh_host_ed25519_key").read_bytes() == b"key", "the host's state is untouched"
    assert (state / RESTORE.MARKER_FILE).exists()


@pytest.mark.parametrize("change", ["grant_with_fresh_scope", "fresh_scope_with_version", "pinned_scope_without_grant", "existing_home"])
def test_fresh_host_refuses_mixed_pins_and_occupied_homes(host, change):
    root, home, fresh, opener = _fresh(host)
    _root, _home, pinned, grant, _opener = host
    scope, given = fresh, None
    if change == "grant_with_fresh_scope":
        given = grant
    elif change == "fresh_scope_with_version":
        scope = {**fresh, "restore_manifest_version": 7}
    elif change == "pinned_scope_without_grant":
        scope = pinned
    else:
        (home / "memory.db").write_bytes(b"old")
    with pytest.raises(ValueError):
        RESTORE.restore_host(scope, given, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                             opener=opener, run=lambda *_args, **_kwargs: None)
    assert not (root / "crew-ready").exists() and not (root / "crew-restore.json").exists()


def _resume_receipts(host, new_run="resumed-run"):
    """A later boot: /run/adp is empty again, the bootstrap republished receipts for a NEW run."""
    root, home, scope, _grant, _opener = host
    for name in ("crew-ready", "crew-restore.json"):
        (root / name).unlink(missing_ok=True)
    scope = {**scope, "run_id": new_run}
    authority = json.loads((root / "crew-host.json").read_text())
    authority["run_id"] = new_run
    (root / "crew-host.json").write_text(json.dumps(authority))
    (root / "crew-model.json").write_text(json.dumps({"run_id": new_run, "lease_generation": scope["lease_generation"]}))
    return scope


def test_restore_writes_a_root_only_marker_before_configuring(host):
    root, home, scope, grant, opener = host
    seen = []

    def configure(mount):
        seen.append(RESTORE.read_marker(home, os.getuid()))

    RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                         opener=opener, run=lambda *_a, **_k: None, configure=configure, now=1_700_000_000)
    marker = seen[0]
    assert marker["tenant_id"] == "tenant" and marker["box_id"] == "box" and marker["resume_count"] == 0
    assert marker["provisioned_from"] == {"checkpoint_id": "checkpoint", "manifest_sha256": scope["manifest_sha256"]}
    assert marker["provisioned_at"] == 1_700_000_000 and marker["crew_version"] == "0.11.2"
    assert "example-run" not in json.dumps(marker)  # only a digest of the run id is kept
    directory = home / RESTORE.MARKER_DIRECTORY
    assert directory.stat().st_mode & 0o777 == 0o700 and (directory / "provision.json").stat().st_mode & 0o777 == 0o600
    assert RESTORE.probe_home(scope, root=root, home=home, uid=os.getuid()) == "resumable"


def test_same_box_restart_resumes_the_provisioned_home_for_the_new_run(host):
    root, home, scope, grant, opener = host
    RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                         opener=opener, run=lambda *_a, **_k: None, now=1_700_000_000)
    (home / "sessions").mkdir()
    (home / "sessions" / "turn.jsonl").write_text("the owner's work")
    (home / "crew.sock").touch()  # a live Crew home has entries a restore would refuse
    resumed = _resume_receipts(host)
    assert RESTORE.probe_home(resumed, root=root, home=home, uid=os.getuid()) == "resumable"
    commands, configured = [], []
    marker = RESTORE.resume_host(resumed, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                                 run=lambda command, *, check: commands.append(command), configure=configured.append,
                                 now=1_700_009_000)
    assert configured == [home]
    assert commands == [["systemctl", "start", "adp-crew.service"], ["systemctl", "start", "adp-crew-host.service"],
                        ["systemctl", "start", "adp-crew-checkpoint-final.service"], ["systemctl", "start", "adp-crew-checkpoint.timer"]]
    assert (home / "memory.db").read_bytes() == b"verified Crew state"
    assert (home / "sessions" / "turn.jsonl").read_text() == "the owner's work"
    assert marker["resume_count"] == 1 and marker["resumed_at"] == 1_700_009_000 and marker["provisioned_at"] == 1_700_000_000
    assert marker["run_id_sha256"] == hashlib.sha256(b"resumed-run").hexdigest()
    ready = json.loads((root / "crew-ready").read_text())
    assert ready["run_id"] == "resumed-run" and ready["lease_generation"] == 2
    profile = root / "profile.json"
    profile.write_text(json.dumps({"revision": scope["profile_revision"]}))
    expected = {field: value for field, value in resumed.items() if field != "run_id"}
    expected["run_id_sha256"] = hashlib.sha256(b"resumed-run").hexdigest()
    assert VERIFY.verify(expected, root=root, profile=profile, uid=os.getuid(), run=lambda *_a, **_k: SimpleNamespace(returncode=0))


def test_replace_onto_the_retained_disk_resumes_across_generations(host):
    root, home, scope, grant, opener = host
    RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                         opener=opener, run=lambda *_a, **_k: None)
    resumed = {**_resume_receipts(host), "lease_generation": 3, "instance_ref": "i-22222222"}
    authority = json.loads((root / "crew-host.json").read_text())
    authority.update(lease_generation=3, instance_ref="i-22222222")
    (root / "crew-host.json").write_text(json.dumps(authority))
    (root / "crew-model.json").write_text(json.dumps({"run_id": "resumed-run", "lease_generation": 3}))
    marker = RESTORE.resume_host(resumed, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                                 run=lambda *_a, **_k: None, configure=lambda _home: None)
    assert marker["lease_generation"] == 3 and marker["instance_ref"] == "i-22222222"
    assert (home / "memory.db").read_bytes() == b"verified Crew state"


def test_failed_configure_leaves_a_home_the_next_attempt_resumes(host):
    root, home, scope, grant, opener = host

    def failing(_home):
        raise ValueError("model profile cannot start")

    with pytest.raises(ValueError, match="model profile"):
        RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                             opener=opener, run=lambda *_a, **_k: None, configure=failing)
    assert not (root / "crew-ready").exists()
    assert RESTORE.probe_home(scope, root=root, home=home, uid=os.getuid()) == "resumable"
    RESTORE.resume_host(scope, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                        run=lambda *_a, **_k: None, configure=lambda _home: None)
    assert json.loads((root / "crew-ready").read_text())["run_id"] == scope["run_id"]


@pytest.mark.parametrize("change", ["no_marker", "agent_owned_marker", "foreign_owner", "other_crew_version", "symlinked_marker"])
def test_occupied_home_of_unknown_provenance_is_refused_by_name(host, change, tmp_path):
    root, home, scope, grant, opener = host
    if change == "no_marker":
        (home / "memory.db").write_bytes(b"someone's data")
    else:
        RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                             opener=opener, run=lambda *_a, **_k: None)
        marker_file = home / RESTORE.MARKER_DIRECTORY / "provision.json"
        if change == "agent_owned_marker":
            marker_file.chmod(0o666)  # writable by others: not the controller's root-only receipt
        elif change == "foreign_owner":
            marker = json.loads(marker_file.read_text())
            marker["user_id"] = "someone-else"
            marker_file.write_text(json.dumps(marker))
        elif change == "other_crew_version":
            marker = json.loads(marker_file.read_text())
            marker["crew_version"] = "0.7.0"
            marker_file.write_text(json.dumps(marker))
        else:
            marker_file.unlink()
            marker_file.symlink_to(root / "crew-host.json")
    resumed = _resume_receipts(host)
    assert RESTORE.probe_home(resumed, root=root, home=home, uid=os.getuid()) == "unknown"
    with pytest.raises(RESTORE.CrewHomeUnknownProvenance):
        RESTORE.resume_host(resumed, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                            run=lambda *_a, **_k: None, configure=lambda _home: None)
    with pytest.raises(ValueError):
        RESTORE.restore_host(resumed, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                             opener=opener, run=lambda *_a, **_k: None)
    assert not (root / "crew-ready").exists()
    assert (home / "memory.db").exists()  # nothing of the occupant was touched


def test_a_home_provisioned_by_a_supported_previous_release_resumes_and_is_remarked(host):
    """Upgrade path (dev 2026-11-04): the retained home of a 0.8.0 box must resume under 0.11.2 and
    carry the current version afterwards; an unsupported version stays unknown provenance."""
    root, home, scope, grant, opener = host
    RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                         opener=opener, run=lambda *_a, **_k: None, now=1_700_000_000)
    marker_file = home / RESTORE.MARKER_DIRECTORY / "provision.json"
    marker = json.loads(marker_file.read_text())
    marker["crew_version"] = RESTORE.SUPPORTED_PREVIOUS_CREW_VERSIONS[0]
    marker_file.write_text(json.dumps(marker))
    (home / "sessions").mkdir()
    (home / "sessions" / "turn.jsonl").write_text("the owner's work")
    resumed = _resume_receipts(host)
    assert RESTORE.probe_home(resumed, root=root, home=home, uid=os.getuid()) == "resumable"
    commands = []
    remarked = RESTORE.resume_host(resumed, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                                   run=lambda command, *, check: commands.append(command), configure=lambda _home: None,
                                   now=1_700_009_000)
    assert remarked["crew_version"] == RESTORE.CREW_VERSION and remarked["resume_count"] == 1
    assert json.loads(marker_file.read_text())["crew_version"] == RESTORE.CREW_VERSION
    assert (home / "sessions" / "turn.jsonl").read_text() == "the owner's work"
    assert commands[0] == ["systemctl", "start", "adp-crew.service"]


def test_resume_never_fetches_and_requires_fresh_receipts_for_its_run(host):
    root, home, scope, grant, opener = host
    RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                         opener=opener, run=lambda *_a, **_k: None)
    # Receipts from the previous run are still there: the box is not "unready".
    with pytest.raises(ValueError, match="unready"):
        RESTORE.resume_host(scope, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                            run=lambda *_a, **_k: None)
    resumed = _resume_receipts(host)
    stale = {**resumed, "run_id": "a-third-run"}  # authority names resumed-run, not this
    with pytest.raises(ValueError, match="authority"):
        RESTORE.resume_host(stale, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                            run=lambda *_a, **_k: None)
    assert not (root / "crew-ready").exists()


def test_probe_classifies_empty_homes_and_rejects_malformed_scopes(host):
    root, home, scope, _grant, _opener = host
    assert RESTORE.probe_home(scope, root=root, home=home, uid=os.getuid()) == "empty"
    with pytest.raises(ValueError):
        RESTORE.probe_home({**scope, "lease_generation": 0}, root=root, home=home, uid=os.getuid())


def test_ensure_is_one_idempotent_command_for_every_home_state(host):
    """``ensure`` (#8459): ready | resumed | needs_grant | restored, decided by the restorer alone."""
    root, home, scope, grant, opener = host
    common = dict(root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(), opener=opener,
                  run=lambda *_a, **_k: None, configure=lambda _home: None, now=1_700_000_000)
    # An empty home whose scope pins a checkpoint: the caller must bring the grant.
    assert RESTORE.ensure_host(scope, None, **common) == "needs_grant"
    assert not any(home.iterdir()) and not (root / "crew-ready").exists()
    assert RESTORE.ensure_host(scope, grant, **common) == "restored"
    assert (home / "memory.db").read_bytes() == b"verified Crew state"
    # Same run again within the boot: nothing to do.
    assert RESTORE.ensure_host(scope, None, **common) == "ready"
    # Receipts for another run while this run's receipt is published: refused by name.
    with pytest.raises(RESTORE.InvalidCheckpoint, match="another run"):
        RESTORE.ensure_host({**scope, "run_id": "other-run"}, None, **common)
    # After a reboot (/run/adp empty, home kept) the same command resumes the home for the new run.
    resumed = _resume_receipts(host)
    assert RESTORE.ensure_host(resumed, None, **common) == "resumed"
    assert json.loads((root / "crew-ready").read_text())["run_id"] == "resumed-run"
    assert RESTORE.ensure_host(resumed, None, **common) == "ready"


def test_ensure_initialises_a_fresh_first_host_without_a_grant(host):
    root, home, scope, _grant, opener = host
    fresh = {**scope, "restore_manifest_version": 0, "checkpoint_id": None, "manifest_sha256": None}
    authority = json.loads((root / "crew-host.json").read_text())
    authority["restore_manifest_version"] = 0  # the gateway publishes a fresh scope for a first host (#8171)
    (root / "crew-host.json").write_text(json.dumps(authority))
    verdict = RESTORE.ensure_host(fresh, None, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                                  opener=opener, run=lambda *_a, **_k: None, configure=lambda _home: None)
    assert verdict == "restored" and (root / "crew-ready").exists()


def test_ensure_refuses_an_occupied_home_of_unknown_provenance(host):
    root, home, scope, grant, opener = host
    (home / "somebody-elses.db").write_text("x")
    with pytest.raises(RESTORE.CrewHomeUnknownProvenance):
        RESTORE.ensure_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                            opener=opener, run=lambda *_a, **_k: None, configure=lambda _home: None)
    assert not (root / "crew-ready").exists()


def test_main_defaults_to_ensure_and_prints_the_verdict(host, monkeypatch, capsys):
    root, home, scope, grant, opener = host
    monkeypatch.setattr(RESTORE.os, "geteuid", lambda: 0)
    monkeypatch.setattr(RESTORE.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(json.dumps(
        {"scope": scope, "grant": None, "model_id": "model-x"}).encode())})())
    monkeypatch.setattr(RESTORE, "ensure_host", lambda scope_seen, grant_seen, *, configure: "needs_grant")
    RESTORE.main()
    assert capsys.readouterr().out.strip() == "needs_grant"


def test_configure_arguments_carry_only_an_https_edge_origin():
    assert RESTORE.configure_arguments("model-x", {}) == ["--configure", "model-x"]
    assert RESTORE.configure_arguments("model-x", {"ADP_CREW_DASHBOARD_ORIGIN": "https://d1example.cloudfront.net"}) == [
        "--configure", "model-x", "https://d1example.cloudfront.net"]
    for bad in ("http://edge.example", "https://edge.example/path", "https://EDGE.example", "javascript:alert(1)", ""):
        assert RESTORE.configure_arguments("model-x", {"ADP_CREW_DASHBOARD_ORIGIN": bad}) == ["--configure", "model-x"]


def _fake_owner(monkeypatch, path, uid):
    """Present ``path`` as owned by ``uid`` without root: the restorer stats through ``_lstat``."""
    real = RESTORE._lstat

    def lstat(target):
        entry = real(target)
        if Path(target) == Path(path):
            values = list(entry)
            values[4] = uid
            return os.stat_result(values)
        return entry

    monkeypatch.setattr(RESTORE, "_lstat", lstat)


def test_probe_and_resume_accept_the_agent_owned_home_a_restore_leaves_behind(host, monkeypatch):
    """dev 2026-10-20: restore chowns the home to agent, then every probe/resume demanded root."""
    root, home, scope, grant, opener = host
    RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                         opener=opener, run=lambda *_a, **_k: None, configure=lambda _home: None)
    agent_uid = os.getuid() + 1000
    _fake_owner(monkeypatch, home, agent_uid)
    resumed = _resume_receipts(host)
    assert RESTORE.probe_home(resumed, root=root, home=home, uid=os.getuid(), agent_uid=agent_uid) == "resumable"
    marker = RESTORE.resume_host(resumed, root=root, home=home, uid=os.getuid(), agent_uid=agent_uid, agent_gid=os.getgid(),
                                 run=lambda *_a, **_k: None, configure=lambda _home: None)
    assert marker["resume_count"] == 1
    # Any other owner, or a group/world-writable home, is still refused.
    _fake_owner(monkeypatch, home, agent_uid + 1)
    with pytest.raises(RESTORE.InvalidCheckpoint, match="untrusted Crew host directory"):
        RESTORE.probe_home(resumed, root=root, home=home, uid=os.getuid(), agent_uid=agent_uid)


def test_run_directory_and_marker_still_require_root_when_the_home_is_agent_owned(host, monkeypatch):
    root, home, scope, grant, opener = host
    RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                         opener=opener, run=lambda *_a, **_k: None, configure=lambda _home: None)
    agent_uid = os.getuid() + 1000
    resumed = _resume_receipts(host)
    _fake_owner(monkeypatch, root, agent_uid)
    with pytest.raises(RESTORE.InvalidCheckpoint, match="untrusted Crew host directory"):
        RESTORE.probe_home(resumed, root=root, home=home, uid=os.getuid(), agent_uid=agent_uid)
    monkeypatch.undo()
    _fake_owner(monkeypatch, home / RESTORE.MARKER_DIRECTORY, agent_uid)
    assert RESTORE.probe_home(resumed, root=root, home=home, uid=os.getuid(), agent_uid=agent_uid) == "unknown"


def test_main_resume_passes_the_edge_origin_to_configure_and_names_refusals(host, monkeypatch, capsys):
    root, home, scope, grant, opener = host
    RESTORE.restore_host(scope, grant, root=root, home=home, uid=os.getuid(), agent_uid=os.getuid(), agent_gid=os.getgid(),
                         opener=opener, run=lambda *_a, **_k: None, configure=lambda _home: None)
    resumed = _resume_receipts(host)
    seen = []
    monkeypatch.setattr(RESTORE.os, "geteuid", lambda: 0)
    monkeypatch.setenv(RESTORE.DASHBOARD_ORIGIN_ENV, "https://d1example.cloudfront.net")
    monkeypatch.setattr(RESTORE.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(json.dumps(
        {"mode": "resume", "scope": resumed, "grant": None, "model_id": "model-x"}).encode())})())

    def resume_host(scope_seen, *, configure):
        configure(home)
        seen.append(scope_seen["run_id"])

    monkeypatch.setattr(RESTORE, "resume_host", resume_host)
    monkeypatch.setattr(RESTORE.subprocess, "run", lambda command, **_k: seen.append(command))
    RESTORE.main()
    assert seen[0][-3:] == ["--configure", "model-x", "https://d1example.cloudfront.net"]
    assert seen[1] == resumed["run_id"]
    # A refusal prints the class and its fixed message, not the class alone (dev 2026-10-20).
    RESTORE.report_refusal(RESTORE.InvalidCheckpoint("untrusted Crew host directory"))
    assert capsys.readouterr().err.strip() == "Crew host restore refused: InvalidCheckpoint: untrusted Crew host directory"
