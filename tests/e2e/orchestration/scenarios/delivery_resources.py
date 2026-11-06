"""Write-ahead inventory for PRs and branches the accepted workers will create."""

import json
import re

from .http import Unsupported


def branch_name(qualification_id, index):
    return f"qualification/{qualification_id}/story-{index}"


class DeliveryResourceProvider:
    def __init__(self, client, kind):
        if kind not in {"qualification-pr", "qualification-branch"}:
            raise ValueError("invalid delivery resource kind")
        self.client, self.kind = client, kind

    def create(self, **kwargs):
        raise Unsupported(
            "delivery resources are created only by accepted workers; reconcile the planned intent"
        )

    def find(self, *, intended_identity, idempotency_token):
        qualification_id, resource = intended_identity.split("/", 1)
        match = re.fullmatch(r"story-([12])-(pr|branch)", resource)
        if not match or not re.fullmatch(r"q-[a-z0-9-]{8,50}", qualification_id):
            raise ValueError("invalid delivery resource intent")
        branch = branch_name(qualification_id, match[1])
        repository = self.client.config.story_repository(int(match[1]))
        rows = self.client.pages(f"/repos/{repository}/pulls?state=all")
        matches = [
            r
            for r in rows
            if r["head"]["ref"] == branch
            and (r["head"].get("repo") or {}).get("full_name") == repository
        ]
        if len(matches) != 1:
            raise Unsupported(
                "worker-created PR/branch absent or ambiguous; retain intent for reconciliation"
            )
        from .cleanup import find_flow

        flow_id = find_flow(self.client, qualification_id)
        return self.descriptor(matches[0], qualification_id, flow_id)

    @staticmethod
    def descriptor(pr, qualification_id, flow_id=None):
        return json.dumps(
            {
                "qualification_id": qualification_id,
                "pr_number": pr["number"],
                "branch": pr["head"]["ref"],
                "repository": pr["base"]["repo"]["full_name"],
                "head_sha": pr["head"]["sha"],
                "repository_id": pr["base"]["repo"].get("id"),
                "flow_id": flow_id,
            },
            sort_keys=True,
        )

    def read_tags(self, resource_id):
        value = json.loads(resource_id)
        if not re.fullmatch(r"q-[a-z0-9-]{8,50}", value["qualification_id"]):
            return None
        if (
            value["branch"]
            not in {branch_name(value["qualification_id"], i) for i in (1, 2)}
            or type(value["pr_number"]) is not int
        ):
            return None
        index = int(value["branch"].rsplit("-", 1)[-1])
        repository = self.client.config.story_repository(index)
        if (
            value.get(
                "repository",
                repository if self.client.config.product_epic is None else None,
            )
            != repository
        ):
            return None
        pr = self.client.get(
            f"/repos/{repository}/pulls/{value['pr_number']}",
            github=True,
        )
        if (
            (pr["head"].get("repo") or {}).get("full_name") != repository
            or pr["base"]["repo"]["full_name"] != repository
            or pr["head"]["ref"] != value["branch"]
            or (
                self.client.config.product_epic is not None
                and (
                    value.get("repository_id")
                    != self.client.config.product_epic.stories[index - 1].repository_id
                    or pr["base"]["repo"].get("id") != value["repository_id"]
                    or (pr["head"].get("repo") or {}).get("id")
                    != value["repository_id"]
                )
            )
            or pr["head"]["sha"] != value["head_sha"]
        ):
            return None
        return self.client.config.ownership_tags(value["qualification_id"])

    def delete(self, resource_id):
        from tests.e2e.orchestration.fixtures import RetainedAudit
        from .cleanup import terminal_flow, delete_branch

        value = json.loads(resource_id)
        if not value.get("flow_id"):
            raise Unsupported("cleanup requires flow lineage reconciliation")
        graph, _ = terminal_flow(
            self.client, value["flow_id"], value["qualification_id"]
        )
        if not any(
            (binding := n.get("bound_pull_request") or {}).get("pr_number")
            == value["pr_number"]
            and (
                self.client.config.product_epic is None
                or binding.get("repo") == value.get("repository")
            )
            for n in graph["nodes"]
        ):
            raise Unsupported("PR is not bound to the owned terminal flow")
        if self.read_tags(resource_id) is None:
            raise Unsupported("delivery resource changed before cleanup")
        path = f"/repos/{value.get('repository', self.client.config.repository)}/pulls/{value['pr_number']}"
        pr = self.client.get(path, github=True)
        if self.kind == "qualification-pr":
            if pr["state"] != "closed":
                status, _ = self.client.request(
                    "PATCH", path, github=True, body={"state": "closed"}
                )
                if (
                    status != 200
                    or self.client.get(path, github=True)["state"] != "closed"
                ):
                    raise Unsupported("PR close was not verified")
            return RetainedAudit("closed pull request and review history retained")
        if pr["state"] != "closed":
            raise Unsupported("close the PR before branch cleanup")
        delete_branch(
            self.client,
            value["branch"],
            value["head_sha"],
            repository=value.get("repository", self.client.config.repository),
        )


def plan_resources(session):
    if session.config.max_resources < 23:
        raise Unsupported(
            "Q2 needs at least 23 inventory slots: delivery resources, worker, namespace, network policy, deployment and its bounded ReplicaSet/Pod"
        )
    for index in (1, 2):
        for resource in ("pr", "branch"):
            fixture_id = f"story-{index}-{resource}"
            session.inventory.record_planned(
                fixture_id=fixture_id,
                kind="qualification-" + resource,
                intended_identity=session.inventory.qualification_id + "/" + fixture_id,
                ownership_tags=session.config.ownership_tags(
                    session.inventory.qualification_id
                ),
                idempotency_token=session.inventory.qualification_id + "-" + fixture_id,
            )


def record_resources(session, data):
    for index, ref in enumerate(("first", "second"), 1):
        node = next(n for n in data["graph"]["nodes"] if n["node_ref"] == ref)
        binding = node.get("bound_pull_request") or {}
        if not binding:
            continue
        repository = session.config.story_repository(index)
        if binding.get("repo") != repository:
            raise Unsupported("worker PR binding names a foreign repository")
        number = binding.get("pr_number") or binding.get("number")
        if type(number) is not int:
            raise Unsupported("worker PR binding is incomplete")
        pr = session.client.get(f"/repos/{repository}/pulls/{number}", github=True)
        if pr["head"]["ref"] != branch_name(session.inventory.qualification_id, index):
            raise Unsupported("worker created an off-plan branch")
        descriptor = DeliveryResourceProvider.descriptor(
            pr, session.inventory.qualification_id, session.flow_id
        )
        for resource in ("pr", "branch"):
            provider = session.providers["qualification-" + resource]
            if provider.read_tags(descriptor) != session.config.ownership_tags(
                session.inventory.qualification_id
            ):
                raise Unsupported("worker-created resource ownership is unverifiable")
            session.inventory.mark_created(f"story-{index}-{resource}", descriptor)
