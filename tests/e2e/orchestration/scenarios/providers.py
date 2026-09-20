"""Real GitHub issue fixtures using Q1 write-ahead ownership and reconciliation."""

import json
import re

from .definitions import STORIES, TESTS
from .http import Unsupported


MARKER = "<!-- adp-qualification:"


class IssueProvider:
    kind = "qualification-issue"

    def __init__(self, client):
        self.client = client
        self.path = "/repos/" + client.config.repository

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
        test_file = "test_pricing.py" if index == 0 else "test_quote.py"
        body = (
            STORIES[index].format(qualification_id=qualification_id)
            + f"\n\nPinned {test_file} (run with unittest discovery in this fixture directory):\n```python\n"
            + TESTS[index]
            + "```\n\n"
            + MARKER
            + json.dumps(metadata, sort_keys=True)
            + " -->"
        )
        status, issue = self.client.request(
            "POST",
            self.path + "/issues",
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
        return str(issue["number"])

    def find(self, *, intended_identity, idempotency_token):
        matches = []
        for issue in self.client.pages(self.path + "/issues?state=all"):
            metadata = self.metadata(issue)
            if metadata and (metadata["identity"], metadata["correlation"]) == (
                intended_identity,
                idempotency_token,
            ):
                matches.append(str(issue["number"]))
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
            return json.loads(matches[0])
        except ValueError:
            return None

    def read_tags(self, resource_id):
        if not resource_id.isdigit():
            raise ValueError("invalid fixture issue id")
        issue = self.client.get(self.path + "/issues/" + resource_id, github=True)
        metadata = self.metadata(issue)
        return metadata.get("ownership_tags") if metadata else None

    def delete(self, resource_id):
        # GitHub issues are closed, not erased. The immutable receipt stays in
        # inventory/evidence and cleanup documents this provider's semantics.
        status, _ = self.client.request(
            "PATCH",
            self.path + "/issues/" + resource_id,
            github=True,
            body={"state": "closed"},
        )
        if status != 200:
            raise Unsupported(f"fixture issue close returned HTTP {status}")
