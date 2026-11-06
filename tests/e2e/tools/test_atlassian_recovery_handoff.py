"""Cross-filesystem recovery uses original authority and modeled external storage."""

import base64
from copy import deepcopy
import hashlib
import io
import json
import shutil
import tarfile

import pytest

from tests.e2e.tools import atlassian_recovery_consumer as consumer
from tests.e2e.tools import atlassian_recovery_handoff as handoff
from tests.e2e.tools import atlassian_recovery_scenario as scenario
from tests.e2e.tools import test_atlassian_fixture_lifecycle as lifecycle_suite


runtime = lifecycle_suite.runtime
lifecycle = lifecycle_suite.lifecycle


@pytest.fixture
def store():
    records = {}

    def publish(root, client):
        records[client] = (
            root.joinpath("capsule.json").read_bytes(),
            json.loads(root.joinpath("binding.json").read_bytes()),
        )
        return {
            "uri": "s3://private.example.invalid/owned",
            "version": "immutable-version",
            "run_id": "41",
            "attempt": "1",
            "revision": "a" * 40,
        }

    def read(receipt, *, account, client):
        return deepcopy(records[client])

    return records, publish, read


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_restored_consumer_works_after_original_filesystem_is_removed(
    tmp_path, lifecycle, store, client
):
    matrix, _, control, state = lifecycle
    state[2]["uncertain"] = (client, "create_jira_issue")
    assert matrix.execute(client, "create_jira_issue")["status"] == "unknown"
    scenario.exercise_consumer_loss(matrix, client)
    source = matrix.fixtures[client]["ledger"].path.parent
    expected = consumer.binding(matrix, client)
    original_transport = (source / "native-transport.json").read_bytes()
    parent = tmp_path / "new-runner"
    parent.mkdir(mode=0o700)
    records, publish, read = store
    origin = handoff.persist_and_restore(
        matrix,
        client,
        key=b"k" * 32,
        parent=parent,
        account="123456789012",
        publish_snapshot=publish,
        read_snapshot=read,
    )
    restored = matrix.fixtures[client]["ledger"].path.parent
    assert not source.exists() and restored.parent == parent
    assert (restored / "native-transport.json").read_bytes() == original_transport
    assert consumer.binding(matrix, client) == expected
    assert origin["archive_sha256"] == hashlib.sha256(records[client][0]).hexdigest()
    assert origin["restored_without_source"] is True
    assert consumer.consume(matrix, client)["status"] == "confirmed"
    matrix.close()
    assert consumer.consume(matrix, client)["status"] == "confirmed"
    assert state[2]["dispatches"] == [(client, "create_jira_issue")]
    assert len(state[2]["exchanges"]) == 1
    assert control["deletions"] == [client]
    matrix.close()


@pytest.mark.parametrize("failure", ["upload", "readback", "key"])
def test_publication_or_authentication_failure_preserves_original_state(
    tmp_path, lifecycle, store, failure
):
    matrix, _, _, state = lifecycle
    client = "codex"
    state[2]["uncertain"] = (client, "create_jira_issue")
    assert matrix.execute(client, "create_jira_issue")["status"] == "unknown"
    scenario.exercise_consumer_loss(matrix, client)
    source = matrix.fixtures[client]["ledger"].path.parent
    original = (source / "operations.json").read_bytes()
    parent = tmp_path / "new-runner"
    parent.mkdir(mode=0o700)
    records, publish, read = store

    def fail_publish(root, client):
        raise ValueError("storage unavailable")

    def fail_read(*args, **kwargs):
        capsule, record = read(*args, **kwargs)
        return capsule + b"changed", record

    with pytest.raises(ValueError):
        handoff.persist_and_restore(
            matrix,
            client,
            key=b"wrong" if failure == "key" else b"k" * 32,
            parent=parent,
            account="123456789012",
            publish_snapshot=fail_publish if failure == "upload" else publish,
            read_snapshot=fail_read if failure == "readback" else read,
        )
    assert (source / "operations.json").read_bytes() == original
    assert matrix.fixtures[client]["ledger"].path.parent == source
    matrix.close()


def test_protected_key_is_exactly_32_bytes():
    assert handoff.protected_key(base64.b64encode(b"k" * 32)) == b"k" * 32
    for invalid in (None, " ", "k" * 32, base64.b64encode(b"k" * 31)):
        with pytest.raises(ValueError):
            handoff.protected_key(invalid)


def test_fresh_consumer_cli_and_reader_load_restored_roots(
    lifecycle, store, monkeypatch
):
    from tests.e2e.tools import runner
    from tests.e2e.tools.atlassian_fixture_provider import FIELDS
    from tests.e2e.tools.atlassian_readback_grant import IdentityReadbackGrant

    matrix, _, control, runtime_state = lifecycle
    _, fixtures, state, connection = runtime_state
    parent = fixtures["codex"]["ledger"].path.parent.parent
    manifest = {"run_id": "run-one", "owners": {}, "readback": {}, "cleanup": {}}
    for client, fixture in fixtures.items():
        provider = fixture["cleanup_provider"]
        manifest["owners"][client] = fixture["owner_id"]
        manifest["readback"][client] = provider.grant.config
        manifest["cleanup"][client] = {key: provider.config[key] for key in FIELDS}
    consumer.save(parent / "clients.json", manifest)
    consumer.save(parent / "plans.json", matrix.plans)
    _, publish, read = store
    expected = {}
    for client in sorted(fixtures):
        state["uncertain"] = (client, "create_jira_issue")
        assert matrix.execute(client, "create_jira_issue")["status"] == "unknown"
        scenario.exercise_consumer_loss(matrix, client)
        expected[client] = {
            key: value
            for key, value in consumer.binding(matrix, client).items()
            if key != "plan_sha256"
        }
        handoff.persist_and_restore(
            matrix,
            client,
            key=b"k" * 32,
            parent=parent,
            account="123456789012",
            publish_snapshot=publish,
            read_snapshot=read,
            retain_pointer=True,
        )
        assert not (parent / client).exists()
    matrix.close()
    monkeypatch.setattr(
        IdentityReadbackGrant,
        "reader",
        lambda grant: next(
            fixture["reader"]
            for fixture in fixtures.values()
            if fixture["owner_id"] == grant.owner_id
        ),
    )
    monkeypatch.setattr(runner, "MCPClient", connection)
    assert (
        consumer.protected_bindings(
            parent / "clients.json",
            gateway_url=matrix.gateway_url,
            gateway_revision=matrix.gateway_revision,
        )
        == expected
    )
    for client in sorted(fixtures):
        arguments = [
            "--manifest",
            str(parent / "clients.json"),
            "--plans",
            str(parent / "plans.json"),
            "--gateway",
            matrix.gateway_url,
            "--revision",
            matrix.gateway_revision,
            "--client",
            client,
        ]
        assert consumer.main(arguments) == 0
        assert consumer.main(arguments) == 0
    assert sorted(control["deletions"]) == sorted(fixtures)
    assert len(state["dispatches"]) == 2 and len(state["exchanges"]) == 2
    assert not (parent / "codex").exists() and not (parent / "claude-code").exists()


@pytest.mark.parametrize(
    "location", ["../outside", "/absolute", "codex", "restored-native-missing"]
)
def test_recovery_root_map_rejects_nonprivate_locations(tmp_path, location):
    from tests.e2e.tools.atlassian_client_matrix import recovery_roots

    consumer.save(tmp_path / "recovery-roots.json", {"codex": location})
    with pytest.raises((ValueError, FileNotFoundError)):
        recovery_roots(tmp_path)


def test_restored_state_cannot_start_an_unattempted_cell(tmp_path, lifecycle, store):
    matrix, _, _, state = lifecycle
    state[2]["uncertain"] = ("codex", "create_jira_issue")
    assert matrix.execute("codex", "create_jira_issue")["status"] == "unknown"
    scenario.exercise_consumer_loss(matrix, "codex")
    parent = tmp_path / "restored"
    parent.mkdir(mode=0o700)
    _, publish, read = store
    handoff.persist_and_restore(
        matrix,
        "codex",
        key=b"k" * 32,
        parent=parent,
        account="123456789012",
        publish_snapshot=publish,
        read_snapshot=read,
    )
    assert matrix.execute("codex", "create_jira_issue")["status"] == "pass"
    assert matrix.execute("codex", "get_jira_issue")["status"] == "blocked"
    assert state[2]["dispatches"] == [("codex", "create_jira_issue")]
    matrix.close()


def published_snapshot(*, fault=None):
    client, account = "codex", "123456789012"
    name = handoff.publication_name(client, "41", "1")
    bucket = f"adp-dev-ci-evidence-{account}"
    prefix = f"artifacts/14/aws-e/adp/41/1/evaluate/{name}/"
    capsule = b'{"ciphertext":"encrypted-only"}'
    record = {
        "schema": handoff.SCHEMA,
        "binding": {"client": client},
        "archive_sha256": hashlib.sha256(capsule).hexdigest(),
    }
    if fault == "client":
        record["binding"]["client"] = "claude-code"
    if fault == "capsule":
        record["archive_sha256"] = "0" * 64
    files = {"capsule.json": capsule, "binding.json": json.dumps(record).encode()}
    if fault == "extra":
        files["unexpected.json"] = b"{}"
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:gz") as archive:
        for path, data in files.items():
            item = tarfile.TarInfo(path)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    archive = packed.getvalue()
    artifact_hash = hashlib.sha256(archive).hexdigest()
    artifact_key = prefix + artifact_hash + ".tar.gz"
    manifest = {
        "schema_version": 1,
        "repository": "aws-e/adp",
        "job": "evaluate",
        "run_id": "41",
        "run_attempt": "1",
        "name": name,
        "retention_days": 14,
        "checked_out_revision": "a" * 40,
        "workflow_revision": "a" * 40,
        "artifact": {
            "bucket": bucket,
            "key": artifact_key,
            "sha256": artifact_hash,
            "size": len(archive),
            "version_id": "immutable-version",
        },
        "files": [
            {
                "path": path,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for path, data in files.items()
        ],
    }
    if fault == "revision":
        manifest["checked_out_revision"] = "b" * 40
    if fault == "attempt":
        manifest["run_attempt"] = "2"
    if fault == "missing":
        manifest["missing_path_count"] = 1
    payload = json.dumps(manifest).encode()
    manifest_key = prefix + hashlib.sha256(payload).hexdigest() + ".manifest.json"
    objects = {manifest_key: payload, artifact_key: archive}

    def fetch(selected_bucket, selected_key):
        assert selected_bucket == bucket
        data = objects[selected_key]
        return data, {
            "VersionId": "different-version"
            if fault == "version"
            else "immutable-version",
            "ContentLength": len(data),
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
        }

    receipt = {
        "uri": f"s3://{bucket}/{manifest_key}",
        "version": "immutable-version",
        "run_id": "41",
        "attempt": "1",
        "revision": "a" * 40,
    }
    return receipt, fetch, capsule, record


def test_independent_publication_reader_verifies_version_checksum_and_binding():
    receipt, fetch, capsule, record = published_snapshot()
    assert handoff.read_publication(
        receipt, account="123456789012", client="codex", fetch=fetch
    ) == (capsule, record)


@pytest.mark.parametrize(
    "fault", ["client", "capsule", "extra", "revision", "attempt", "missing", "version"]
)
def test_independent_publication_reader_rejects_substitution(fault):
    receipt, fetch, _, _ = published_snapshot(fault=fault)
    with pytest.raises(ValueError):
        handoff.read_publication(
            receipt, account="123456789012", client="codex", fetch=fetch
        )


def test_restore_entrypoint_recovers_without_original_files_or_new_login(
    tmp_path, lifecycle, store, monkeypatch
):
    from tests.e2e.tools import runner
    from tests.e2e.tools.atlassian_fixture_provider import FIELDS
    from tests.e2e.tools.atlassian_readback_grant import IdentityReadbackGrant

    matrix, _, control, runtime_state = lifecycle
    _, fixtures, state, connection = runtime_state
    original = fixtures["codex"]["ledger"].path.parent.parent
    manifest = {"run_id": "run-one", "owners": {}, "readback": {}, "cleanup": {}}
    records, publish, read = store
    receipts = {}
    for client, fixture in list(fixtures.items()):
        provider = fixture["cleanup_provider"]
        manifest["owners"][client] = fixture["owner_id"]
        manifest["readback"][client] = provider.grant.config
        manifest["cleanup"][client] = {key: provider.config[key] for key in FIELDS}
        state["uncertain"] = (client, "create_jira_issue")
        assert matrix.execute(client, "create_jira_issue")["status"] == "unknown"
        scenario.exercise_consumer_loss(matrix, client)
        origin = handoff.persist_and_restore(
            matrix,
            client,
            key=b"k" * 32,
            parent=original,
            account="123456789012",
            publish_snapshot=publish,
            read_snapshot=read,
        )
        receipts[client] = origin["publication"]
    matrix.close()
    shutil.rmtree(original)
    fresh = tmp_path / "new-workflow"
    fresh.mkdir(mode=0o700)
    for client in fixtures:
        (fresh / client).mkdir(mode=0o700)
    consumer.save(fresh / "clients.json", manifest)
    monkeypatch.setattr(
        IdentityReadbackGrant,
        "reader",
        lambda grant: next(
            fixture["reader"]
            for fixture in fixtures.values()
            if fixture["owner_id"] == grant.owner_id
        ),
    )
    monkeypatch.setattr(runner, "MCPClient", connection)
    monkeypatch.setattr(handoff, "read_publication", read)
    for name, value in {
        "ADP_TOOLS_ATLASSIAN_RECOVERY_RESUME_JSON": json.dumps(receipts),
        "ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE": str(fresh / "clients.json"),
        "GATEWAY_URL": matrix.gateway_url,
        "GATEWAY_REVISION": matrix.gateway_revision,
        "GITHUB_SHA": "a" * 40,
        "EXPECTED_ACCOUNT": "123456789012",
        "ADP_TOOLS_ATLASSIAN_RECOVERY_KEY_B64": base64.b64encode(b"k" * 32).decode(),
        "GITHUB_ENV": str(fresh / "env"),
    }.items():
        monkeypatch.setenv(name, value)
    assert not original.exists() and not (fresh / "native-plans.json").exists()
    assert handoff.main() == 0
    assert len(state["exchanges"]) == 2 and len(state["dispatches"]) == 2
    assert sorted(control["deletions"]) == sorted(fixtures)
    restored_plans = consumer.load_native_plans(fresh / "native-plans.json")
    assert set(restored_plans) == set(fixtures)
    assert consumer.protected_bindings(
        fresh / "clients.json",
        gateway_url=matrix.gateway_url,
        gateway_revision=matrix.gateway_revision,
    ) == {
        client: {
            key: value
            for key, value in records[client][1]["binding"].items()
            if key != "plan_sha256"
        }
        for client in fixtures
    }
    assert handoff.main() == 0
    assert len(state["exchanges"]) == 2 and len(state["dispatches"]) == 2


@pytest.mark.parametrize("selection", ["SCN-8454-04", "SCN-8454-0[24]"])
def test_runner_and_independent_reader_join_original_durable_recovery(
    tmp_path, lifecycle, store, selection
):
    from tests.e2e.tools import evidence_reader, runner
    from tests.e2e.tools import test_atlassian_native_matrix as native_suite

    matrix, _, control, runtime_state = lifecycle
    matrix.close()
    plans, fixtures, state, connection = runtime_state
    parent = fixtures["codex"]["ledger"].path.parent.parent
    records, publish, read = store

    def transfer(executor, client):
        return handoff.persist_and_restore(
            executor,
            client,
            key=b"k" * 32,
            parent=parent,
            account="123456789012",
            publish_snapshot=publish,
            read_snapshot=read,
            retain_pointer=True,
        )

    output = tmp_path / "evidence"
    assert (
        runner.evaluate(
            selection,
            output,
            "https://gateway.example.test",
            native_suite.SOURCE,
            native_suite.IMAGE,
            native_suite.IMAGE,
            command=["unused"],
            live=True,
            client_factory=connection,
            atlassian_client_fixtures=fixtures,
            atlassian_native_plans=plans,
            atlassian_recovery_handoff=transfer,
        )
        is True
    )
    report = json.loads((output / "evidence.json").read_bytes())
    assert all(row["status"] == "pass" for row in report["scenarios"])
    assert len(state["dispatches"]) == 16 and len(state["exchanges"]) == 2
    assert sorted(control["deletions"]) == sorted(fixtures)
    publications = {client: record for client, (_, record) in records.items()}
    bindings = {
        client: {
            key: value
            for key, value in record["binding"].items()
            if key != "plan_sha256"
        }
        for client, record in publications.items()
    }
    arguments = dict(
        selected=report["selected_scenario_ids"],
        run_id=report["github_run_id"],
        attempt=report["github_run_attempt"],
        revision=report["harness_revision"],
        target=report["deployed_revisions"],
        recovery_bindings=bindings,
        recovery_publications=publications,
    )
    assert evidence_reader.validate_atlassian_rows({}, report, **arguments) == len(
        report["scenarios"]
    )
    with pytest.raises(ValueError):
        evidence_reader.validate_atlassian_rows(
            {}, report, **{**arguments, "recovery_publications": None}
        )
    publications["codex"] = {**publications["codex"], "archive_sha256": "0" * 64}
    with pytest.raises(ValueError):
        evidence_reader.validate_atlassian_rows({}, report, **arguments)
