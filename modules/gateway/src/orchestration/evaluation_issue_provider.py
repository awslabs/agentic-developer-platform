"""Bounded, tenant-scoped GitHub issue correlation for evaluation corrections."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

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
    async def read_native_parent(self, binding, parent_binding, issue):
        token, _ = await self.credentials(binding, related_binding=parent_binding)
        await self.repository(binding, token)
        await self.repository(parent_binding, token)
        try:
            response = await self.request(binding, "GET", f"/repos/{binding.repo}/issues/{issue.number}/parent", token=token)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise CycleBlockedError("evaluation_correction_parent_unavailable") from None
            response = None
        except httpx.HTTPError:
            raise CycleBlockedError("evaluation_correction_parent_unavailable") from None

        if response is not None:
            parent = response.json()
            if not isinstance(parent, dict) or type(parent.get("id")) is not int or not isinstance(parent.get("repository_url"), str):
                raise CycleBlockedError("evaluation_correction_parent_unverifiable")
            if (
                parent["id"] != parent_binding.issue_id
                or parent["repository_url"].casefold() != f"https://api.github.com/repos/{parent_binding.repo}".casefold()
            ):
                raise CycleBlockedError("evaluation_correction_parent_conflict")
        return response is not None

    async def confirm_native_parent(self, binding, parent_binding, issue, content, *, reauthorize):
        if await self.read_native_parent(binding, parent_binding, issue):
            return
        token, app_id = await self.credentials(binding, related_binding=parent_binding)
        await self.repository(binding, token)
        await self.repository(parent_binding, token)
        try:
            child = (await self.request(binding, "GET", f"/repos/{binding.repo}/issues/{issue.number}", token=token)).json()
            await self.validate(binding, child, content, app_id, token=token)
            if child.get("node_id") != issue.node_id or type(child.get("id")) is not int or child["id"] <= 0:
                raise CycleBlockedError("evaluation_correction_issue_identity_missing")
            token, _ = await self.credentials(parent_binding, write=True, related_binding=binding)
            await self.repository(binding, token)
            await self.repository(parent_binding, token)
            await reauthorize()
            await self.request(
                parent_binding,
                "POST",
                f"/repos/{parent_binding.repo}/issues/{parent_binding.issue_number}/sub_issues",
                token=token,
                json={"sub_issue_id": child["id"]},
            )
        except httpx.HTTPError:
            raise CycleBlockedError("evaluation_correction_link_unconfirmed") from None
        if not await self.read_native_parent(binding, parent_binding, issue):
            raise CycleBlockedError("evaluation_correction_link_unconfirmed")

    async def credentials(self, binding, *, write=False, related_binding=None):
        targets = (binding,) if related_binding is None else (binding, related_binding)
        if any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target.repo)
            or any(part in {".", ".."} for part in target.repo.split("/"))
            or type(target.provider_repository_id) is not int
            or target.provider_repository_id <= 0
            for target in targets
        ):
            raise CycleBlockedError("evaluation_correction_repository_invalid")
        if related_binding is not None and (
            related_binding.org_id != binding.org_id or related_binding.repo.split("/")[0].casefold() != binding.repo.split("/")[0].casefold()
        ):
            raise CycleBlockedError("evaluation_correction_repository_scope_mismatch")
        try:
            app_id, key = await resolve_tenant_app_credentials(binding.org_id)
            token, expires = await mint_installation_token_with_expiry(
                app_id,
                key,
                binding.installation_id,
                repositories=sorted({target.repo.split("/", 1)[1] for target in targets}),
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

    async def app_owns_issue(self, binding, data, app_id, *, token):
        """GitHub may omit the app receipt for installation-token issue writes.

        In that case verify the provider's bot author against both the registered
        app ID and its numeric user ID. A marker or a bot-looking name alone is
        never ownership, and an explicit conflicting receipt cannot fall back.
        """
        if not isinstance(data, dict):
            return False
        receipt = data.get("performed_via_github_app")
        if receipt is not None:
            return isinstance(receipt, dict) and type(receipt.get("id")) is int and receipt["id"] == app_id
        author = data.get("user")
        if not isinstance(author, dict) or author.get("type") != "Bot" or type(author.get("id")) is not int or author["id"] <= 0:
            return False
        login = author.get("login")
        match = re.fullmatch(r"([A-Za-z0-9-]{1,100})\[bot\]", login) if isinstance(login, str) else None
        if match is None:
            return False
        slug = match[1]
        app = (await self.request(binding, "GET", f"/apps/{slug}", token=token)).json()
        if (
            not isinstance(app, dict)
            or type(app.get("id")) is not int
            or app["id"] != app_id
            or not isinstance(app.get("slug"), str)
            or app["slug"].casefold() != slug.casefold()
        ):
            return False
        bot = (await self.request(binding, "GET", f"/users/{login}", token=token)).json()
        return (
            isinstance(bot, dict)
            and bot.get("type") == "Bot"
            and type(bot.get("id")) is int
            and bot["id"] == author["id"]
            and isinstance(bot.get("login"), str)
            and bot["login"].casefold() == login.casefold()
        )

    async def validate(self, binding, data, content, app_id, *, token):
        number = data.get("number")
        if (
            type(number) is not int
            or number <= 0
            or not isinstance(data.get("node_id"), str)
            or not data["node_id"]
            or data.get("pull_request") is not None
            or not await self.app_owns_issue(binding, data, app_id, token=token)
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
            return await self.validate(binding, data, content, app_id, token=token)
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
                    found.append(await self.validate(binding, row, content, app_id, token=token))
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
        return await self.validate(binding, response.json(), content, app_id, token=token)
