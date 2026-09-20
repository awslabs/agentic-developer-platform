"""Q2 adapter contract tests; simulated boundaries are NEVER live qualification."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.scenarios import REGISTRY
from tests.e2e.orchestration.scenarios.definitions import (
    CRITERIA,
    DEFINITION_HASH,
    FAULTS,
    CONTROLS,
    TESTS,
    definition,
    digest,
)
from tests.e2e.orchestration.scenarios.delivery import build_plan, assert_review_repair
from tests.e2e.orchestration.scenarios.faults import (
    CASES,
    inject,
    assert_outcome,
    execute_case,
)
from tests.e2e.orchestration.scenarios.http import Client, Unsupported
from tests.e2e.orchestration.scenarios.manifest import Manifest, load_manifest
from tests.e2e.orchestration.scenarios.providers import IssueProvider


@pytest.fixture
def manifest_document(valid_config):
    target = dict(
        cluster="fixture-cluster",
        namespace="fixture",
        deployment="fixture",
        container="app",
        ecr_repository="fixture",
    )
    return dict(
        schema_version=1,
        definition_hash=DEFINITION_HASH,
        api_origin="https://fixture.example",
        planned_gates=["release", "refusal"],
        runtime={"engine": target, "worker": target},
        required_checks=["fixture-unittests"],
        deployment_workflows=[".github/workflows/fixture-deploy.yml"],
        execution_policy=dict(
            org_id=valid_config.org_ref,
            repository_ids=[valid_config.repository],
            environment_connection_ids=[valid_config.connection_ref],
            limits=dict(
                max_spend_usd=1, max_wall_clock_seconds=60, max_attempts_per_node=1
            ),
        ),
        evaluation={"acceptance_mode": "machine"},
    )


def test_fixed_matrix_is_registered_without_paid_execution():
    assert list(REGISTRY) == ["autonomous-delivery"]
    assert len({c.id for c in CRITERIA}) == len(CRITERIA) == 24
    assert set(CASES) == {c.id for c in FAULTS + CONTROLS} - {"A6-4.tenant"}
    assert DEFINITION_HASH == digest(definition())
    assert definition()["tests"] == TESTS


@pytest.mark.parametrize(
    "field,value",
    [
        ("definition_hash", "0" * 64),
        ("planned_gates", []),
        ("runtime", {}),
        ("api_origin", "http://fixture.example"),
        ("api_origin", "https://user:password@fixture.example"),
        ("api_origin", "https://fixture.example/arbitrary-api"),
        ("deployment_workflows", ["../other.yml"]),
        ("required_checks", []),
        ("criteria", []),
    ],
)
def test_manifest_cannot_weaken_scenario(manifest_document, field, value):
    manifest_document[field] = value
    with pytest.raises(ValueError):
        Manifest.model_validate(manifest_document)


def test_real_code_plan_keeps_evaluation_and_human_barriers(
    valid_config, manifest_document
):
    manifest = Manifest.model_validate(manifest_document)
    plan = build_plan(valid_config, manifest, "q-0123456789", ["101", "102"])
    assert [n["kind"] for n in plan["nodes"]] == ["story", "eval", "gate", "story"]
    assert [n.get("issue_ref") for n in plan["nodes"]] == ["101", None, None, "102"]
    assert plan["edges"] == [
        dict(from_address=a["address"], to_address=b["address"])
        for a, b in zip(plan["nodes"], plan["nodes"][1:])
    ]
    assert plan["execution_policy"]["evaluation_acceptance"] == {
        "q-0123456789/delivery/qualification/verify": "machine"
    }
    assert "evaluation_acceptance" not in manifest.execution_policy


def test_loaded_manifest_enforces_policy_bounds(
    valid_config, manifest_document, tmp_path, monkeypatch
):
    from tests.e2e.orchestration.scenarios import manifest

    monkeypatch.setattr(manifest, "ROOT", tmp_path)
    path = tmp_path / "manifest.json"
    config = replace(
        valid_config,
        scenario_manifest="manifest.json",
        versions={k: "a" * 40 for k in ("engine", "worker", "harness")},
    )
    path.write_text(json.dumps(manifest_document))
    assert load_manifest(config)[0].planned_gates == ["release", "refusal"]
    manifest_document["execution_policy"]["limits"]["max_spend_usd"] = 9999
    path.write_text(json.dumps(manifest_document))
    with pytest.raises(ValueError, match="bounds"):
        load_manifest(config)


@pytest.fixture
def owned_fault(valid_config):
    inventory = Inventory.create(
        valid_config.artifact_directory, "q-fault0123456789", valid_config.environment
    )
    tags = valid_config.ownership_tags(inventory.qualification_id)
    inventory.record_planned(
        fixture_id="worker",
        kind="qualification-worker",
        intended_identity=inventory.qualification_id + "/worker",
        ownership_tags=tags,
        idempotency_token="offline",
    )
    inventory.mark_created("worker", "worker-uid")
    provider = Mock()
    provider.read_tags.return_value = tags
    provider.inject.return_value = {"live": False, "simulation": "worker-loss"}
    return inventory, provider, valid_config


def test_fault_intent_is_durable_before_injection(owned_fault):
    inventory, provider, config = owned_fault

    def operation(name, resource_id):
        assert name == "worker-loss" and resource_id == "worker-uid"
        assert (
            json.loads((inventory.path.parent / "fault-worker-loss.json").read_text())[
                "status"
            ]
            == "started"
        )
        return {"live": False}

    provider.inject.side_effect = operation
    result = inject(
        CASES["A6-3.worker-loss"],
        fixture_id="worker",
        inventory=inventory,
        config=config,
        providers={"qualification-worker": provider},
    )
    assert result["live"] is False
    with pytest.raises(FileExistsError):
        inject(
            CASES["A6-3.worker-loss"],
            fixture_id="worker",
            inventory=inventory,
            config=config,
            providers={"qualification-worker": provider},
        )
    assert provider.inject.call_count == 1


def test_fault_refuses_foreign_tags(owned_fault):
    inventory, provider, config = owned_fault
    provider.read_tags.return_value = {"adp:qualification-id": "someone-else"}
    with pytest.raises(Exception, match="ownership"):
        inject(
            CASES["A6-3.worker-loss"],
            fixture_id="worker",
            inventory=inventory,
            config=config,
            providers={"qualification-worker": provider},
        )
    provider.inject.assert_not_called()


@pytest.mark.parametrize(
    "criterion", ["A6-3.tick-restart", "A6-3.missed-wakeup", "A6-3.duplicate-events"]
)
def test_worker_is_never_a_shared_controller_or_queue_target(owned_fault, criterion):
    inventory, provider, config = owned_fault
    with pytest.raises(ValueError, match="shared controller"):
        inject(
            CASES[criterion],
            fixture_id="worker",
            inventory=inventory,
            config=config,
            providers={"qualification-worker": provider},
        )
    provider.inject.assert_not_called()


@pytest.mark.parametrize("criterion", list(CASES))
def test_unsupported_case_is_not_simulated_success(criterion):
    session = SimpleNamespace(
        live=False,
        exercise=Mock(side_effect=Unsupported("isolated adapter unavailable")),
    )
    with pytest.raises(Unsupported):
        execute_case(CASES[criterion], session)


@pytest.fixture
def recovery_observation():
    before = dict(
        flow_id="simulated-flow",
        policy_id="simulated-policy",
        claim_generation=1,
        mutating_owner_count=1,
        coordinator_retriggers=0,
        effect_ids=["effect"],
        pending_operation="operation",
        progress_revision=1,
        terminal=False,
        explicit_block=None,
    )
    after = {**before, "progress_revision": 2, "terminal": True}
    return {
        "live": False,
        "before": before,
        "after": after,
        "injection": {"status": "complete", "liveness": "exited"},
    }


def test_normal_exit_requires_actual_continuation(recovery_observation):
    assert_outcome("wait-exit", recovery_observation)
    recovery_observation["after"]["progress_revision"] = 1
    with pytest.raises(AssertionError):
        assert_outcome("wait-exit", recovery_observation)


@pytest.mark.parametrize(
    "field,value",
    [
        ("flow_id", "other-flow"),
        ("policy_id", "other-policy"),
        ("claim_generation", 0),
        ("mutating_owner_count", 2),
        ("coordinator_retriggers", 1),
        ("effect_ids", ["same", "same"]),
        ("pending_operation", "replaced"),
        ("progress_revision", 0),
    ],
)
def test_recovery_rejects_duplicate_or_foreign_progress(
    recovery_observation, field, value
):
    recovery_observation["after"][field] = value
    with pytest.raises(AssertionError):
        assert_outcome("worker-loss", recovery_observation)


def test_501_is_never_proven_stop(recovery_observation):
    recovery_observation.update(
        graph={"halted": True},
        worker={"termination_confirmed": True, "status_code": 501},
    )
    with pytest.raises(AssertionError):
        assert_outcome("halt-stop", recovery_observation)


def reviewed_rows():
    rows = []
    for number in (1, 2):
        pr = dict(
            merged=True,
            merge_commit_sha="c" * 40,
            merged_at="2026-09-20T02:00:00Z",
            user={"id": 1},
            head={"sha": "b" * 40},
        )
        reviews = [
            dict(
                id=1,
                state="CHANGES_REQUESTED",
                commit_id="a" * 40,
                user={"id": 2},
                submitted_at="2026-09-20T00:00:00Z",
            ),
            dict(
                id=2,
                state="APPROVED",
                commit_id="b" * 40,
                user={"id": 2},
                submitted_at="2026-09-20T01:00:00Z",
            ),
        ]
        rows.append(
            dict(
                pr=pr,
                reviews=reviews,
                checks=[
                    dict(
                        id=1,
                        name="fixture-unittests",
                        head_sha="b" * 40,
                        conclusion="success",
                    )
                ],
            )
        )
    return rows


def test_review_requires_real_correction_and_current_head():
    rows = reviewed_rows()
    assert_review_repair(rows, ["fixture-unittests"])
    rows[0]["reviews"][0]["commit_id"] = "b" * 40
    with pytest.raises(AssertionError):
        assert_review_repair(rows, ["fixture-unittests"])


@pytest.mark.parametrize(
    "attack",
    [
        "self-review",
        "stale-approval",
        "missing-check",
        "failed-check",
        "later-refusal",
        "no-correction",
    ],
)
def test_false_review_success_is_refused(attack):
    rows = reviewed_rows()
    row = rows[0]
    if attack == "self-review":
        row["reviews"][-1]["user"]["id"] = 1
    elif attack == "stale-approval":
        row["reviews"][-1]["commit_id"] = "d" * 40
    elif attack == "missing-check":
        row["checks"] = []
    elif attack == "failed-check":
        row["checks"][0]["conclusion"] = "failure"
    elif attack == "later-refusal":
        row["reviews"].append(
            dict(
                id=3,
                state="CHANGES_REQUESTED",
                commit_id="b" * 40,
                user={"id": 2},
                submitted_at="2026-09-20T01:30:00Z",
            )
        )
    else:
        row["reviews"] = row["reviews"][1:]
    with pytest.raises(AssertionError):
        assert_review_repair(rows, ["fixture-unittests"])


def test_fixture_provider_never_reposts_unknown_creation(valid_config):
    client = Mock(config=valid_config)
    client.get.return_value = {"id": 1}
    provider = IssueProvider(client)
    client.request.side_effect = TimeoutError("unknown")
    with pytest.raises(TimeoutError):
        provider.create(
            intended_identity="q-0123456789/story-1",
            ownership_tags={"adp:qualification-id": "q-0123456789"},
            idempotency_token="unique",
        )
    assert client.request.call_count == 1


def test_real_connection_resolver_uses_registry_not_expected_config(
    valid_config, manifest_document
):
    client = Client(valid_config, Manifest.model_validate(manifest_document))
    client.get = Mock(
        side_effect=[
            {"org_id": valid_config.org_ref, "user_id": valid_config.identity_ref},
            [
                {
                    "id": valid_config.connection_ref,
                    "service": "aws",
                    "credential_type": "aws_role",
                    "scopes": {"account_id": "999988887777", "status": "verified"},
                }
            ],
            {"connections": [{"routable": True, "github_org_login": "actual-org"}]},
        ]
    )
    resolved = client.resolve_connection(valid_config.connection_ref)
    assert resolved.account_id == "999988887777" and resolved.org == "actual-org"
    assert resolved.account_id != valid_config.expected_account_id
