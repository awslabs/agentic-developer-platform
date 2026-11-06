"""Real GitHub issue fixtures using Q1 write-ahead ownership and reconciliation."""

import json
import re

from .definitions import fixture_story, fixture_test, fixture_test_path
from .http import Unsupported


MARKER = "<!-- adp-qualification:"


def ensure_native_parent(inventory, config, provider, record):
    allow_create = record.native_link_attempts < config.max_runs
    if allow_create:
        inventory.mark_native_link_attempt(record.fixture_id)
    try:
        provider.link_parent(
            record.observed_resource_id,
            record.ownership_tags,
            allow_create=allow_create,
        )
    except Exception:
        inventory.mark_reconcile_failed(
            record.fixture_id,
            "native parent not verified; reconcile before retry"
            if allow_create
            else "native parent retry bound reached; manual reconciliation required",
        )
        raise
    inventory.mark_native_parent_verified(record.fixture_id)


class IssueProvider:
    kind = "qualification-issue"

    def __init__(self, client):
        self.client = client
        self.path = "/repos/" + client.config.repository

    def story_target(self, intended_identity):
        product_epic = self.client.config.product_epic
        if product_epic is None or self.kind == "qualification-stop-issue":
            return None
        story = intended_identity.split("/", 1)[1]
        if story not in {"story-1", "story-2"}:
            raise ValueError("invalid Product-bound story intent")
        return product_epic.stories[int(story[-1]) - 1]

    def issue_path(self, resource_id):
        product_epic = self.client.config.product_epic
        if product_epic is None or self.kind == "qualification-stop-issue":
            if not resource_id.isdigit():
                raise ValueError("invalid fixture issue id")
            return self.path + "/issues/" + resource_id
        repository, separator, number = resource_id.partition("#")
        if (
            not separator
            or not number.isdigit()
            or repository not in {target.repository for target in product_epic.stories}
        ):
            raise ValueError("invalid qualified fixture issue id")
        return f"/repos/{repository}/issues/{number}"

    def link_parent(self, resource_id, ownership_tags, *, allow_create=True):
        product_epic = self.client.config.product_epic
        if product_epic is None:
            return
        if self.read_tags(resource_id) != ownership_tags:
            raise Unsupported("story ownership differs before native linking")
        path = self.issue_path(resource_id)
        child = self.client.get(path, github=True)
        parent_path = f"/repos/{product_epic.epic.repository}/issues/{product_epic.epic_issue_number}"
        repository = self.client.get(
            "/repos/" + product_epic.epic.repository, github=True
        )
        parent = self.client.get(parent_path, github=True)
        if (
            repository.get("id") != product_epic.epic.repository_id
            or parent.get("number") != product_epic.epic_issue_number
            or type(parent.get("id")) is not int
            or type(child.get("id")) is not int
        ):
            raise Unsupported(
                "planning epic or story provider identity differs from reviewed config"
            )
        status, current = self.client.request("GET", path + "/parent", github=True)
        if status == 200:
            if current.get("id") != parent["id"]:
                raise Unsupported(
                    "story already has a different native parent; reparenting refused"
                )
            return
        if status != 404:
            raise Unsupported("native parentage could not be verified")
        if not allow_create:
            raise Unsupported(
                "native parent retry bound reached; manual reconciliation required"
            )
        status, _ = self.client.request(
            "POST",
            parent_path + "/sub_issues",
            github=True,
            body={"sub_issue_id": child["id"]},
        )
        if status not in {200, 201}:
            raise Unsupported(
                "native link not acknowledged; reconcile parent before retry"
            )
        status, linked = self.client.request("GET", path + "/parent", github=True)
        if status != 200 or linked.get("id") != parent["id"]:
            raise Unsupported(
                "native parentage not confirmed; retain issue for reconciliation"
            )

    def create(self, *, intended_identity, ownership_tags, idempotency_token):
        qualification_id, story = intended_identity.split("/")
        index = int(story.removeprefix("story-")) - 1
        if (
            index not in {0, 1}
            or qualification_id != ownership_tags["adp:qualification-id"]
        ):
            raise ValueError("invalid real-code story fixture")
        metadata = dict(
            identity=intended_identity,
            ownership_tags=ownership_tags,
            correlation=idempotency_token,
        )
        target = self.story_target(intended_identity)
        path = self.path if target is None else "/repos/" + target.repository
        if target is not None:
            repository = self.client.get(path, github=True)
            if (
                repository.get("id") != target.repository_id
                or repository.get("full_name") != target.repository
            ):
                raise Unsupported(
                    "story target repository identity differs from reviewed config"
                )
            metadata.update(
                product_id=self.client.config.product_epic.product_id,
                repository_id=target.repository_id,
                parent_repository_id=self.client.config.product_epic.epic.repository_id,
                parent_issue_number=self.client.config.product_epic.epic_issue_number,
            )
        actor = self.client.get("/user", github=True)
        metadata["creator_id"] = actor["id"]
        product = target is not None
        test_path = fixture_test_path(index, qualification_id, product=product)
        body = (
            fixture_story(index, qualification_id, product=product)
            + f"\n\nUse exactly the branch qualification/{qualification_id}/story-{index + 1}. "
            "Its PR must stay in the accepted repository; the harness inventories both before dispatch."
            + f"\n\nPinned {test_path} "
            + (
                "(run python -m unittest discover -s tests -v from this repository root; "
                if product
                else "(run pytest from modules/gateway; "
            )
            + "these tests must be collected by required CI):\n```python\n"
            + fixture_test(index, qualification_id, product=product)
            + "```\n\n"
            + MARKER
            + json.dumps(metadata, sort_keys=True)
            + " -->"
        )
        status, issue = self.client.request(
            "POST",
            path + "/issues",
            github=True,
            body={
                "title": f"[{qualification_id}] Delivery qualification story {index + 1}",
                "body": body,
            },
        )
        if status != 201:
            raise Unsupported(
                f"issue creation returned HTTP {status}; reconcile before retry"
            )
        if issue["user"]["id"] != actor["id"]:
            raise Unsupported(
                "created issue author differs from the scoped GitHub actor"
            )
        if target is not None and (
            type(issue.get("id")) is not int or type(issue.get("number")) is not int
        ):
            raise Unsupported("story issue lacks immutable provider identity")
        return (
            str(issue["number"])
            if target is None
            else f"{target.repository}#{issue['number']}"
        )

    def find(self, *, intended_identity, idempotency_token):
        matches = []
        target = self.story_target(intended_identity)
        for issue in self.client.pages(
            (self.path if target is None else "/repos/" + target.repository)
            + "/issues?state=all"
        ):
            metadata = self.metadata(issue)
            if metadata and (metadata["identity"], metadata["correlation"]) == (
                intended_identity,
                idempotency_token,
            ):
                matches.append(
                    str(issue["number"])
                    if target is None
                    else f"{target.repository}#{issue['number']}"
                )
        if len(matches) > 1:
            raise Unsupported(
                "duplicate issue correlation; manual reconciliation required"
            )
        return matches[0] if matches else None

    @staticmethod
    def metadata(issue):
        matches = re.findall(re.escape(MARKER) + r"(.+?) -->", issue.get("body") or "")
        if len(matches) != 1 or "pull_request" in issue:
            return None
        try:
            value = json.loads(matches[0])
            if (
                not isinstance(value, dict)
                or value.get("creator_id") != issue.get("user", {}).get("id")
                or not isinstance(value.get("identity"), str)
                or not isinstance(value.get("correlation"), str)
                or not isinstance(value.get("ownership_tags"), dict)
            ):
                return None
            return value
        except ValueError:
            return None

    def read_tags(self, resource_id):
        path = self.issue_path(resource_id)
        issue = self.client.get(path, github=True)
        metadata = self.metadata(issue)
        if (
            self.client.config.scenarios == ("legacy-attribution",)
            and metadata
            and metadata.get("product_id") is not None
        ):
            return None
        if (
            self.client.config.product_epic is not None
            and self.kind != "qualification-stop-issue"
        ):
            target = next(
                target
                for target in self.client.config.product_epic.stories
                if path.startswith("/repos/" + target.repository + "/issues/")
            )
            if (
                not metadata
                or metadata.get("product_id")
                != self.client.config.product_epic.product_id
                or metadata.get("repository_id") != target.repository_id
                or type(issue.get("id")) is not int
                or self.client.get("/repos/" + target.repository, github=True).get("id")
                != target.repository_id
            ):
                return None
        return metadata.get("ownership_tags") if metadata else None

    def delete(self, resource_id):
        from tests.e2e.orchestration.fixtures import RetainedAudit
        from .cleanup import find_flow, terminal_flow

        path = self.issue_path(resource_id)
        issue = self.client.get(path, github=True)
        metadata = self.metadata(issue)
        if not metadata or self.read_tags(resource_id) is None:
            raise Unsupported("issue ownership metadata unavailable")
        qualification_id = metadata["ownership_tags"]["adp:qualification-id"]
        suffix = "-stop" if self.kind == "qualification-stop-issue" else ""
        flow_id = find_flow(self.client, qualification_id, suffix, allow_absent=True)
        if flow_id:
            terminal_flow(self.client, flow_id, qualification_id)
        status, _ = self.client.request(
            "PATCH", path, github=True, body={"state": "closed"}
        )
        if status != 200 or self.client.get(path, github=True)["state"] != "closed":
            raise Unsupported("fixture issue closure was not verified")
        return RetainedAudit("closed issue and its provider history retained")
