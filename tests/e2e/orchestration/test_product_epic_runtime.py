"""Gateway-admitted plan and authenticated worker lineage with stubbed reads."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.config import load_config
from tests.e2e.orchestration.scenarios.definitions import DEFINITION_HASH
from tests.e2e.orchestration.scenarios.delivery import build_plan
from tests.e2e.orchestration.scenarios.http import Unsupported
from tests.e2e.orchestration.scenarios.workers import WorkerProvider
from tests.e2e.orchestration.test_product_epic_config import PRODUCT_EPIC


@pytest.fixture
def accepted(write_config):
    config = load_config(write_config({"product_epic": PRODUCT_EPIC}))
    manifest = SimpleNamespace(execution_policy={}, evaluation={})
    plan = build_plan(config, manifest, "q-0123456789abcdef", ["10", "10"])
    return config, plan


def worker_fixture(config, plan):
    qualification_id = "q-0123456789abcdef"
    nodes = []
    invocations = {}
    for index, proposed in enumerate((plan["nodes"][0], plan["nodes"][3]), 1):
        invocation_id = f"invocation-{index}"
        target = config.product_epic.stories[index - 1]
        node = {
            "id": f"node-{index}",
            "node_ref": "first" if index == 1 else "second",
            "issue_ref": proposed["issue_ref"],
            "repository_identity": proposed["repository_identity"],
            "provider_repository_id": proposed["provider_repository_id"],
            "repository_connection": proposed["repository_connection"],
            "run_id": f"run-{index}",
            "execution_history": {
                "history_complete": True,
                "runs": [{"invocation_id": invocation_id}],
            },
        }
        nodes.append(node)
        invocations[f"/me/agent-invocations/{invocation_id}"] = {
            "repo": target.repository,
            "issue_number": 10,
            "correlation_id": node["run_id"],
        }
    graph = {
        "slug": qualification_id,
        "product_id": config.product_epic.product_id,
        "nodes": nodes,
    }
    plans = [
        {
            "superseded_at": None,
            "plan_document": {**plan, "spec_revision": DEFINITION_HASH},
        }
    ]
    client = Mock()
    client.get.side_effect = lambda path: {
        "/orchestration/flows/flow-1": graph,
        "/orchestration/flows/flow-1/plans": plans,
        **invocations,
    }[path]
    session = SimpleNamespace(
        config=config,
        inventory=SimpleNamespace(qualification_id=qualification_id),
        client=client,
    )
    provider = WorkerProvider(session)
    resources = [
        {
            "qualification_id": qualification_id,
            "flow_id": "flow-1",
            "node_id": node["id"],
            "invocation_id": f"invocation-{index}",
        }
        for index, node in enumerate(nodes, 1)
    ]
    return provider, resources, graph, plans, invocations


def test_worker_reads_each_accepted_story_repository_even_when_issue_numbers_collide(
    accepted,
):
    config, plan = accepted
    provider, resources, graph, _, _ = worker_fixture(config, plan)
    assert [provider._binding(resource)[1]["repo"] for resource in resources] == [
        target.repository for target in config.product_epic.stories
    ]
    assert [node["issue_ref"] for node in graph["nodes"]] == ["10", "10"]


@pytest.mark.parametrize(
    "change",
    [
        "wrong_product",
        "wrong_repo",
        "wrong_id",
        "wrong_connection",
        "wrong_issue",
        "wrong_invocation",
    ],
)
def test_worker_refuses_mismatched_product_target_or_invocation(accepted, change):
    config, plan = accepted
    provider, resources, graph, plans, invocations = worker_fixture(config, plan)
    if change == "wrong_product":
        graph["product_id"] = "other-product"
    elif change == "wrong_repo":
        graph["nodes"][0]["repository_identity"] = config.product_epic.stories[
            1
        ].repository
    elif change == "wrong_id":
        graph["nodes"][0]["provider_repository_id"] = config.product_epic.stories[
            1
        ].repository_id
    elif change == "wrong_connection":
        graph["nodes"][0]["repository_connection"] = {
            "provider": "github",
            "installation_id": 999,
        }
    elif change == "wrong_issue":
        graph["nodes"][0]["issue_ref"] = "11"
    else:
        invocations["/me/agent-invocations/invocation-1"]["repo"] = (
            config.product_epic.stories[1].repository
        )
    with pytest.raises(Unsupported):
        provider._binding(resources[0])


def test_worker_checks_accepted_plan_not_only_live_graph(accepted):
    config, plan = accepted
    provider, resources, _, plans, _ = worker_fixture(config, plan)
    plans[0]["plan_document"]["nodes"][0]["provider_repository_id"] = 999
    with pytest.raises(Unsupported, match="accepted Product story"):
        provider._binding(resources[0])


def test_legacy_worker_binding_requires_no_product(valid_config):
    qualification_id = "q-0123456789abcdef"
    node = {
        "id": "node-1",
        "issue_ref": "10",
        "run_id": "run-1",
        "execution_history": {
            "history_complete": True,
            "runs": [{"invocation_id": "invocation-1"}],
        },
    }
    client = Mock()
    client.get.side_effect = lambda path: {
        "/orchestration/flows/flow-1": {"slug": qualification_id, "nodes": [node]},
        "/orchestration/flows/flow-1/plans": [
            {"superseded_at": None, "plan_document": {"spec_revision": DEFINITION_HASH}}
        ],
        "/me/agent-invocations/invocation-1": {
            "repo": valid_config.repository,
            "issue_number": 10,
            "correlation_id": "run-1",
        },
    }[path]
    provider = WorkerProvider(
        SimpleNamespace(
            config=valid_config,
            inventory=SimpleNamespace(qualification_id=qualification_id),
            client=client,
        )
    )
    assert (
        provider._binding(
            {
                "qualification_id": qualification_id,
                "flow_id": "flow-1",
                "node_id": "node-1",
                "invocation_id": "invocation-1",
            }
        )[0]
        is node
    )
