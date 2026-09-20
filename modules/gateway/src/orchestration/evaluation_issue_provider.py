"""Bounded, tenant-scoped GitHub issue correlation for evaluation corrections."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

from .deployment_workflow_provider import WorkflowProvider
from .review_cycle import CycleBlockedError


@dataclass(frozen=True)
class CorrectionIssue:
    number: int
    node_id: str
    repository_id: int
    state: str
    url: str
    correlation: str


def correlation(operation_key):
    return hashlib.sha256(operation_key.encode()).hexdigest()


def issue_content(*, operation_key, evaluation_id, cycle, failed_criteria, actual_revision, evidence_ref, source_issue):
    """Public issue content omits protected claim, connection and grant fields."""
    marker = correlation(operation_key)
    if (
        type(source_issue) is not int
        or source_issue < 1
        or type(cycle) is not int
        or cycle < 1
        or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", evaluation_id)
        or not failed_criteria
        or len(failed_criteria) > 128
        or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", item) for item in failed_criteria)
        or not re.fullmatch(r"[0-9a-f]{40}", actual_revision)
        or not re.fullmatch(r"github/actions/runs/[0-9]+/artifacts/[0-9]+", evidence_ref)
    ):
        raise CycleBlockedError("evaluation_correction_input_invalid")
    title = f"Evaluation correction {marker[:12]} (cycle {cycle})"
    body = (
        f"<!-- adp-evaluation-correction:v1:{marker} -->\n\n"
        f"Correct the required criteria observed failing for evaluation `{evaluation_id}`, cycle {cycle}.\n\n"
        f"Original accepted work: #{source_issue}.\n\n"
        + "\n".join(f"- `{item}`" for item in sorted(failed_criteria))
        + f"\n\nObserved release: `{actual_revision}`.\nEvidence: `{evidence_ref}`.\n\n"
        "Stay within the accepted evaluation and original implementation scope. "
        "Scope or dependency changes require an accepted amendment. Preserve passed predecessors. "
        "This correction must pass the existing review, merge and verified deployment controls "
        "before fresh evaluation evidence can be accepted. Issue closure is not acceptance.\n"
    )
    if len(body.encode()) > 32768:
        raise CycleBlockedError("evaluation_correction_input_limit")
    return dict(title=title, body=body, correlation=marker)


class EvaluationIssueProvider(WorkflowProvider):
    async def credentials(self, binding, *, write=False):
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", binding.repo)
            or any(part in {".", ".."} for part in binding.repo.split("/"))
            or type(binding.provider_repository_id) is not int
            or binding.provider_repository_id <= 0
        ):
            raise CycleBlockedError("evaluation_correction_repository_invalid")
        try:
            app_id, key = await resolve_tenant_app_credentials(binding.org_id)
            token, expires = await mint_installation_token_with_expiry(
                app_id,
                key,
                binding.installation_id,
                repositories=[binding.repo.split("/", 1)[1]],
                permissions={"issues": "write" if write else "read", "metadata": "read"},
            )
            deadline = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if deadline.tzinfo is None or deadline <= datetime.now(UTC):
                raise ValueError("expired")
            return token, int(app_id)
        except Exception:
            raise CycleBlockedError("evaluation_correction_scoped_credential_unavailable") from None

    async def repository(self, binding, token):
        data = (await self.request(binding, "GET", f"/repos/{binding.repo}", token=token)).json()
        if data.get("id") != binding.provider_repository_id or data.get("full_name") != binding.repo:
            raise CycleBlockedError("evaluation_correction_repository_changed")

    def validate(self, binding, data, content, app_id):
        number = data.get("number")
        if (
            type(number) is not int
            or number <= 0
            or not isinstance(data.get("node_id"), str)
            or not data["node_id"]
            or data.get("pull_request") is not None
            or (data.get("performed_via_github_app") or {}).get("id") != app_id
            or data.get("title") != content["title"]
            or data.get("body") != content["body"]
            or data.get("state") not in {"open", "closed"}
            or data.get("html_url") != f"https://github.com/{binding.repo}/issues/{number}"
        ):
            raise CycleBlockedError("evaluation_correction_issue_mismatch")
        return CorrectionIssue(number, data["node_id"], binding.provider_repository_id, data["state"], data["html_url"], content["correlation"])

    async def find(self, binding, content, *, since, issue_number=None):
        token, app_id = await self.credentials(binding)
        await self.repository(binding, token)
        if issue_number is not None:
            if type(issue_number) is not int or issue_number <= 0:
                raise CycleBlockedError("evaluation_correction_issue_number_invalid")
            data = (await self.request(binding, "GET", f"/repos/{binding.repo}/issues/{issue_number}", token=token)).json()
            return self.validate(binding, data, content, app_id)
        if since.tzinfo is None:
            raise CycleBlockedError("evaluation_correction_start_time_invalid")
        marker = f"<!-- adp-evaluation-correction:v1:{content['correlation']} -->"
        found = []
        for page in range(1, 6):
            rows = (
                await self.request(
                    binding,
                    "GET",
                    f"/repos/{binding.repo}/issues",
                    token=token,
                    params={"state": "all", "since": (since - timedelta(minutes=1)).isoformat(), "per_page": 100, "page": page},
                )
            ).json()
            if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
                raise CycleBlockedError("evaluation_correction_issue_listing_invalid")
            for row in rows:
                if marker in (row.get("body") or ""):
                    found.append(self.validate(binding, row, content, app_id))
            if len(found) > 1:
                raise CycleBlockedError("evaluation_correction_issue_ambiguous")
            if len(rows) < 100:
                return found[0] if found else None
        raise CycleBlockedError("evaluation_correction_issue_history_limit")

    async def create(self, binding, content, *, reauthorize):
        token, app_id = await self.credentials(binding, write=True)
        await self.repository(binding, token)
        # The controller persists creation_started in this callback. A lost
        # response must reconcile that correlation; absence never grants repost.
        await reauthorize()
        response = await self.request(
            binding, "POST", f"/repos/{binding.repo}/issues", token=token, json={"title": content["title"], "body": content["body"]}
        )
        return self.validate(binding, response.json(), content, app_id)
