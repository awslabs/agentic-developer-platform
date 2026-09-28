"""Commit-then-error tests through the maintained S3/Kubernetes tool boundary."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from installation.config import Refusal
from installation.runner import Installer
from .test_complete_command import setup


def restart(installer, tools):
    receipt = json.loads(installer.receipt_path.read_text())
    resumed = Installer(installer.env, installer.lock, installer.directory, tools)
    resumed.resume(receipt)
    tools.installer = resumed
    return resumed


def key_is(args, key):
    return "--key" in args and args[args.index("--key") + 1] == key


def lost_response(
    tools, key, verb, *, read_failure=False, interrupt=False, malformed=False
):
    original = tools.call
    committed = False

    def call(args, **kwargs):
        nonlocal committed
        if committed and read_failure and "get-object" in args and key_is(args, key):
            raise Refusal("readback unavailable")
        result = original(args, **kwargs)
        if not committed and verb in args and key_is(args, key):
            committed = True
            if interrupt:
                raise KeyboardInterrupt
            if malformed:
                return SimpleNamespace(returncode=0, stdout="{}", stderr="")
            raise Refusal("response lost after commit")
        return result

    tools.call = call
    return original


@pytest.mark.parametrize("malformed", [False, True])
def test_lock_acquisition_adopts_only_its_committed_attempt(
    tmp_path, environment, release, monkeypatch, malformed
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    lost_response(tools, installer.lock_key, "put-object", malformed=malformed)
    with installer.exclusive():
        persisted = json.loads(installer.receipt_path.read_text())
        assert persisted["lock_attempt"]["value"] == tools.lock_object
        assert persisted["remote_lock"]["etag"] == tools.lock_etag
    assert tools.lock_object is None
    assert "lock_attempt" not in installer.receipt


@pytest.mark.parametrize("verb", ["put-object", "delete-object"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_lock_restart_recovers_commit_when_initial_readback_is_unavailable(
    tmp_path, environment, release, monkeypatch, verb, interrupt
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = lost_response(
        tools, installer.lock_key, verb, read_failure=True, interrupt=interrupt
    )
    with pytest.raises(KeyboardInterrupt if interrupt else Refusal):
        with installer.exclusive():
            pass
    persisted = json.loads(installer.receipt_path.read_text())
    assert persisted["lock_attempt"]["value"]["nonce"]
    assert persisted["status"] == "recovery-required"
    tools.call = original
    resumed = restart(installer, tools)
    resumed.recover_lock(resumed.run_id)
    assert tools.lock_object is None
    assert (
        "remote_lock" not in resumed.receipt and "lock_attempt" not in resumed.receipt
    )
    with resumed.exclusive():
        pass


def test_lock_release_recognizes_committed_delete(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    lost_response(tools, installer.lock_key, "delete-object")
    with installer.exclusive():
        pass
    assert tools.lock_object is None and "remote_lock" not in installer.receipt


@pytest.mark.parametrize("field", ["installation_id", "run_id", "nonce", "extra"])
def test_lock_never_adopts_a_different_full_attempt(
    tmp_path, environment, release, monkeypatch, field
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = tools.call

    def foreign(args, **kwargs):
        result = original(args, **kwargs)
        if "put-object" in args:
            tools.lock_object[field] = "foreign"
            raise Refusal("lost response")
        return result

    tools.call = foreign
    with pytest.raises(Refusal, match="another attempt"):
        with installer.exclusive():
            pytest.fail("foreign lock acquired")
    assert "remote_lock" not in installer.receipt
    tools.call = original
    resumed = restart(installer, tools)
    with pytest.raises(Refusal, match="another attempt"):
        resumed.recover_lock(resumed.run_id)
    assert not any("delete-object" in args for args, _ in tools.calls)


def test_lock_release_preserves_concurrent_replacement(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = tools.call

    def replace(args, **kwargs):
        if "delete-object" in args:
            tools.lock_object = {"installation_id": "foreign"}
            tools.lock_etag = '"foreign"'
        return original(args, **kwargs)

    tools.call = replace
    with pytest.raises(Refusal):
        with installer.exclusive():
            pass
    assert tools.lock_object == {"installation_id": "foreign"}
    assert installer.receipt["remote_lock"]["etag"] != tools.lock_etag


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("malformed", [False, True])
def test_route_adopts_committed_put_with_lost_or_incomplete_ack(
    tmp_path, environment, release, monkeypatch, enabled, malformed
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.route(not enabled)
    lost_response(tools, installer.route_key, "put-object", malformed=malformed)
    installer.route(enabled)
    assert tools.route["enabled"] is enabled
    assert installer.receipt["public_route"]["etag"] == tools.route_etag
    assert installer.receipt["public_route_enabled"] is enabled
    assert "pending_route" not in json.loads(installer.receipt_path.read_text())


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("interrupt", [False, True])
def test_route_restart_adopts_exact_pending_publication_read_only(
    tmp_path, environment, release, monkeypatch, enabled, interrupt
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.route(not enabled)
    original = lost_response(
        tools, installer.route_key, "put-object", read_failure=True, interrupt=interrupt
    )
    with pytest.raises(KeyboardInterrupt if interrupt else Refusal):
        installer.route(enabled)
    pending = json.loads(installer.receipt_path.read_text())["pending_route"]
    assert pending["value"] == tools.route
    assert installer.receipt["public_route_enabled"] is None
    tools.call = original
    resumed = restart(installer, tools)
    before = len(tools.calls)
    resumed.gateway()
    assert not any("put-object" in args for args, _ in tools.calls[before:])
    assert resumed.receipt["public_route"]["etag"] == tools.route_etag
    assert resumed.receipt["public_route_enabled"] is enabled
    assert "pending_route" not in resumed.receipt


@pytest.mark.parametrize(
    "field,value",
    [
        ("installation_id", "foreign"),
        ("namespace", "foreign"),
        ("revision", "e" * 32),
        ("release_id", "e" * 64),
        ("enabled", False),
        ("version", 1),
        ("extra", True),
    ],
)
def test_pending_route_rejects_any_different_committed_payload(
    tmp_path, environment, release, monkeypatch, field, value
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = lost_response(
        tools, installer.route_key, "put-object", read_failure=True
    )
    with pytest.raises(Refusal):
        installer.route(True)
    tools.call = original
    tools.route[field] = value
    tools.route_etag = '"replacement"'
    foreign = copy.deepcopy(tools.route)
    resumed = restart(installer, tools)
    before = len(tools.calls)
    with pytest.raises(Refusal):
        resumed.route(False)
    assert tools.route == foreign and resumed.receipt["pending_route"]
    assert not any("put-object" in args for args, _ in tools.calls[before:])


def test_pending_route_retries_same_identity_when_prior_remains(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.route(False)
    original = tools.call
    attempts = []

    def before_commit(args, **kwargs):
        if "put-object" in args and key_is(args, installer.route_key):
            attempts.append(
                json.loads(Path(args[args.index("--body") + 1]).read_text())
            )
            raise Refusal("unavailable before commit")
        return original(args, **kwargs)

    tools.call = before_commit
    with pytest.raises(Refusal):
        installer.route(True)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    tools.call = original
    resumed = restart(installer, tools)
    resumed.finish_route()
    assert tools.route == attempts[0]
    assert resumed.receipt["public_route_enabled"] is True


@pytest.mark.parametrize("broken", ["etag", "body", "json", "denied"])
def test_incomplete_readback_retains_pending_identity(
    tmp_path, environment, release, monkeypatch, broken
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = lost_response(
        tools, installer.route_key, "put-object", read_failure=True
    )
    with pytest.raises(Refusal):
        installer.route(True)
    expected = copy.deepcopy(installer.receipt["pending_route"])

    def incomplete(args, **kwargs):
        result = original(args, **kwargs)
        if "get-object" in args:
            if broken == "etag":
                result.stdout = "{}"
            elif broken == "body":
                Path(args[-1]).unlink()
            elif broken == "json":
                Path(args[-1]).write_text("invalid")
            else:
                result.returncode, result.stderr = 254, "AccessDenied"
        return result

    tools.call = incomplete
    resumed = restart(installer, tools)
    with pytest.raises(Refusal):
        resumed.route(False)
    assert resumed.receipt["pending_route"] == expected
    assert resumed.receipt["public_route_enabled"] is None
    assert tools.route["enabled"] is True


def test_supported_lock_recovery_disables_unacknowledged_enable_before_unlock(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = lost_response(
        tools, installer.route_key, "put-object", read_failure=True
    )
    with pytest.raises(Refusal):
        with installer.exclusive():
            installer.route(True)
    tools.call = original
    resumed = restart(installer, tools)
    resumed.recover_lock(resumed.run_id)
    assert tools.route["enabled"] is False and tools.lock_object is None
    assert "pending_route" not in resumed.receipt
    verbs = [args for args, _ in tools.calls]
    delete_index = next(i for i, args in enumerate(verbs) if "delete-object" in args)
    assert any(
        "put-object" in args and key_is(args, installer.route_key)
        for args in verbs[:delete_index]
    )


def test_unresolved_route_recovery_retains_owned_lock(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = lost_response(
        tools, installer.route_key, "put-object", read_failure=True
    )
    with pytest.raises(Refusal):
        with installer.exclusive():
            installer.route(True)
    resumed = restart(installer, tools)
    with pytest.raises(Refusal, match="readback unavailable"):
        resumed.recover_lock(resumed.run_id)
    assert tools.lock_object is not None and resumed.receipt["pending_route"]
    assert not any("delete-object" in args for args, _ in tools.calls)
    tools.call = original


def test_fresh_cleanup_carries_pending_commit_evidence(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path / "first", environment, release, monkeypatch)
    original = lost_response(
        tools, installer.route_key, "put-object", read_failure=True
    )
    with pytest.raises(Refusal):
        installer.route(True)
    previous = json.loads(installer.receipt_path.read_text())
    tools.call = original
    cleanup = Installer(environment, release, tmp_path / "cleanup", tools)
    tools.installer = cleanup
    cleanup.cleanup(previous)
    assert cleanup.run_id != previous["run_id"]
    assert tools.route["enabled"] is False
    assert "pending_route" not in cleanup.receipt and tools.lock_object is None


def test_failed_acquisition_with_verified_absence_is_recoverable(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    tools.failure = "put-object"
    with pytest.raises(Refusal):
        with installer.exclusive():
            pytest.fail("unacquired lock")
    assert tools.lock_object is None and installer.receipt["lock_attempt"]
    tools.failure = None
    resumed = restart(installer, tools)
    resumed.recover_lock(resumed.run_id)
    assert "lock_attempt" not in resumed.receipt
    with resumed.exclusive():
        pass


def test_lost_enable_ack_still_runs_verification_and_disables_on_failure(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.preflight()
    original = tools.call
    enabled = False

    def lose_enable(args, **kwargs):
        nonlocal enabled
        result = original(args, **kwargs)
        if (
            "put-object" in args
            and key_is(args, installer.route_key)
            and tools.route["enabled"]
        ):
            enabled = True
            raise Refusal("enable committed but response lost")
        return result

    def fail_verification(_):
        assert enabled and tools.route["enabled"] is True
        raise Refusal("public verification failed")

    tools.call = lose_enable
    monkeypatch.setattr(installer, "verify", fail_verification)
    with pytest.raises(Refusal, match="public verification failed"):
        installer.execute(installer.receipt["plan_sha256"], "verified-user")
    assert tools.route["enabled"] is False and tools.lock_object is not None
    assert installer.receipt["status"] == "recovery-required"


def test_failed_durable_ack_keeps_pending_identity(
    tmp_path, environment, release, monkeypatch
):
    import installation.runner as runner

    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = runner.atomic

    def failed_ack(path, value):
        if path == installer.receipt_path and value.get("public_route_enabled") is True:
            raise OSError("receipt storage unavailable")
        return original(path, value)

    monkeypatch.setattr(runner, "atomic", failed_ack)
    with pytest.raises(OSError):
        installer.route(True)
    assert installer.receipt["pending_route"]["value"] == tools.route
    assert (
        json.loads(installer.receipt_path.read_text())["pending_route"]
        == installer.receipt["pending_route"]
    )
    monkeypatch.setattr(runner, "atomic", original)
    resumed = restart(installer, tools)
    resumed.check_route_owner()
    assert resumed.receipt["public_route_enabled"] is True
    assert "pending_route" not in resumed.receipt


def test_preparation_cli_refuses_unresolved_attempt_before_mutation(
    tmp_path, environment, release, monkeypatch, capsys
):
    from installation.__main__ import main

    installer, tools = setup(tmp_path / "run", environment, release, monkeypatch)
    tools.failure = "put-object"
    with pytest.raises(Refusal):
        with installer.exclusive():
            pytest.fail("unacquired lock")
    env_path, release_path = tmp_path / "env.json", tmp_path / "release.json"
    env_path.write_text(json.dumps(environment))
    release_path.write_text(json.dumps(release))
    monkeypatch.setattr(
        "installation.database_preparation.prepare",
        lambda *args: pytest.fail("preparation must not run before lock recovery"),
    )
    result = main(
        [
            "--environment",
            str(env_path),
            "--release-lock",
            str(release_path),
            "--output",
            str(installer.directory),
            "--prepare-database",
            "--apply-preparation",
            "--resume",
        ]
    )
    assert result == 2
    assert "requires lock/temporary-namespace recovery" in capsys.readouterr().out
