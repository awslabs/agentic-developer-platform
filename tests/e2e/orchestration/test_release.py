"""Offline current-release E1 adapter contracts; no recursive qualification."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.scenarios import DeliveryAdapter
from tests.e2e.orchestration.scenarios import release
from tests.e2e.orchestration.scenarios.definitions import DEFINITION_HASH, fixture_test
from tests.e2e.orchestration.scenarios.http import Unsupported


@pytest.fixture
def release_fixture(valid_config, monkeypatch):
    target = NS(namespace="fixture")
    manifest = NS(runtime={"engine": target})
    graph = {
        "slug": "q-release0123456789",
        "nodes": [{"id": "eval", "kind": "eval", "node_ref": "verify"}],
    }
    plans = [
        {
            "superseded_at": None,
            "version": 1,
            "plan_document": {
                "spec_revision": DEFINITION_HASH,
                "execution_policy": {"policy_hash": "policy-hash"},
            },
        }
    ]
    actor = {
        "org_id": valid_config.org_ref,
        "user_id": valid_config.identity_ref,
        "is_admin": False,
    }
    observed_target = {
        "provider": "aws",
        "account_id": valid_config.expected_account_id,
        "region": "us-east-1",
        "resource_kind": "eks-namespace",
        "resource_id": "fixture",
    }
    spec = NS(
        criteria=[
            NS(criterion_id=k, kind=v, required=True)
            for k, v in release.EVALUATION_CRITERIA.items()
        ],
        target=NS(model_dump=lambda: observed_target),
        fixtures=NS(
            fixture_set_id="q2-delivery-v1",
            definition_hash=DEFINITION_HASH,
            roles=["member"],
            org_refs=[valid_config.org_ref],
            minimum_rows_per_org=1,
        ),
    )
    context = NS(
        specification=spec,
        flow_id="flow",
        node_id="eval",
        accepted_plan_version=1,
        policy_hash="policy-hash",
        actual_revision="a" * 40,
    )
    client = Mock()
    client.get.side_effect = (
        lambda path: actor
        if path == "/auth/me"
        else plans
        if path.endswith("/plans")
        else graph
    )
    runtime = {
        "account_id": valid_config.expected_account_id,
        "actual_revision": "a" * 40,
        "digest": "sha256:" + "b" * 64,
        "cluster": f"arn:aws:eks:us-east-1:{valid_config.expected_account_id}:cluster/fixture",
    }
    monkeypatch.setattr(release, "Client", Mock(return_value=client))
    monkeypatch.setattr(release, "load_manifest", lambda config: (manifest, "hash"))
    monkeypatch.setattr(release, "verify_checkout", Mock())
    monkeypatch.setattr(release, "read_runtime", Mock(return_value=runtime))
    monkeypatch.setattr(
        release,
        "execute_source",
        Mock(return_value={"successful": True, "tests_run": 3, "skipped": 0}),
    )
    monkeypatch.setattr(
        release,
        "observe",
        Mock(return_value={"current_ui": graph, "preview_ui": graph, "api": graph}),
    )
    inventory = Inventory.create(
        valid_config.artifact_directory,
        "q-evaluation0123456789",
        valid_config.environment,
    )
    return NS(
        config=valid_config,
        context=context,
        inventory=inventory,
        client=client,
        plans=plans,
        runtime=runtime,
    )


def test_e1_observes_existing_release_without_creating_a_flow(release_fixture):
    ctx = release_fixture
    result = DeliveryAdapter().execute_evaluation(
        config=ctx.config, inventory=ctx.inventory, providers={}, context=ctx.context
    )
    assert {c["outcome"] for c in result["criteria"]} == {"pass"}
    assert result["actual_revision"] == ctx.context.actual_revision
    assert ctx.inventory.fixtures == []
    ctx.client.request.assert_not_called()
    request = release.execute_source.call_args.args[-1]
    assert request["tests"] == fixture_test(0, "q-release0123456789")


@pytest.mark.parametrize(
    "attack",
    ["plan", "policy", "revision", "account", "criteria", "roles", "population"],
)
def test_e1_refuses_changed_provenance(release_fixture, attack):
    ctx = release_fixture
    if attack == "plan":
        ctx.plans[0]["version"] = 2
    elif attack == "policy":
        ctx.context.policy_hash = "changed"
    elif attack in {"revision", "account"}:
        ctx.runtime["actual_revision" if attack == "revision" else "account_id"] = (
            "wrong"
        )
    elif attack == "criteria":
        ctx.context.specification.criteria.pop()
    elif attack == "roles":
        ctx.context.specification.fixtures.roles = ["admin"]
    else:
        ctx.context.specification.fixtures.minimum_rows_per_org = 2
    with pytest.raises(Unsupported):
        release.evaluate_release(
            config=ctx.config, inventory=ctx.inventory, context=ctx.context
        )
    ctx.client.request.assert_not_called()


def test_e1_records_failed_deployed_test_without_claiming_success(
    release_fixture, monkeypatch
):
    ctx = release_fixture
    monkeypatch.setattr(
        release,
        "execute_source",
        Mock(return_value={"successful": False, "tests_run": 3, "skipped": 0}),
    )
    result = release.evaluate_release(
        config=ctx.config, inventory=ctx.inventory, context=ctx.context
    )
    assert result["criteria"][0]["outcome"] == "fail"
