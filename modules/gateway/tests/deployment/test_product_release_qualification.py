"""A Product rollback preserves durable receipts and restores the candidate."""

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("product_qualification", ROOT / "modules/gateway/scripts/product-release-qualification.py")
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


@pytest.fixture
def release_pair():
    baseline = {
        "component": "gateway-backend",
        "workflow_path": ".github/workflows/gateway-deploy.yml",
        "run_id": 40,
        "run_attempt": 1,
        "source_revision": qualification.ENGINE_FLOOR,
        "workflow_revision": "b" * 40,
        "account_id": "000000000000",
        "region": "us-east-1",
        "resource_id": "example-cluster/adp-gateway",
        "image_digest": "sha256:" + "c" * 64,
        "migration_head": "schema-head",
        "cli_manifest_sha256": "d" * 64,
        "product_profile_enabled": True,
        "product_onboarding_enabled": False,
    }
    candidate = {
        **baseline,
        "run_id": 42,
        "source_revision": "f" * 40,
        "workflow_revision": "e" * 40,
        "image_digest": "sha256:" + "f" * 64,
    }
    runtime = {
        key: candidate[key]
        for key in (
            "source_revision",
            "workflow_revision",
            "image_digest",
            "migration_head",
            "account_id",
            "region",
            "resource_id",
            "product_profile_enabled",
            "product_onboarding_enabled",
        )
    }
    return candidate, baseline, runtime


@pytest.fixture
def recovery(release_pair):
    candidate, baseline, runtime = release_pair
    owned = {
        "run_id": "product-42-1",
        "product_id": "owned-product",
        "history_ref": "private:owned-history",
        "idempotency_key": "owned-key",
        "idempotency_receipt": "private:original-receipt",
    }
    immutable_images = {
        candidate["source_revision"]: candidate["image_digest"],
        baseline["source_revision"]: baseline["image_digest"],
    }
    state = {"calls": [], "current": candidate, "admissions": True, "saved": []}

    def retain(report):
        state["saved"].append(copy.deepcopy(report))
        state["calls"].append("retain")

    def create():
        state["calls"].append("create")
        return copy.deepcopy(owned)

    def quiesce():
        state["calls"].append("quiesce")
        state["admissions"] = False
        return {"admissions_disabled": True}

    def switch(artifact, *, admissions):
        state["calls"].append("switch:" + artifact["source_revision"])
        assert not admissions or artifact["source_revision"] == candidate["source_revision"]
        state["current"] = artifact
        state["admissions"] = admissions

    def observe(fixture):
        state["calls"].append("observe")
        return {
            **state["current"],
            **(fixture or {}),
            "product_profile_enabled": state["admissions"],
            "product_onboarding_enabled": False,
            "supported_routes_ok": True,
            "non_product_available": True,
        }

    def cleanup(fixture):
        state["calls"].append("cleanup")
        assert fixture == owned
        return {"run_id": fixture["run_id"], "owned_absent": True, "shared_unchanged": True}

    return state, dict(
        candidate=candidate,
        baseline=baseline,
        runtime=runtime,
        is_ancestor=lambda source, target: (source, target)
        in {
            (qualification.ENGINE_FLOOR, baseline["source_revision"]),
            (baseline["source_revision"], candidate["source_revision"]),
            (qualification.RUNTIME_FLOOR, candidate["source_revision"]),
        },
        resolve_image=immutable_images.__getitem__,
        create=create,
        quiesce=quiesce,
        switch=switch,
        observe=observe,
        cleanup=cleanup,
        retain=retain,
    )


def test_compatible_rollback_retains_owned_history_and_restores_candidate(recovery):
    state, params = recovery
    report = qualification.qualify(**params)
    assert report["status"] == "passed"
    assert [attempt["stage"] for attempt in report["attempts"]] == [
        "created",
        "quiesced",
        "candidate_quiesced",
        "baseline_restore_started",
        "baseline_verified",
        "candidate_restore_started",
        "candidate_verified",
        "cleaned",
    ]
    assert (
        state["calls"].index("quiesce")
        < state["calls"].index("switch:" + params["baseline"]["source_revision"])
        < state["calls"].index("switch:" + params["candidate"]["source_revision"])
    )
    assert state["calls"].index("cleanup") > state["calls"].index("switch:" + params["candidate"]["source_revision"])
    assert state["saved"][-1] == report
    assert state["current"] == report["candidate"]


def test_changed_idempotency_receipt_fails_and_candidate_is_restored(recovery):
    state, params = recovery
    original_observe = params["observe"]

    def changed_receipt(owned):
        result = original_observe(owned)
        if state["current"]["source_revision"] == params["baseline"]["source_revision"]:
            result["idempotency_receipt"] = "different-receipt"
        return result

    params["observe"] = changed_receipt
    with pytest.raises(qualification.QualificationError) as failure:
        qualification.qualify(**params)
    assert any("idempotency receipt" in reason for reason in failure.value.report["failures"])
    assert state["current"] == failure.value.report["candidate"]
    assert state["saved"][-1] == failure.value.report
    assert "candidate_verified" in [attempt["stage"] for attempt in failure.value.report["attempts"]]


@pytest.mark.parametrize(
    "failure",
    (
        "schema",
        "baseline_image",
        "candidate_image",
        "engine",
        "runtime",
        "target",
        "flags",
    ),
)
def test_incompatible_or_unpinned_artifact_never_creates_fixture(recovery, failure):
    state, params = recovery
    if failure == "schema":
        params["baseline"]["migration_head"] = "old-head"
    elif failure == "baseline_image":
        params["baseline"]["image_digest"] = "sha256:" + "1" * 64
    elif failure == "candidate_image":
        params["candidate"]["image_digest"] = "sha256:" + "1" * 64
    elif failure == "engine":
        params["is_ancestor"] = lambda *_: False
    elif failure == "runtime":
        params["runtime"]["source_revision"] = "0" * 40
    elif failure == "target":
        params["baseline"]["resource_id"] = "other-cluster/adp-gateway"
    elif failure == "flags":
        params["baseline"]["product_onboarding_enabled"] = True
    with pytest.raises(ValueError):
        qualification.qualify(**params)
    assert state["calls"] == []


def staged(recovery):
    state, params = recovery

    class Store:
        def __init__(self, candidate, baseline):
            self.saved = None
            self.binding = {
                "run_id": "product-42-1",
                "source_revision": candidate["source_revision"],
                "workflow_revision": candidate["workflow_revision"],
                "account_id": candidate["account_id"],
                "region": candidate["region"],
                "resource_id": candidate["resource_id"],
                "candidate_image_digest": candidate["image_digest"],
                "baseline_source_revision": baseline["source_revision"],
                "baseline_image_digest": baseline["image_digest"],
            }

        def load(self):
            return copy.deepcopy(self.saved)

        def retain(self, report):
            self.saved = copy.deepcopy(report)

    binding = Store(params["candidate"], params["baseline"])
    stages = qualification.QualificationStages(
        params["candidate"],
        params["baseline"],
        params["runtime"],
        is_ancestor=params["is_ancestor"],
        resolve_image=params["resolve_image"],
        state=binding,
    )
    return state, params, stages


def test_staged_release_restores_then_cleans_only_owned_product(recovery):
    state, params, stages = staged(recovery)
    stages.begin(params["create"])
    stages.quiesce(params["quiesce"], params["observe"])
    stages.rollback(params["switch"])
    stages.verify_baseline(params["observe"])
    stages.restore(params["switch"])
    stages.verify_candidate(params["observe"])
    assert stages.finish(params["cleanup"])["status"] == "passed"
    assert state["calls"].index("cleanup") > state["calls"].index("switch:" + params["candidate"]["source_revision"])
    assert stages.report()["attempts"][-2]["stage"] == "cleanup_started"


def test_staged_interrupted_baseline_restores_candidate_without_cleanup(recovery):
    state, params, stages = staged(recovery)
    stages.begin(params["create"])
    stages.quiesce(params["quiesce"], params["observe"])
    stages.rollback(params["switch"])
    with pytest.raises(ValueError, match="completed recovery"):
        stages.finish(params["cleanup"])
    assert "cleanup" not in state["calls"]
    stages.restore(params["switch"])
    stages.verify_candidate(params["observe"])
    with pytest.raises(ValueError, match="completed recovery"):
        stages.finish(params["cleanup"])
    assert state["current"] == stages.candidate
    assert "cleanup" not in state["calls"]


def test_staged_failed_cleanup_retains_retry_intent(recovery):
    _, params, stages = staged(recovery)
    stages.begin(params["create"])
    stages.quiesce(params["quiesce"], params["observe"])
    stages.rollback(params["switch"])
    stages.verify_baseline(params["observe"])
    stages.restore(params["switch"])
    stages.verify_candidate(params["observe"])
    with pytest.raises(RuntimeError, match="interrupted"):
        stages.finish(lambda owned: (_ for _ in ()).throw(RuntimeError("interrupted")))
    assert stages.report()["attempts"][-1]["stage"] == "cleanup_started"
    assert stages.finish(params["cleanup"])["status"] == "passed"


@pytest.mark.parametrize("field", ("source_revision", "workflow_revision", "resource_id", "baseline_source_revision"))
def test_staged_binding_rejects_wrong_source_or_baseline(recovery, field):
    _, params, stages = staged(recovery)
    stages.state.binding[field] = "0" * 40
    with pytest.raises(ValueError, match="Recovery state differs"):
        qualification.QualificationStages(
            params["candidate"],
            params["baseline"],
            params["runtime"],
            is_ancestor=params["is_ancestor"],
            resolve_image=params["resolve_image"],
            state=stages.state,
        )


def test_staged_handoff_adopts_original_held_product_without_second_create(recovery):
    state, params, stages = staged(recovery)
    owned = {
        **params["create"](),
        "source_revision": params["candidate"]["source_revision"],
        "project_id": "owned-project",
        "tenant_id": "owned-tenant",
        "actor_id": "owned-actor",
    }
    state["calls"].clear()
    stages.adopt(owned)
    assert state["calls"] == []

    def observe_held(fixture):
        result = params["observe"](fixture)
        result["source_revision"] = state["current"]["source_revision"]
        return result

    stages.quiesce(params["quiesce"], observe_held)
    stages.rollback(params["switch"])
    stages.verify_baseline(observe_held)
    stages.restore(params["switch"])
    stages.verify_candidate(observe_held)

    def cleanup_held(fixture):
        assert fixture == owned
        original = {field: fixture[field] for field in ("run_id", "product_id", "history_ref", "idempotency_key", "idempotency_receipt")}
        return params["cleanup"](original)

    assert stages.finish(cleanup_held)["status"] == "passed"
    assert stages.report()["owned"] == owned


@pytest.mark.parametrize("tamper", ["run", "source", "idempotency", "unsafe"])
def test_staged_handoff_cannot_adopt_foreign_or_duplicate_product(recovery, tamper):
    _, params, stages = staged(recovery)
    owned = {
        **params["create"](),
        "source_revision": params["candidate"]["source_revision"],
        "project_id": "owned-project",
        "tenant_id": "owned-tenant",
        "actor_id": "owned-actor",
    }
    if tamper == "run":
        owned["run_id"] = "product-other-1"
    elif tamper == "source":
        owned["source_revision"] = "0" * 40
    elif tamper == "idempotency":
        owned["idempotency_receipt"] = ""
    else:
        owned["actor_token"] = "secret"
    with pytest.raises(ValueError, match="original held Product"):
        stages.adopt(owned)
    assert stages.report()["owned"] is None
    assert stages.report()["attempts"] == []
