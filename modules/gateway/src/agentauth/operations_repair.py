"""Repair checks for the existing mediated PR path; GitHub remains merge authority."""

import base64
import binascii
import json
import re
from urllib.parse import quote

import yaml

from src.orchestration.execution_policy import Action

from .actions_logs import redact_text
from .github_operations import OperationRefusedError, RepairScopeMissingError, escalating_paths
from .github_provider import ProviderBranchRulesUnavailableError, ProviderConflictError


async def qualify_repair_readiness(provider, default_branch):
    branch = quote(default_branch, safe="")
    repo = await provider._call("GET", f"/repos/{provider.repo}")
    if not isinstance(repo, dict) or repo.get("id") != provider.assignment.repository_id or repo.get("default_branch") != default_branch:
        raise OperationRefusedError("repair repository or default branch changed")
    info = await provider._call("GET", f"/repos/{provider.repo}/branches/{branch}")
    if not isinstance(info, dict) or info.get("name") != default_branch or type(info.get("protected")) is not bool:
        raise OperationRefusedError("repair branch protection is unavailable")
    try:
        rules = await provider._call("GET", f"/repos/{provider.repo}/rules/branches/{branch}?per_page=100")
    except ProviderBranchRulesUnavailableError:
        # A generic permission failure never establishes absence. This exact
        # provider feature response is usable only with an explicitly private,
        # unprotected branch; conflicting/unknown metadata still refuses repair.
        if repo.get("private") is not True or info["protected"]:
            raise OperationRefusedError("repair branch rules are not qualified") from None
        rules = []
    if not isinstance(rules, list) or len(rules) >= 100:
        raise OperationRefusedError("repair branch rules are unavailable")
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("type"), str):
            raise OperationRefusedError("repair branch rules are unavailable")
        if rule["type"] == "pull_request":
            parameters = rule.get("parameters")
            if not isinstance(parameters, dict):
                raise OperationRefusedError("repair review requirement is unavailable")
            count = parameters.get("required_approving_review_count")
            code_owners = parameters.get("require_code_owner_review")
            last_push = parameters.get("require_last_push_approval", False)
            threads = parameters.get("required_review_thread_resolution", False)
            if type(count) is not int or count < 0 or type(code_owners) is not bool or type(last_push) is not bool or type(threads) is not bool:
                raise OperationRefusedError("repair review requirement is unavailable")
            if count or code_owners or last_push or threads:
                raise OperationRefusedError("repair requires external approval")
        elif rule["type"] not in {
            "creation",
            "update",
            "deletion",
            "required_linear_history",
            "required_signatures",
            "required_status_checks",
            "non_fast_forward",
            "commit_message_pattern",
            "commit_author_email_pattern",
            "committer_email_pattern",
            "branch_name_pattern",
            "file_path_restriction",
            "file_extension_restriction",
            "max_file_size",
            "required_workflows",
            "required_deployments",
        }:
            raise OperationRefusedError("repair branch rule is not qualified")
    if info["protected"]:
        protection = await provider._call("GET", f"/repos/{provider.repo}/branches/{branch}/protection")
        if not isinstance(protection, dict) or "required_pull_request_reviews" not in protection:
            raise OperationRefusedError("repair review requirement is unavailable")
        reviews = protection["required_pull_request_reviews"]
        if reviews is not None:
            if not isinstance(reviews, dict):
                raise OperationRefusedError("repair review requirement is unavailable")
            count = reviews.get("required_approving_review_count")
            code_owners = reviews.get("require_code_owner_reviews")
            last_push = reviews.get("require_last_push_approval", False)
            if type(count) is not int or count < 0 or type(code_owners) is not bool or type(last_push) is not bool:
                raise OperationRefusedError("repair review requirement is unavailable")
            if count or code_owners or last_push:
                raise OperationRefusedError("repair requires external approval")


async def ensure_repair_readiness(authorized, reauthorize):
    from sqlalchemy import select

    from src.orchestration.models import OrchestrationDecision, OrchestrationNode
    from src.shared.database import get_session_factory

    from .github_operation_service import installation_token
    from .github_provider import GitHubProvider

    assignment = authorized.assignment
    scope = {
        "plan_version": assignment.accepted_plan_version,
        "policy_hash": authorized.policy.policy_hash,
        "repository_id": assignment.repository_id,
        "repository": assignment.repository,
        "default_branch": assignment.default_branch,
    }
    async with get_session_factory()() as session:
        node = await session.get(OrchestrationNode, assignment.node_id)
        if node is None or node.org_id != assignment.tenant_id:
            raise OperationRefusedError("operations node is unavailable")
        previous = await session.scalar(
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.org_id == assignment.tenant_id,
                OrchestrationDecision.node_id == assignment.node_id,
                OrchestrationDecision.kind == "operations_readiness",
            )
            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
            .limit(1)
        )
        if (
            previous is not None
            and previous.actor_id == "system:operations-readiness"
            and previous.actor_kind == "service"
            and previous.flow_id == node.flow_id
            and previous.reason == json.dumps(scope, sort_keys=True)
        ):
            return
        token = await installation_token(
            org_id=assignment.tenant_id,
            installation_id=assignment.installation_id,
            repository=assignment.repository,
            permissions={"metadata": "read", "administration": "read", "contents": "read"},
        )
        async with GitHubProvider(token=token, assignment=assignment) as provider:
            await qualify_repair_readiness(provider, assignment.default_branch)
        if await reauthorize() != authorized:
            raise OperationRefusedError("repair authority changed during readiness qualification")
        session.add(
            OrchestrationDecision(
                org_id=assignment.tenant_id,
                flow_id=node.flow_id,
                node_id=assignment.node_id,
                kind="operations_readiness",
                actor_id="system:operations-readiness",
                actor_role="engine",
                actor_kind="service",
                reason=json.dumps(scope, sort_keys=True),
            )
        )
        await session.commit()


def check_paths(paths, authorized):
    scope = set(getattr(authorized.policy, "repair_paths", ()))
    repository_scope = getattr(authorized.policy, "repair_scope", None) == "repository"
    if not (scope or repository_scope):
        raise RepairScopeMissingError("repair paths are not accepted")
    if not paths:
        raise OperationRefusedError("repair paths are not accepted")
    for path in paths:
        if (
            not isinstance(path, str)
            or not 0 < len(path) <= 1024
            or not repository_scope
            and path not in scope
            or "\\" in path
            or ":" in path
            or any(ord(character) < 32 for character in path)
        ):
            raise OperationRefusedError("repair path is outside the accepted scope")
        parts = path.lower().split("/")
        if (
            any(part in {"", ".", "..", ".git", ".adp"} for part in parts)
            or path.startswith(".github/")
            and not path.startswith(".github/workflows/")
            or any(term in parts[-1] for term in ("credential", "secret", "approval", "policy", "token"))
        ):
            raise OperationRefusedError("repair cannot edit approval or credential policy")
    offending = escalating_paths(paths, policy=authorized.policy)
    # OPERATE already authorizes these named deployment workflows. It does not
    # authorize arbitrary automation or remove a human MERGE gate.
    if offending and not (
        authorized.policy.permits(Action.OPERATE)
        and authorized.policy.permits(Action.MERGE)
        and all(
            path.startswith(".github/workflows/") and path.removeprefix(".github/workflows/") in authorized.workflow_allowlist for path in offending
        )
    ):
        raise OperationRefusedError("repair content requires accepted workflow/merge authority")
    for path in paths:
        if path.startswith(".github/workflows/") and path.removeprefix(".github/workflows/") not in authorized.workflow_allowlist:
            raise OperationRefusedError("repair workflow is outside the accepted allowlist")


async def repair_status(provider, pull_number, *, expected_head=None, required_checks=()):
    state = await provider.read_repository()
    pull = await provider._call("GET", f"/repos/{provider.repo}/pulls/{pull_number}")
    provider._require_assigned_pull_request(pull)
    head = pull["head"]["sha"]
    if expected_head is not None and head != expected_head:
        raise ProviderConflictError("repair PR head changed")
    result = {
        "pull_number": pull_number,
        "head": head,
        "base": pull["base"]["sha"],
        "base_head": state["default_branch_head"],
        "merged": pull.get("merged") is True,
        "merge_sha": pull.get("merge_commit_sha"),
        "ready": False,
        "checks": [],
    }
    # Page every check, never infer success from a truncated first page.
    checks = []
    for page in range(1, 11):
        data = await provider._call("GET", f"/repos/{provider.repo}/commits/{head}/check-runs?per_page=100&page={page}&filter=latest")
        runs = data.get("check_runs")
        if not isinstance(runs, list):
            raise OperationRefusedError("repair checks unavailable")
        checks.extend(runs)
        if len(checks) >= data.get("total_count", 1001):
            break
    else:
        raise OperationRefusedError("repair checks exceed observation bound")
    status = await provider._call("GET", f"/repos/{provider.repo}/commits/{head}/status")
    result["checks"] = [{"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")} for r in checks]
    result["status"] = status.get("state")
    # Legacy commit statuses can be the only pending merge dependency. Expose
    # bounded context names so the worker can wait for the actual prerequisite.
    # These are diagnostic only; the aggregate state still gates readiness.
    contexts = status.get("statuses", [])
    result["status_contexts"] = (
        [
            {"context": redact_text(str(item.get("context") or ""), max_bytes=256), "state": item.get("state")}
            for item in contexts[:100]
            if isinstance(item, dict)
        ]
        if isinstance(contexts, list)
        else []
    )
    required = set(required_checks)
    successful = {
        run.get("name") for run in checks if run.get("head_sha") == head and run.get("status") == "completed" and run.get("conclusion") == "success"
    }
    result["checks_verified"] = bool(
        required
        and required <= successful
        and (checks or status.get("total_count", 0) > 0)
        and all(
            r.get("head_sha") == head and r.get("status") == "completed" and r.get("conclusion") in {"success", "neutral", "skipped"} for r in checks
        )
        and (status.get("total_count") == 0 or status.get("state") == "success")
    )
    result["ready"] = bool(
        pull.get("state") == "open"
        and pull.get("draft") is False
        and pull.get("mergeable") is True
        and pull.get("mergeable_state") == "clean"
        and head == state["branch_head"]
        and result["checks_verified"]
    )
    # Give the agent the actual prerequisite. The old combined reason made
    # pending commit statuses look like branch drift, causing needless PR
    # republishes followed by deployment of the still-unmerged repair.
    blockers = []
    if not required:
        blockers.append({"code": "required_checks_unconfigured"})
    for name in sorted(required - {run.get("name") for run in checks}):
        blockers.append({"code": "required_check_missing", "name": redact_text(str(name), max_bytes=256)})
    for check in checks:
        if check.get("head_sha") != head:
            code = "check_head_changed"
        elif check.get("status") != "completed":
            code = "check_pending"
        elif check.get("conclusion") not in {"success", "neutral", "skipped"} or (
            check.get("name") in required and check.get("conclusion") != "success"
        ):
            code = "check_failed"
        else:
            continue
        blockers.append({"code": code, "name": redact_text(str(check.get("name") or ""), max_bytes=256)})
    if status.get("total_count", 0) and status.get("state") != "success":
        blockers.append({"code": "commit_status_pending" if status.get("state") == "pending" else "commit_status_failed"})
    result["mergeable_state"] = pull.get("mergeable_state")
    if pull.get("merged") is not True:
        if pull.get("mergeable") is not True or pull.get("mergeable_state") != "clean":
            blockers.append({"code": "branch_not_mergeable", "state": pull.get("mergeable_state")})
        if head != state["branch_head"]:
            blockers.append({"code": "branch_changed"})
        if pull.get("draft") is not False or pull.get("state") != "open":
            blockers.append({"code": "pull_request_not_open_for_merge"})
    result["blockers"] = blockers[:100]
    if result["checks_verified"] and head == state["branch_head"] and blockers == [{"code": "branch_not_mergeable", "state": "behind"}]:
        result["next_action"] = (
            "GitHub requires this branch to be updated. Call repair_merge with this observed head to request a checked branch refresh. "
            "The tool revalidates repair authority, paths and ancestry before updating. "
            "Then observe the new head and wait for its checks before merging. Waiting alone will not refresh this branch."
        )
    return result


async def repair_file_entries(provider, pull_number):
    entries = []
    for page in range(1, 7):
        files = await provider._call("GET", f"/repos/{provider.repo}/pulls/{pull_number}/files?per_page=100&page={page}")
        if not isinstance(files, list):
            raise OperationRefusedError("repair diff unavailable")
        for entry in files:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("filename"), str)
                or "previous_filename" in entry
                and not isinstance(entry["previous_filename"], str)
            ):
                raise OperationRefusedError("repair diff unavailable")
            entries.append(entry)
        if len(files) < 100:
            break
    else:
        raise OperationRefusedError("repair diff exceeds bound")
    return entries


def file_paths(entries):
    return tuple(sorted(path for entry in entries for path in (entry["filename"], entry.get("previous_filename")) if path is not None))


async def repair_paths(provider, pull_number):
    return file_paths(await repair_file_entries(provider, pull_number))


async def qualified_merge_paths(provider, pull_number, authorized, *, head):
    """Only merge/refresh may inherit a verified progress board beside source.

    Keep the complete diff for race checks and workflow validation. The edit and
    publication paths still call the unchanged check_paths and forbid all .adp.
    """
    from .operations_progress_board import assigned_story_issue, verify_progress_board

    entries = await repair_file_entries(provider, pull_number)
    paths = file_paths(entries)
    if not paths:
        raise OperationRefusedError("repair diff is empty")
    protected = [entry for entry in entries if any(".adp" in path.lower().split("/") for path in file_paths([entry]))]
    if protected:
        issue = await assigned_story_issue(provider, authorized)
        if len(protected) != 1:
            raise OperationRefusedError("repair may only inherit one assigned story progress board")
        await verify_progress_board(provider, protected[0], head=head, issue=issue)
    ordinary = file_paths([entry for entry in entries if entry not in protected])
    if not ordinary:
        raise OperationRefusedError("repair diff is progress_only; implement the accepted source repair before requesting merge or refresh")
    check_paths(ordinary, authorized)
    return paths


async def workflow_content(provider, path, revision):
    response = await provider._call("GET", f"/repos/{provider.repo}/contents/{quote(path, safe='/')}?ref={revision}")
    if (
        not isinstance(response, dict)
        or response.get("type") != "file"
        or response.get("encoding") != "base64"
        or type(response.get("size")) is not int
        or not 0 < response["size"] <= 65536
        or not isinstance(response.get("content"), str)
    ):
        raise OperationRefusedError("repair workflow source unavailable")
    try:
        content = base64.b64decode(response["content"].encode("ascii").translate(None, b" \t\r\n\v\f"), validate=True)
    except (binascii.Error, UnicodeError):
        raise OperationRefusedError("repair workflow source invalid") from None
    if len(content) != response["size"]:
        raise OperationRefusedError("repair workflow source invalid")
    return content


def compare_workflow_authority(original, proposed):
    documents = []
    for content in (original, proposed):
        if not content or len(content) > 65536:
            raise OperationRefusedError("repair workflow source invalid")
        try:
            document = yaml.safe_load(content)
        except (UnicodeError, yaml.YAMLError):
            raise OperationRefusedError("repair workflow source invalid") from None
        if not isinstance(document, dict):
            raise OperationRefusedError("repair workflow source invalid")
        documents.append(document)
    before, after = documents
    if any(before.get(key) != after.get(key) for key in ("on", True, "permissions", "env", "concurrency", "defaults")):
        raise OperationRefusedError("repair workflow authority changed")
    jobs_before, jobs_after = before.get("jobs"), after.get("jobs")
    if not isinstance(jobs_before, dict) or not isinstance(jobs_after, dict) or jobs_before.keys() != jobs_after.keys():
        raise OperationRefusedError("repair workflow authority changed")
    for name, old in jobs_before.items():
        new = jobs_after[name]
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise OperationRefusedError("repair workflow authority changed")
        if {key: value for key, value in old.items() if key != "steps"} != {key: value for key, value in new.items() if key != "steps"}:
            raise OperationRefusedError("repair workflow authority changed")
        steps_old, steps_new = old.get("steps"), new.get("steps")
        if not isinstance(steps_old, list) or not isinstance(steps_new, list) or len(steps_old) != len(steps_new):
            raise OperationRefusedError("repair workflow authority changed")
        for old_step, new_step in zip(steps_old, steps_new, strict=True):
            if (
                not isinstance(old_step, dict)
                or not isinstance(new_step, dict)
                # Commands and display names can be repaired. Execution controls
                # (including conditions, credentials and failure handling) stay
                # bound to the reviewed workflow, regardless of input names.
                or {key: value for key, value in old_step.items() if key not in {"run", "name"}}
                != {key: value for key, value in new_step.items() if key not in {"run", "name"}}
            ):
                raise OperationRefusedError("repair workflow authority changed")
            if old_step.get("run") != new_step.get("run") and any(
                re.search(r"\bsecrets\b", str(step.get("run", "")), re.IGNORECASE) for step in (old_step, new_step)
            ):
                raise OperationRefusedError("repair workflow credential authority changed")


async def check_proposed_workflows(provider, changes):
    workflows = [change for change in changes if change.path.startswith(".github/workflows/")]
    if not workflows:
        return
    if any(change.deleted or change.mode != "100644" or change.content is None for change in workflows):
        raise OperationRefusedError("repair workflow must be a regular file")
    state = await provider.read_repository()
    base = state["default_branch_head"]
    for change in workflows:
        compare_workflow_authority(await workflow_content(provider, change.path, base), change.content)


async def check_workflow_authority(provider, paths, *, base, head):
    for path in paths:
        if path.startswith(".github/workflows/"):
            compare_workflow_authority(await workflow_content(provider, path, base), await workflow_content(provider, path, head))


async def refresh_repair_base(provider, body, authorized, reauthorize, status):
    """Refresh a tested PR only when GitHub requires an up-to-date branch.

    A clean, mergeable PR may retain an older base SHA. Moving main alone must
    not restart CI indefinitely. An actual GitHub 'behind' prerequisite uses
    the ordinary branch update and requires checks on the resulting new head.
    """
    if not (
        status["checks_verified"]
        and status["blockers"] == [{"code": "branch_not_mergeable", "state": "behind"}]
        and all(authorized.policy.permits(action) for action in (Action.OPERATE, Action.REPAIR, Action.MERGE))
    ):
        return None
    state = await provider.read_repository()
    if state["branch_head"] != body.expected_head or state["default_branch_head"] != status["base_head"]:
        raise ProviderConflictError("repair branch changed before refresh")
    if status["base"] != status["base_head"]:
        comparison = await provider._call("GET", f"/repos/{provider.repo}/compare/{status['base']}...{status['base_head']}")
        if comparison.get("status") != "ahead" or comparison.get("merge_base_commit", {}).get("sha") != status["base"]:
            return None
    paths = await qualified_merge_paths(provider, body.pull_number, authorized, head=status["head"])
    await check_workflow_authority(provider, paths, base=status["base_head"], head=status["head"])
    await reauthorize()
    fresh = await repair_status(
        provider, body.pull_number, expected_head=body.expected_head, required_checks=authorized.policy.repair_required_checks
    )
    if any(fresh[key] != status[key] for key in ("base", "base_head", "head", "checks_verified", "blockers")):
        raise ProviderConflictError("repair checks or branch changed before refresh")
    if await qualified_merge_paths(provider, body.pull_number, authorized, head=fresh["head"]) != paths:
        raise ProviderConflictError("repair diff changed before refresh")
    await reauthorize()
    await provider._call(
        "PUT",
        f"/repos/{provider.repo}/pulls/{body.pull_number}/update-branch",
        json={"expected_head_sha": body.expected_head},
        expect=(202,),
    )
    return {
        "merged": False,
        "reason": "repair_branch_update_requested",
        "pull_number": body.pull_number,
        "head": status["head"],
        "base_head": status["base_head"],
        "next_action": (
            "Read repair_status to record the refreshed head, wait for its checks, then call repair_merge with that head. The repair is not merged."
        ),
    }


async def merge_repair(provider, body, authorized, reauthorize):
    required_checks = getattr(authorized.policy, "repair_required_checks", ())
    status = await repair_status(provider, body.pull_number, expected_head=body.expected_head, required_checks=required_checks)
    if status["merged"]:
        if not status["checks_verified"] or not isinstance(status["merge_sha"], str) or len(status["merge_sha"]) != 40:
            raise OperationRefusedError("merged repair has no tested revision")
        commit = await provider._call("GET", f"/repos/{provider.repo}/commits/{status['merge_sha']}")
        parents = [item.get("sha") for item in commit.get("parents", [])]
        if commit.get("sha") != status["merge_sha"] or len(parents) != 2 or parents[1] != status["head"]:
            raise OperationRefusedError("merged repair identity changed")
        if status["base_head"] != status["merge_sha"]:
            compared = await provider._call("GET", f"/repos/{provider.repo}/compare/{status['merge_sha']}...{status['base_head']}")
            if compared.get("status") != "ahead" or compared.get("merge_base_commit", {}).get("sha") != status["merge_sha"]:
                raise OperationRefusedError("merged repair is not on the current base")
        paths = await qualified_merge_paths(provider, body.pull_number, authorized, head=status["head"])
        await check_workflow_authority(provider, paths, base=parents[0], head=status["head"])
        return {"merged": True, "sha": status["merge_sha"]}
    if not status["ready"]:
        refreshed = await refresh_repair_base(provider, body, authorized, reauthorize, status)
        if refreshed is not None:
            return refreshed
        return {"merged": False, "reason": "checks_or_branch_not_ready", **status}
    paths = await qualified_merge_paths(provider, body.pull_number, authorized, head=status["head"])
    await check_workflow_authority(provider, paths, base=status["base_head"], head=status["head"])

    async def checked():
        await reauthorize()
        fresh = await repair_status(provider, body.pull_number, expected_head=body.expected_head, required_checks=required_checks)
        if not fresh["ready"] or any(fresh[key] != status[key] for key in ("base", "base_head")):
            raise ProviderConflictError("repair checks or base changed before merge")
        updated_paths = await qualified_merge_paths(provider, body.pull_number, authorized, head=fresh["head"])
        if updated_paths != paths:
            raise ProviderConflictError("repair diff changed before merge")
        await check_workflow_authority(provider, updated_paths, base=fresh["base_head"], head=fresh["head"])

    # GitHub enforces branch protections too. There is no bypass/admin merge.
    return await provider.merge_pull_request(pull_number=body.pull_number, expected_head=body.expected_head, reauthorize=checked, method="merge")
