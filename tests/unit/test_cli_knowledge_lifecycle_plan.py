"""Fail-closed source identities and multi-event knowledge watch grading."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def plan():
    path = (
        Path(__file__).parents[1] / "e2e/cli_uplift/remote/knowledge_lifecycle_plan.py"
    )
    spec = importlib.util.spec_from_file_location("knowledge_plan", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config():
    return {
        "evaluation_id": "owned-test",
        "gateway_url": "https://gateway.example",
        "knowledge_lifecycle": {
            "canonical_user_id": "bec852f3-d20a-4d58-84cf-3c25a1d63079",
            "login_user_id": "e512c3c3-cc09-4d09-8820-1a887b77d633",
            "tenant_id": "aws-e",
            "bucket": "owned-documents",
        },
    }


def event(status="ok"):
    return {
        "status": status,
        "detail": {
            "asset_id": "aa852f3d-d20a-4d58-84cf-3c25a1d63079",
            "status": "complete",
            "run_id": "bb852f3d-d20a-4d58-84cf-3c25a1d63079",
            "run_status": "complete",
            "usable": status == "ok",
            "stages": [
                {"stage": "s3_upload", "status": "completed"},
                {"stage": "graphrag", "status": "skipped"},
            ],
        },
    }


def test_exact_owned_source_is_stable_and_scoped(plan):
    cfg = config()
    result = plan.recovery_plan(cfg)
    assert result == plan.recovery_plan(copy.deepcopy(cfg))
    assert result["key"].startswith(
        "users/" + cfg["knowledge_lifecycle"]["canonical_user_id"] + "/"
    )
    assert result["key"].endswith("/source.md")
    assert result["content_bytes"] <= 512
    assert result["registration_key"] != result["reindex_key"]
    cfg["knowledge_lifecycle"]["tenant_id"] = "another"
    assert plan.recovery_plan(cfg)["registration_key"] != result["registration_key"]


@pytest.mark.parametrize("bucket", ["bucket/path", "s3://bucket", "", "bucket?token=x"])
def test_source_bucket_cannot_smuggle_path_or_credentials(plan, bucket):
    cfg = config()
    cfg["knowledge_lifecycle"]["bucket"] = bucket
    with pytest.raises(ValueError):
        plan.recovery_plan(cfg)


def test_watch_accepts_ndjson_and_preserves_pending_timeout(plan):
    pending, complete = event("pending"), event()
    asset = complete["detail"]["asset_id"]
    assert plan.watch_events(json.dumps(pending), asset)[-1]["status"] == "pending"
    assert (
        len(plan.watch_events(json.dumps(pending) + "\n" + json.dumps(complete), asset))
        == 2
    )


@pytest.mark.parametrize(
    "change",
    [
        {"asset_id": "cc852f3d-d20a-4d58-84cf-3c25a1d63079"},
        {"run_id": None},
        {"run_id": "bad"},
        {"run_status": "running"},
        {"status": "indexing"},
        {"usable": False},
        {"stages": []},
        {"stages": [{"status": "skipped"}]},
        {"stages": [{"status": "completed"}, {"status": "failed"}]},
    ],
)
def test_watch_rejects_false_success(plan, change):
    value = event()
    asset = value["detail"]["asset_id"]
    value["detail"].update(change)
    with pytest.raises(ValueError):
        plan.watch_events(json.dumps(value), asset)


@pytest.mark.parametrize("stream", ["", "[]", "not json", '{"status":"ok"}'])
def test_watch_rejects_missing_or_malformed_evidence(plan, stream):
    with pytest.raises(ValueError):
        plan.watch_events(stream, event()["detail"]["asset_id"])


def test_watch_rejects_post_terminal_events(plan):
    value = event()
    with pytest.raises(ValueError):
        plan.watch_events(
            json.dumps(value) + "\n" + json.dumps(event("pending")),
            value["detail"]["asset_id"],
        )


def dispatch_fixture():
    return {
        **config()["knowledge_lifecycle"],
        "owned_mutations_authorized": True,
        "source_upload_verified": True,
        "runtime_cost_verified": True,
        "max_attempts": 6,
        "max_spend_usd": 0.5,
        "verified_worst_case_usd": 0.2,
        "cost_evidence_sha256": "a" * 64,
        "source_etag": "b" * 32,
        "source_version_id": "null",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_cost_verified", False),
        ("source_upload_verified", False),
        ("owned_mutations_authorized", False),
        ("max_attempts", 7),
        ("max_attempts", True),
        ("max_spend_usd", 2),
        ("verified_worst_case_usd", 0.6),
        ("verified_worst_case_usd", float("nan")),
        ("cost_evidence_sha256", ""),
        ("source_etag", ""),
        ("source_version_id", ""),
    ],
)
def test_dispatch_refuses_unverified_or_unbounded_fixture(plan, field, value):
    fixture = dispatch_fixture()
    fixture[field] = value
    with pytest.raises(ValueError):
        plan.validate_dispatch_fixture(fixture)


def test_explicit_diagnostic_excluded_from_default_suites(plan):
    from tests.e2e.cli_uplift import cases, fixtures

    fixture = dispatch_fixture()
    fixtures.validate_fixture("knowledge_lifecycle", fixture)
    assert cases.BY_ID["D04"] not in cases.suite_cases("nightly")
    assert cases.BY_ID["D04"] not in cases.suite_cases("full")


@pytest.fixture
def scenario(monkeypatch):
    remote = Path(__file__).parents[1] / "e2e/cli_uplift/remote"
    monkeypatch.syspath_prepend(str(remote))
    spec = importlib.util.spec_from_file_location(
        "knowledge_scenario", remote / "knowledge_lifecycle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_guard_precedes_session_or_cli_access(scenario, monkeypatch):
    monkeypatch.setattr(
        scenario.common, "session_tokens", lambda _: pytest.fail("read session")
    )
    with pytest.raises(ValueError):
        scenario.execute({"knowledge_lifecycle": {}}, {"transcript": []})


@pytest.mark.parametrize(
    "outcome", ["success", "pending", "unknown_reindex", "stale_after_reindex"]
)
def test_owned_lifecycle_cleanup_requires_terminal_known_outcome(
    scenario, monkeypatch, tmp_path, outcome
):
    cfg = config()
    cfg.update(
        knowledge_lifecycle=dispatch_fixture(),
        test_user_id=dispatch_fixture()["login_user_id"],
        work_dir=str(tmp_path),
        cli_path="adp",
    )
    cfg["recovery_plan"] = scenario.recovery_plan(cfg)
    asset = event()["detail"]["asset_id"]
    state = {"exists": False, "reindex": 0, "deleted": False}
    row = {
        "id": asset,
        "source_ref_sha256": scenario.source_digest(cfg["recovery_plan"]["source_ref"]),
    }

    class Cli:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, args, **kwargs):
            state["exists"] = True
            return 4, {"status": "pending"}

        def json(self, args, **kwargs):
            if args[:2] == ["models", "mappings"]:
                return {
                    "detail": {
                        "principal_id": cfg["knowledge_lifecycle"]["canonical_user_id"],
                        "tenant_id": "aws-e",
                    }
                }
            if "--dry-run" in args:
                return {"status": "preview"}
            if args[1] == "show":
                return {"detail": row}
            if args[1] == "reindex":
                state["reindex"] += 1
                if outcome == "unknown_reindex":
                    return {"status": "failed"}
                return {
                    "status": "pending",
                    "detail": {
                        **row,
                        "status": "queued" if state["reindex"] == 1 else "indexing",
                        "updated_at": str(state["reindex"]),
                    },
                }
            if args[1] == "status":
                return {
                    "status": "pending" if outcome == "pending" else "ok",
                    "detail": {
                        "status": "indexing" if outcome == "pending" else "complete",
                        "run_id": event()["detail"]["run_id"]
                        if outcome == "stale_after_reindex"
                        else "cc852f3d-d20a-4d58-84cf-3c25a1d63079",
                        "run_status": "complete",
                    },
                }
            if args[1] == "delete":
                state.update(exists=False, deleted=True)
                return {"status": "ok", "detail": {"soft_deleted": True}}
            pytest.fail(str(args))

    def watching(cli, target):
        value = event("pending" if outcome == "pending" else "ok")
        if state["reindex"] and outcome != "stale_after_reindex":
            value["detail"]["run_id"] = "cc852f3d-d20a-4d58-84cf-3c25a1d63079"
        return [value]

    monkeypatch.setattr(scenario.common, "Cli", Cli)
    monkeypatch.setattr(scenario.common, "clean_env", lambda *a, **k: {})
    monkeypatch.setattr(scenario.common, "session_tokens", lambda _: {})
    monkeypatch.setattr(scenario, "_write_session", lambda *a: None)
    monkeypatch.setattr(
        scenario, "find_source", lambda *a: row if state["exists"] else None
    )
    monkeypatch.setattr(scenario, "watch", watching)
    evidence = {"transcript": []}
    if outcome == "success":
        scenario.execute(cfg, evidence)
        assert state["reindex"] == 2
        assert state["deleted"] is True
        assert evidence["detail"]["input_cleanup_ready"] is True
    else:
        with pytest.raises(scenario.common.RemoteError):
            scenario.execute(cfg, evidence)
        assert state["deleted"] is False
        assert evidence["detail"]["input_cleanup_ready"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "instance_loss",
        "changed_receipt",
        "unverified_cost",
        "sink_failure",
        "missing_manifest",
    ],
)
def test_upload_receipt_and_cost_bounds_persist_before_ssm(plan, tmp_path, fault):
    from tests.e2e.cli_uplift import cleanup, live
    from tests.e2e.cli_uplift.ports import PortError

    payload = config()
    payload["knowledge_lifecycle"] = dispatch_fixture()
    payload["recovery_plan"] = plan.recovery_plan(payload)
    calls, retained = [], []

    def persist(document, **kwargs):
        if fault == "sink_failure":
            raise RuntimeError("External sink unavailable")
        retained.append(copy.deepcopy(document))

    manifest = cleanup.Manifest(
        tmp_path / "manifest.json", "knowledge", on_change=persist
    )

    class Ssm:
        def json_result(self, *args, **kwargs):
            calls.append("ssm")
            assert retained[-1]["diagnostic_intents"][
                "knowledge_lifecycle:owned-test"
            ] == plan.recovery_plan(payload)
            raise RuntimeError("Instance lost")

    if fault == "changed_receipt":
        payload["knowledge_lifecycle"]["source_etag"] = "c" * 32
    if fault == "unverified_cost":
        payload["knowledge_lifecycle"]["runtime_cost_verified"] = False
    worker = live._run_worker(Ssm(), {}, lambda *args: calls.append("install"))
    with pytest.raises((RuntimeError, ValueError, PortError)):
        worker(
            "i-owned",
            "knowledge_lifecycle",
            payload,
            manifest=None if fault == "missing_manifest" else manifest,
        )
    if fault == "instance_loss":
        assert calls == ["install", "ssm"]
        recovered = retained[-1]["diagnostic_intents"]["knowledge_lifecycle:owned-test"]
        assert recovered["source_receipt"]["source_etag"] == "b" * 32
        assert recovered["cost_bounds"]["max_spend_usd"] == 0.5
    else:
        assert calls == []
