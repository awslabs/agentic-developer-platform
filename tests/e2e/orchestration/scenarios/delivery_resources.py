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
        rows = self.client.pages(
            f"/repos/{self.client.config.repository}/pulls?state=all"
        )
        matches = [
            r
            for r in rows
            if r["head"]["ref"] == branch
            and (r["head"].get("repo") or {}).get("full_name")
            == self.client.config.repository
        ]
        if len(matches) != 1:
            raise Unsupported(
                "worker-created PR/branch absent or ambiguous; retain intent for reconciliation"
            )
        return self.descriptor(matches[0], qualification_id)

    @staticmethod
    def descriptor(pr, qualification_id):
        return json.dumps(
            {
                "qualification_id": qualification_id,
                "pr_number": pr["number"],
                "branch": pr["head"]["ref"],
                "head_sha": pr["head"]["sha"],
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
        pr = self.client.get(
            f"/repos/{self.client.config.repository}/pulls/{value['pr_number']}",
            github=True,
        )
        if (
            (pr["head"].get("repo") or {}).get("full_name")
            != self.client.config.repository
            or pr["base"]["repo"]["full_name"] != self.client.config.repository
            or pr["head"]["ref"] != value["branch"]
            or pr["head"]["sha"] != value["head_sha"]
        ):
            return None
        return self.client.config.ownership_tags(value["qualification_id"])

    def delete(self, resource_id):
        # GitHub's branch-delete API has no expected-SHA precondition. A stale
        # read must not delete a replacement branch; preserve the explicit record.
        raise Unsupported(
            "retain worker-created PR/branch audit; cleanup requires terminal worker evidence and provider-safe branch reconciliation"
        )


def plan_resources(session):
    if session.config.max_resources < 9:
        raise Unsupported(
            "Q2 needs at least 9 inventory slots: two issues, two PRs, two branches, two flows and one worker"
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
        if binding.get("repo") != session.config.repository:
            raise Unsupported("worker PR binding names a foreign repository")
        number = binding.get("pr_number") or binding.get("number")
        if type(number) is not int:
            raise Unsupported("worker PR binding is incomplete")
        pr = session.client.get(
            f"/repos/{session.config.repository}/pulls/{number}", github=True
        )
        if pr["head"]["ref"] != branch_name(session.inventory.qualification_id, index):
            raise Unsupported("worker created an off-plan branch")
        descriptor = DeliveryResourceProvider.descriptor(
            pr, session.inventory.qualification_id
        )
        for resource in ("pr", "branch"):
            provider = session.providers["qualification-" + resource]
            if provider.read_tags(descriptor) != session.config.ownership_tags(
                session.inventory.qualification_id
            ):
                raise Unsupported("worker-created resource ownership is unverifiable")
            session.inventory.mark_created(f"story-{index}-{resource}", descriptor)
