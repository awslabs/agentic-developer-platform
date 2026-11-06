"""A running service alone cannot declare an unverified host ready."""

import json
import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = Path(__file__).resolve().parents[1] / "image" / "verify_crew_ready.py"
SPEC = importlib.util.spec_from_file_location("verify_crew_ready", MODULE)
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)
verify = VERIFIER.verify


@pytest.fixture
def ready_host(tmp_path):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    profile = tmp_path / "profile" / "profile.json"
    profile.parent.mkdir(mode=0o700)
    scope = {
        "run_id": "example-run", "tenant_id": "one", "user_id": "alice", "box_id": "box",
        "instance_ref": "i-example", "private_ip": "10.4.0.3", "profile_revision": "r1",
        "lease_generation": 2, "restore_manifest_version": 7,
        "checkpoint_id": "checkpoint-one", "manifest_sha256": "a" * 64,
    }
    bindings = {key: value for key, value in scope.items() if key not in ("checkpoint_id", "manifest_sha256")}
    for name, value in (("crew-host.json", bindings), ("crew-ready", bindings),
                        ("crew-restore.json", {"checkpoint_id": "checkpoint-one", "manifest_sha256": "a" * 64})):
        path = root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    profile.write_text(json.dumps({"revision": "r1"}))
    profile.chmod(0o600)
    expected = {key: value for key, value in scope.items() if key != "run_id"}
    expected["run_id_sha256"] = hashlib.sha256(scope["run_id"].encode()).hexdigest()
    return root, profile, expected


def test_current_scoped_receipt_requires_both_services(ready_host):
    root, profile, scope = ready_host
    calls = []

    def active(command, **_):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    assert verify(scope, root=root, profile=profile, uid=os.getuid(), run=active)
    assert calls == [
        ["systemctl", "is-active", "--quiet", "adp-crew.service"],
        ["systemctl", "is-active", "--quiet", "adp-crew-host.service"],
    ]
    assert not verify(scope, root=root, profile=profile, uid=os.getuid(),
                      run=lambda *_args, **_kwargs: SimpleNamespace(returncode=1))


@pytest.mark.parametrize("field,replacement", [
    ("run_id_sha256", hashlib.sha256(b"stale-run").hexdigest()), ("lease_generation", 1), ("restore_manifest_version", 6),
    ("checkpoint_id", "other-checkpoint"), ("manifest_sha256", "b" * 64),
])
def test_changed_scope_never_claims_readiness(ready_host, field, replacement):
    root, profile, scope = ready_host
    scope[field] = replacement
    assert not verify(scope, root=root, profile=profile, uid=os.getuid(),
                      run=lambda *_args, **_kwargs: SimpleNamespace(returncode=0))


def test_missing_or_untrusted_receipt_never_claims_readiness(ready_host):
    root, profile, scope = ready_host
    receipt = root / "crew-restore.json"
    receipt.unlink()
    assert not verify(scope, root=root, profile=profile, uid=os.getuid())
    receipt.symlink_to(root / "crew-ready")
    assert not verify(scope, root=root, profile=profile, uid=os.getuid())
    receipt.unlink()
    receipt.write_text(json.dumps({"checkpoint_id": scope["checkpoint_id"], "manifest_sha256": scope["manifest_sha256"]}))
    receipt.chmod(0o666)
    assert not verify(scope, root=root, profile=profile, uid=os.getuid())


@pytest.fixture
def fresh_host(tmp_path):
    """A first host (#8171): scoped receipts with manifest version 0 and a restore receipt that pins nothing."""
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    profile = tmp_path / "profile" / "profile.json"
    profile.parent.mkdir(mode=0o700)
    bindings = {
        "run_id": "fresh-run", "tenant_id": "one", "user_id": "alice", "box_id": "box",
        "instance_ref": "i-example", "private_ip": "10.4.0.3", "profile_revision": "r1",
        "lease_generation": 1, "restore_manifest_version": 0,
    }
    for name, value in (("crew-host.json", bindings), ("crew-ready", bindings),
                        ("crew-restore.json", {"checkpoint_id": None, "manifest_sha256": None})):
        path = root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    profile.write_text(json.dumps({"revision": "r1"}))
    profile.chmod(0o600)
    scope = {key: value for key, value in bindings.items() if key != "run_id"}
    scope.update(run_id_sha256=hashlib.sha256(b"fresh-run").hexdigest(), checkpoint_id=None, manifest_sha256=None)
    return root, profile, scope


def test_fresh_first_host_is_verified_only_as_fresh(fresh_host):
    root, profile, scope = fresh_host

    def active(*_args, **_kwargs):
        return SimpleNamespace(returncode=0)

    assert verify(scope, root=root, profile=profile, uid=os.getuid(), run=active)
    assert not verify(scope, root=root, profile=profile, uid=os.getuid(),
                      run=lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    for field, replacement in (("restore_manifest_version", 1), ("checkpoint_id", "checkpoint-one"),
                               ("manifest_sha256", "a" * 64), ("run_id_sha256", hashlib.sha256(b"other").hexdigest())):
        assert not verify({**scope, field: replacement}, root=root, profile=profile, uid=os.getuid(), run=active), field
    # A host that actually restored a checkpoint never passes as fresh, and vice versa.
    (root / "crew-restore.json").write_text(json.dumps({"checkpoint_id": "checkpoint-one", "manifest_sha256": "a" * 64}))
    assert not verify(scope, root=root, profile=profile, uid=os.getuid(), run=active)
    (root / "crew-restore.json").write_text(json.dumps({}))
    assert not verify(scope, root=root, profile=profile, uid=os.getuid(), run=active)


def test_pinned_scope_never_accepts_a_fresh_restore_receipt(ready_host):
    root, profile, scope = ready_host
    (root / "crew-restore.json").write_text(json.dumps({"checkpoint_id": None, "manifest_sha256": None}))
    assert not verify(scope, root=root, profile=profile, uid=os.getuid(),
                      run=lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
