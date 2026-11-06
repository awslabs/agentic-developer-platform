"""Offline provider transport for cross-repository Product delivery."""

from types import SimpleNamespace
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.config import load_config
from tests.e2e.orchestration.fixtures import FixtureRequest, provision, readback
from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.scenarios.delivery_resources import (
    DeliveryResourceProvider,
    branch_name,
)
from tests.e2e.orchestration.scenarios.definitions import DEFINITION_HASH
from tests.e2e.orchestration.scenarios.delivery import (
    FlowProvider,
    LiveSession,
    build_plan,
)
from tests.e2e.orchestration.scenarios.http import Unsupported
from tests.e2e.orchestration.scenarios.providers import IssueProvider
from tests.e2e.orchestration.test_product_epic_config import PRODUCT_EPIC


class ProviderTransport:
    def __init__(self, config):
        self.config = config
        self.issues = {}
        self.parents = {}
        self.calls = []
        self.accepted = None

    def verify_product_epic(self):
        return None

    def get(self, path, *, github=False):
        if path == "/user":
            return {"id": 99}
        if path == "/orchestration/flows/flow-1":
            return {
                "slug": "q-0123456789abcdef",
                "product_id": self.accepted["product_id"] if self.accepted else None,
            }
        if path == "/orchestration/flows/flow-1/plans":
            return [
                {
                    "plan_document": {
                        **(self.accepted or {}),
                        "spec_revision": DEFINITION_HASH,
                    }
                }
            ]
        for target in (
            *self.config.product_epic.stories,
            self.config.product_epic.epic,
        ):
            if path == "/repos/" + target.repository:
                return {"full_name": target.repository, "id": target.repository_id}
        epic = self.config.product_epic
        if path == f"/repos/{epic.epic.repository}/issues/{epic.epic_issue_number}":
            return {"number": epic.epic_issue_number, "id": 500}
        if path in self.issues:
            return self.issues[path]
        raise AssertionError(f"unexpected provider read {path}")

    def pages(self, path):
        self.calls.append(("LIST", path))
        return [
            issue
            for issue_path, issue in self.issues.items()
            if issue_path.startswith(
                path.split("?", 1)[0].removesuffix("/issues") + "/issues/"
            )
        ]

    def request(self, method, path, *, github=False, body=None):
        self.calls.append((method, path))
        if path == "/orchestration/flows" and method == "POST":
            self.accepted = body
            return 201, {"flow_id": "flow-1"}
        if method == "POST" and path.endswith("/issues"):
            issue = {
                "number": 10,
                "id": 600 + len(self.issues),
                "user": {"id": 99},
                "body": body["body"],
            }
            self.issues[path + "/10"] = issue
            return 201, issue
        if method == "GET" and path.endswith("/parent"):
            parent = self.parents.get(path)
            return (200, {"id": parent}) if parent is not None else (404, {})
        if method == "POST" and path.endswith("/sub_issues"):
            child = next(
                (
                    name
                    for name, issue in self.issues.items()
                    if issue["id"] == body["sub_issue_id"]
                ),
                None,
            )
            assert child is not None
            self.parents[child + "/parent"] = 500
            return 201, {}
        raise AssertionError(f"unexpected provider mutation {method} {path}")


@pytest.fixture
def wired(write_config):
    config = load_config(
        write_config({"product_epic": PRODUCT_EPIC, "bounds": {"max_resources": 25}})
    )
    return config, ProviderTransport(config)


def test_qualified_issue_publication_and_native_parentage(wired, artifact_dir):
    config, transport = wired
    inventory = Inventory.create(artifact_dir, "q-0123456789abcdef", config.environment)
    provider = IssueProvider(transport)
    issue_numbers = []
    for index in (1, 2):
        request = FixtureRequest(
            f"story-{index}",
            provider.kind,
            f"{inventory.qualification_id}/story-{index}",
        )
        record = provision(inventory, config, provider, request)
        assert record.observed_resource_id == f"{config.story_repository(index)}#10"
        readback(
            inventory,
            provider,
            request.fixture_id,
            config.ownership_tags(inventory.qualification_id),
        )
        provider.link_parent(
            record.observed_resource_id,
            config.ownership_tags(inventory.qualification_id),
        )
        issue_numbers.append(record.observed_resource_id.rsplit("#", 1)[-1])
    assert len(transport.issues) == 2
    assert len({record.observed_resource_id for record in inventory.fixtures}) == 2
    assert (
        len(
            [
                call
                for call in transport.calls
                if call[0] == "POST" and call[1].endswith("/sub_issues")
            ]
        )
        == 2
    )
    for index in (1, 2):
        assert (
            provider.find(
                intended_identity=f"{inventory.qualification_id}/story-{index}",
                idempotency_token=f"{inventory.qualification_id}-story-{index}",
            )
            == f"{config.story_repository(index)}#10"
        )
    assert issue_numbers == ["10", "10"]


def test_plan_submission_uses_product_and_each_story_repository(wired, tmp_path):
    config, transport = wired
    manifest = SimpleNamespace(
        execution_policy={"limits": {"max_attempts_per_node": 1}}, evaluation={}
    )
    plan = build_plan(config, manifest, "q-0123456789abcdef", ["10", "10"])
    assert plan["product_id"] == config.product_epic.product_id
    assert plan["origin_ref"]["repository"] == config.product_epic.epic.repository
    assert plan["tracker_refs"][0]["issue"]["issue_number"] == 5
    stories = [node for node in plan["nodes"] if node["kind"] == "story"]
    assert [
        (node["issue_ref"], node["repository_identity"], node["provider_repository_id"])
        for node in stories
    ] == [
        ("10", target.repository, target.repository_id)
        for target in config.product_epic.stories
    ]
    inventory = Inventory.create(tmp_path, "q-0123456789abcdef", config.environment)
    flow = FlowProvider(transport, inventory, plan)
    assert (
        flow.create(
            intended_identity=inventory.qualification_id + "/flow",
            ownership_tags=config.ownership_tags(inventory.qualification_id),
            idempotency_token="flow",
        )
        == "flow-1"
    )
    assert transport.accepted == plan


def test_foreign_native_parent_is_never_overwritten(wired, artifact_dir):
    config, transport = wired
    provider = IssueProvider(transport)
    record = provision(
        Inventory.create(artifact_dir, "q-0123456789abcdef", config.environment),
        config,
        provider,
        FixtureRequest("story-1", provider.kind, "q-0123456789abcdef/story-1"),
    )
    transport.parents[provider.issue_path(record.observed_resource_id) + "/parent"] = (
        999
    )
    with pytest.raises(Unsupported, match="reparenting refused"):
        provider.link_parent(
            record.observed_resource_id, config.ownership_tags("q-0123456789abcdef")
        )
    assert not any(
        call[0] == "POST" and call[1].endswith("/sub_issues")
        for call in transport.calls
    )


def test_colliding_pr_numbers_resolve_only_in_their_story_repositories(
    wired, monkeypatch
):
    from tests.e2e.orchestration.scenarios import cleanup

    config, _ = wired
    monkeypatch.setattr(cleanup, "find_flow", lambda client, qualification_id: "flow-1")
    qualification_id = "q-0123456789abcdef"
    pulls = {}
    for index, target in enumerate(config.product_epic.stories, 1):
        pulls[target.repository] = {
            "number": 10,
            "head": {
                "sha": "a" * 40,
                "ref": branch_name(qualification_id, index),
                "repo": {"full_name": target.repository, "id": target.repository_id},
            },
            "base": {
                "repo": {"full_name": target.repository, "id": target.repository_id}
            },
        }
    client = Mock(config=config)
    client.pages.side_effect = lambda path: [
        pulls[path.split("/pulls", 1)[0].removeprefix("/repos/")]
    ]
    client.get.side_effect = lambda path, **kwargs: pulls[
        path.split("/pulls", 1)[0].removeprefix("/repos/")
    ]
    provider = DeliveryResourceProvider(client, "qualification-pr")
    descriptors = []
    for index, target in enumerate(config.product_epic.stories, 1):
        descriptor = provider.find(
            intended_identity=f"{qualification_id}/story-{index}-pr",
            idempotency_token=f"{qualification_id}-story-{index}-pr",
        )
        assert provider.read_tags(descriptor) == config.ownership_tags(qualification_id)
        assert f"/repos/{target.repository}/pulls?state=all" in [
            call.args[0] for call in client.pages.call_args_list
        ]
        descriptors.append(descriptor)
    assert descriptors[0] != descriptors[1]
    pulls[config.product_epic.stories[0].repository]["base"]["repo"]["id"] = 999
    assert provider.read_tags(descriptors[0]) is None


def test_product_plan_passes_gateway_proposal_validation(wired, monkeypatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[3] / "modules/gateway")
    )
    from src.orchestration.proposal import LoopProposal, validate_proposal

    config, _ = wired
    document = json.loads(
        (Path(__file__).parent / "manifest.example.json").read_text(encoding="utf-8")
    )
    policy = document["execution_policy"]
    policy["repository_ids"] = [
        config.repository,
        *(target.repository for target in config.product_epic.stories),
    ]
    policy["environment_connection_ids"] = [
        config.connection_ref,
        *(target.connection_ref for target in config.product_epic.stories),
    ]
    plan = build_plan(
        config,
        SimpleNamespace(execution_policy=policy, evaluation=document["evaluation"]),
        "q-0123456789abcdef",
        ["10", "10"],
    )
    accepted = LoopProposal.model_validate(plan)
    assert validate_proposal(accepted) == []
    assert accepted.product_id == config.product_epic.product_id
    assert accepted.origin_ref.repository_id == config.product_epic.epic.repository_id
    assert [
        node.repository_identity for node in accepted.nodes if node.kind == "story"
    ] == [target.repository for target in config.product_epic.stories]


def test_existing_delivery_session_links_before_gateway_submission(
    wired, artifact_dir, monkeypatch
):
    from tests.e2e.orchestration.scenarios import runtime

    config, transport = wired
    manifest = SimpleNamespace(
        runtime={"engine": "engine", "worker": "worker"},
        execution_policy={"limits": {"max_attempts_per_node": 1}},
        evaluation={},
    )
    monkeypatch.setattr(
        runtime,
        "read_runtime",
        lambda client, target: {"actual_revision": config.versions[target]},
    )
    monkeypatch.setattr(LiveSession, "versions", lambda session: config.versions)
    inventory = Inventory.create(artifact_dir, "q-0123456789abcdef", config.environment)
    session = LiveSession(
        config, manifest, inventory, {IssueProvider.kind: IssueProvider(transport)}
    )
    session.client = transport
    plan = session.start()
    assert plan["product_id"] == config.product_epic.product_id
    assert [node["issue_ref"] for node in plan["nodes"] if node["kind"] == "story"] == [
        "10",
        "10",
    ]
    assert [
        node["repository_identity"] for node in plan["nodes"] if node["kind"] == "story"
    ] == [target.repository for target in config.product_epic.stories]
    calls = transport.calls
    assert (
        sum(method == "POST" and path.endswith("/sub_issues") for method, path in calls)
        == 2
    )
    assert max(
        index
        for index, (method, path) in enumerate(calls)
        if method == "POST" and path.endswith("/sub_issues")
    ) < next(
        index
        for index, (method, path) in enumerate(calls)
        if method == "POST" and path == "/orchestration/flows"
    )
    assert (
        inventory.get("story-1").observed_resource_id
        != inventory.get("story-2").observed_resource_id
    )
