"""The host publishes its own owner's Crew home as a verified checkpoint, and only that (#8379).

Proven offline with a fake gateway, IMDS and S3: a periodic capture signs ``begin``/``complete``
with the instance role (SigV4 identical to botocore's), uploads exactly the captured bytes to the
granted presigned URLs, records the verified state root-only; an unchanged home uploads nothing;
the final capture stops the Crew units first and says so in the manifest; stale receipts, an
unprovisioned home, an unversioned bucket and a gateway refusal are named failures that leave the
recorded state untouched; the CLI prints one JSON verdict line the controller parses.
"""

import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

IMAGE = Path(__file__).resolve().parents[1] / "image"
sys.path.insert(0, str(IMAGE.parents[2] / "agent-factory" / "agent" / "scripts"))
SPEC = importlib.util.spec_from_file_location("checkpoint_crew_host", IMAGE / "checkpoint_crew_host.py")
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)

GATEWAY_ARN = "arn:aws:execute-api:us-east-1:000000000000:abc123/dev"
GATEWAY = "https://abc123.execute-api.us-east-1.amazonaws.com/dev"
CREDENTIALS = {"access_key": "AKIAEXAMPLE", "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "token": "session-token"}


class Response:
    def __init__(self, body=b"", headers=None, status=200):
        self.body, self.headers, self.status = body, headers or {}, status

    def read(self, limit=None):
        return self.body if limit is None else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class World:
    """Fake IMDS, gateway and S3 behind one opener; records every call."""

    def __init__(self):
        self.calls = []
        self.uploads = {}
        self.begin_status = "uploading"
        self.refuse_begin = None
        self.versioned = True
        self.checkpoint_id = "0f3b8e6a-1111-4222-8333-444455556666"

    def open(self, request, timeout):
        url = request.full_url
        self.calls.append((request.get_method(), url, dict(request.header_items()), request.data))
        if url.startswith("http://169.254.169.254/latest/api/token"):
            return Response(b"imds-token")
        if url == "http://169.254.169.254/latest/meta-data/iam/security-credentials/":
            assert request.headers["X-aws-ec2-metadata-token"] == "imds-token"
            return Response(b"adp-dev-box-guest-role\n")
        if url.startswith("http://169.254.169.254/latest/meta-data/iam/security-credentials/adp-dev-box-guest-role"):
            return Response(json.dumps({"Code": "Success", "AccessKeyId": CREDENTIALS["access_key"],
                                        "SecretAccessKey": CREDENTIALS["secret_key"], "Token": CREDENTIALS["token"]}).encode())
        if url == f"{GATEWAY}/agent/crew/host-checkpoint/begin":
            assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
            assert request.headers["X-agent-runid"] == "11111111-2222-4333-8444-555555555555"
            if self.refuse_begin:
                raise HTTPError(url, self.refuse_begin, "refused", {}, io.BytesIO(b'{"error":"host_checkpoint_fenced"}'))
            body = json.loads(request.data)
            self.begin_body = body
            if self.begin_status == "verified":
                return Response(json.dumps({"id": self.checkpoint_id, "status": "verified", "sequence": 4}).encode())
            grants = {label: {"bucket": "ckpt-bucket", "key": f"crew-checkpoints/{'k' * 52}/{self.checkpoint_id}/{label}",
                              "url": f"https://ckpt-bucket.s3.us-east-1.amazonaws.com/crew-checkpoints/k/{self.checkpoint_id}/{label}?X-Amz-Signature=sig",
                              "headers": {"x-amz-checksum-sha256": "c2hh"}} for label in ("archive", "manifest")}
            return Response(json.dumps({"id": self.checkpoint_id, "status": "uploading", "sequence": 4, **grants}).encode())
        if url.startswith("https://ckpt-bucket.s3.us-east-1.amazonaws.com/"):
            assert request.get_method() == "PUT" and request.headers["X-amz-checksum-sha256"] == "c2hh"
            label = url.split("?", 1)[0].rsplit("/", 1)[-1]
            self.uploads[label] = request.data
            return Response(b"", {"x-amz-version-id": f"{label}-v7"} if self.versioned else {})
        if url == f"{GATEWAY}/agent/crew/host-checkpoint/complete":
            body = json.loads(request.data)
            self.complete_body = body
            assert body["checkpoint_id"] == self.checkpoint_id
            for label, data in self.uploads.items():
                receipt = body["objects"][label]
                assert receipt == {"bucket": "ckpt-bucket", "key": f"crew-checkpoints/{'k' * 52}/{self.checkpoint_id}/{label}",
                                   "version_id": f"{label}-v7", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            return Response(json.dumps({"id": self.checkpoint_id, "status": "verified"}).encode())
        raise AssertionError(f"unexpected request {request.get_method()} {url}")


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    marker = home / ".adp-host"
    marker.mkdir(mode=0o700)
    (marker / "provision.json").write_text("{}")
    (home / "lessons.jsonl").write_bytes(b'{"lesson":"keep answers short"}\n')
    (home / "workspace" / "memory").mkdir(parents=True)
    (home / "workspace" / "memory" / "preferences.md").write_text("preferred: concise\n")
    with sqlite3.connect(home / "memory.db") as db:
        db.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, text TEXT)")
        db.execute("INSERT INTO memories (text) VALUES ('owner likes tests')")
    (home / "config.json").write_text('{"dashboard": {"url": "https://edge"}}')   # excluded root
    (home / ".kiro").mkdir()
    (home / ".kiro" / "agents").mkdir()                                        # excluded name
    (home / ".kiro" / "agents" / "kirocrew.json").write_text("{}")
    authority = {"run_id": "11111111-2222-4333-8444-555555555555", "tenant_id": "tenant", "user_id": "owner", "box_id": "box",
                 "instance_ref": "i-0123456789abcdef0", "private_ip": "10.0.0.9", "profile_revision": "crew-r1",
                 "lease_generation": 3, "lease_expires_at": 4_000_000_000, "gateway_fingerprint": "f" * 64, "verification_keys": {}}
    (root / "crew-host.json").write_text(json.dumps(authority))
    (root / "crew-model.json").write_text(json.dumps({"run_id": authority["run_id"], "lease_generation": 3,
                                                       "gateway_execute_arn": GATEWAY_ARN, "model_id": "example-model"}))
    (root / "crew-ready").write_text("{}")
    for path in (root / "crew-host.json", root / "crew-model.json", root / "crew-ready"):
        path.chmod(0o600)
    return root, home


def run(host, mode, world=None, **options):
    root, home = host
    world = world or World()
    units = []
    result = HELPER.run_checkpoint(mode, home=home, root=root, uid=os.getuid(), opener=world,
                                   run=lambda command, **kwargs: units.append(command), **options)
    return result, world, units


def test_periodic_capture_uploads_the_allowlisted_home_and_records_the_verified_state(host):
    root, home = host
    result, world, units = run(host, "periodic")
    assert result["status"] == "verified" and result["checkpoint_id"] == world.checkpoint_id and result["sequence"] == 4
    assert result["quiesced"] is False and units == []
    manifest = json.loads(world.uploads["manifest"])
    assert set(manifest["files"]) == {"lessons.jsonl", "memory.db", "workspace/memory/preferences.md"}
    assert manifest["quiesced"] is False and manifest["crew_version"] == "0.11.2"
    assert "config.json" in manifest["excluded"] and ".adp-host" in manifest["excluded"] and ".kiro/agents" in manifest["excluded"]
    assert hashlib.sha256(world.uploads["archive"]).hexdigest() == manifest["archive_sha256"] == world.begin_body["archive_sha256"]
    assert world.begin_body["manifest_sha256"] == hashlib.sha256(world.uploads["manifest"]).hexdigest()
    assert world.begin_body["idempotency_key"].startswith("host:3:") and world.begin_body["crew_version"] == "0.11.2"
    methods = [(method, url.split("?")[0].rsplit("/", 2)[-2:]) for method, url, _h, _d in world.calls]
    assert [m for m, _ in methods] == ["PUT", "GET", "GET", "POST", "PUT", "PUT", "POST"]
    state = json.loads((home / ".adp-host" / "checkpoint.json").read_text())
    assert state["checkpoint_id"] == world.checkpoint_id and state["sequence"] == 4 and state["lease_generation"] == 3
    assert state["files_sha256"] == HELPER.files_digest(manifest)
    assert (home / ".adp-host" / "checkpoint.json").stat().st_mode & 0o077 == 0
    # Nothing but the archive and manifest left the host, and no secret-shaped header went to S3.
    for _method, url, headers, _data in world.calls:
        if "s3." in url:
            assert "Authorization" not in headers and "X-amz-security-token" not in headers



def test_capture_from_owner_home_includes_codex_metadata_but_not_rollout(host, tmp_path):
    owner = tmp_path / "ec2-user"
    codex = owner / ".codex"
    codex.mkdir(parents=True, mode=0o700)
    codex.chmod(0o700)
    (codex / "history.jsonl").write_text('{"session_id":"example","text":"hello"}\n')
    sessions = codex / "sessions" / "2026"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-10-23T00-00-00-11111111-2222-3333-4444-555555555555.jsonl"
    with rollout.open("wb") as writer:
        writer.write(b'{"type":"session_meta","payload":{"id":"11111111-2222-3333-4444-555555555555"}}\n')
        writer.truncate(101 * 1024 * 1024)
    result, world, _ = run(host, "periodic", owner_home=owner)
    assert result["status"] == "verified"
    manifest = json.loads(world.uploads["manifest"])
    prefix = ".codex/owner-shell"
    assert f"{prefix}/history.jsonl" in manifest["files"]
    assert f"{prefix}/sessions/2026/metadata-2026-10-23T00-00-00-11111111-2222-3333-4444-555555555555.json" in manifest["files"]
    assert f"{prefix}/sessions/2026/{rollout.name}" not in manifest["files"]
    assert len(world.uploads["archive"]) < 512 * 1024 * 1024


def test_unchanged_home_uploads_nothing_and_a_change_uploads_again(host):
    root, home = host
    first, world, _ = run(host, "periodic")
    again, world_again, _ = run(host, "periodic")
    assert again == {"status": "unchanged", "mode": "periodic", "checkpoint_id": world.checkpoint_id, "sequence": 4, "quiesced": False}
    assert world_again.calls == []   # not even IMDS
    (home / "lessons.jsonl").write_bytes(b'{"lesson":"and cite sources"}\n')
    changed, world_changed, _ = run(host, "periodic")
    assert changed["status"] == "verified" and world_changed.begin_body["idempotency_key"] != world.begin_body["idempotency_key"]


def test_final_capture_quiesces_crew_first_and_a_replayed_key_is_accepted_as_done(host):
    root, home = host
    result, world, units = run(host, "final")
    assert result["status"] == "verified" and result["quiesced"] is True
    assert units == [["systemctl", "stop", "adp-crew-host.service"], ["systemctl", "stop", "adp-crew.service"]]
    assert json.loads(world.uploads["manifest"])["quiesced"] is True
    # A lost `complete` reply: the next run replays the stable key, the gateway says verified, no upload.
    (home / ".adp-host" / "checkpoint.json").unlink()
    replay = World()
    replay.begin_status = "verified"
    result, replay, _ = run(host, "final", world=replay)
    assert result["status"] == "verified" and replay.uploads == {} and len([c for c in replay.calls if c[0] == "POST"]) == 1
    assert json.loads((home / ".adp-host" / "checkpoint.json").read_text())["checkpoint_id"] == replay.checkpoint_id


@pytest.mark.parametrize("fault", ["not_ready", "stale_model", "unprovisioned", "unversioned", "refused", "bad_arn"])
def test_faults_are_named_and_never_record_a_verified_state(host, fault):
    root, home = host
    world = World()
    if fault == "not_ready":
        (root / "crew-ready").unlink()
    elif fault == "stale_model":
        model = json.loads((root / "crew-model.json").read_text())
        model["lease_generation"] = 2
        (root / "crew-model.json").write_text(json.dumps(model))
    elif fault == "bad_arn":
        model = json.loads((root / "crew-model.json").read_text())
        model["gateway_execute_arn"] = "arn:aws:execute-api:us-east-1:000000000000:abc123"
        (root / "crew-model.json").write_text(json.dumps(model))
    elif fault == "unprovisioned":
        (home / ".adp-host" / "provision.json").unlink()
        (home / ".adp-host").rmdir()
    elif fault == "unversioned":
        world.versioned = False
    elif fault == "refused":
        world.refuse_begin = 409
    with pytest.raises(HELPER.InvalidCheckpoint) as refused:
        run(host, "periodic", world=world)
    assert not (home / ".adp-host" / "checkpoint.json").exists()
    if fault == "refused":
        assert "409 host_checkpoint_fenced" in str(refused.value)
        assert not [c for c in world.calls if c[0] == "PUT" and "s3." in c[1]]
    if fault in ("not_ready", "stale_model", "unprovisioned", "bad_arn"):
        assert world.calls == []


def test_sigv4_matches_botocore(monkeypatch):
    botocore_auth = pytest.importorskip("botocore.auth")
    import datetime

    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    url = f"{GATEWAY}/agent/crew/host-checkpoint/begin"
    body = b'{"idempotency_key":"host:3:abc"}'
    now = 1_760_900_000
    ours = HELPER.sigv4_headers("POST", url, body, CREDENTIALS, "us-east-1", now=now,
                                headers={"content-type": "application/json", "x-agent-runid": "run-1"})

    class FixedDateTime(datetime.datetime):   # botocore stamps the request with its own utcnow()
        @classmethod
        def utcnow(cls):
            return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).replace(tzinfo=None)

        @classmethod
        def now(cls, tz=None):
            moment = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
            return moment if tz is not None else moment.replace(tzinfo=None)

    monkeypatch.setattr(botocore_auth.datetime, "datetime", FixedDateTime)
    request = AWSRequest(method="POST", url=url, data=body, headers={
        "content-type": "application/json", "x-agent-runid": "run-1", "x-amz-content-sha256": ours["x-amz-content-sha256"]})
    signer = botocore_auth.SigV4Auth(Credentials(CREDENTIALS["access_key"], CREDENTIALS["secret_key"], CREDENTIALS["token"]), "execute-api", "us-east-1")
    signer.add_auth(request)
    assert request.headers["Authorization"] == ours["Authorization"]
    assert request.headers["X-Amz-Security-Token"] == ours["x-amz-security-token"]


def test_cli_prints_one_verdict_line_and_exit_codes(host, monkeypatch, capsys):
    root, home = host
    monkeypatch.setattr(HELPER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(HELPER, "run_checkpoint", lambda mode: {"status": "verified", "mode": mode, "checkpoint_id": "c", "sequence": 1})
    assert HELPER.main(["--mode", "periodic"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"status": "verified", "mode": "periodic", "checkpoint_id": "c", "sequence": 1}

    def refuse(mode):
        raise HELPER.CheckpointRefused("Crew host is not ready")

    monkeypatch.setattr(HELPER, "run_checkpoint", refuse)
    assert HELPER.main(["--mode", "final"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out.strip()) == {"status": "failed", "mode": "final", "error": "CheckpointRefused"}
    assert "Crew host checkpoint failed: CheckpointRefused" in captured.err
    monkeypatch.setattr(HELPER.os, "geteuid", lambda: 1000)
    assert HELPER.main(["--mode", "periodic"]) == 1
    assert json.loads(capsys.readouterr().out.strip())["error"] == "NotRoot"


def test_helper_imports_its_archive_module_under_isolated_python(tmp_path):
    """systemd runs the helper with ``python3 -I``; the archive module sits beside it (#8379 fix)."""
    import shutil
    import subprocess

    deployed = tmp_path / "crew"
    deployed.mkdir()
    shutil.copy(IMAGE / "checkpoint_crew_host.py", deployed / "checkpoint_crew_host.py")
    shutil.copy(IMAGE.parents[2] / "agent-factory" / "agent" / "scripts" / "crew_checkpoint.py", deployed / "crew_checkpoint.py")
    result = subprocess.run([sys.executable, "-I", str(deployed / "checkpoint_crew_host.py"), "--help"],
                            capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def _fake_owner(monkeypatch, path, uid):
    """Present ``path`` as owned by ``uid`` without root: the helper stats the home through ``_lstat``."""
    real = HELPER._lstat

    def lstat(target):
        entry = real(target)
        if Path(target) == Path(path):
            values = list(entry)
            values[4] = uid
            return os.stat_result(values)
        return entry

    monkeypatch.setattr(HELPER, "_lstat", lstat)


@pytest.mark.parametrize("mode", ["periodic", "final"])
def test_capture_accepts_the_agent_owned_home_a_provisioned_box_has(host, monkeypatch, mode):
    """dev 2026-10-20/21 (#8223 cause 35): the restorer chowns the home to agent, Kiro Crew runs as
    agent, and every periodic and final capture was refused as an untrusted directory — the bucket
    never received a single object. The home owner is agent (or root); the receipts stay root-only."""
    root, home = host
    agent_uid = os.getuid() + 5000
    _fake_owner(monkeypatch, home, agent_uid)
    with pytest.raises(HELPER.CheckpointRefused, match="untrusted Crew host directory"):
        HELPER.run_checkpoint(mode, home=home, root=root, uid=os.getuid(), opener=World(),
                              run=lambda command, **kwargs: None, agent_uid=os.getuid() + 1)
    result, world, units = run(host, mode, agent_uid=agent_uid)
    assert result["status"] == "verified" and result["mode"] == mode
    assert world.uploads and HELPER.read_state(home, os.getuid())["checkpoint_id"] == result["checkpoint_id"]
    assert (mode == "final") == any("stop" in command for command in units)


def test_home_refused_by_name_when_world_writable_or_a_symlink(host, monkeypatch, tmp_path):
    root, home = host
    home.chmod(0o777)
    with pytest.raises(HELPER.CheckpointRefused, match=r"untrusted Crew host directory: .*found uid \d+, mode 0777"):
        run(host, "periodic")
    home.chmod(0o700)
    link = tmp_path / "home-link"
    link.symlink_to(home)
    with pytest.raises(HELPER.CheckpointRefused, match="untrusted Crew host directory"):
        HELPER.run_checkpoint("periodic", home=link, root=root, uid=os.getuid(), opener=World(),
                              run=lambda command, **kwargs: None)
    assert not World().uploads


def test_receipts_directory_still_requires_root_even_when_the_home_is_agent_owned(host, monkeypatch):
    root, home = host
    _fake_owner(monkeypatch, home, os.getuid() + 5000)
    with pytest.raises(HELPER.CheckpointRefused, match="untrusted Crew host"):
        HELPER.run_checkpoint("periodic", home=home, root=root, uid=os.getuid() + 7, opener=World(),
                              run=lambda command, **kwargs: None, agent_uid=os.getuid() + 5000)

