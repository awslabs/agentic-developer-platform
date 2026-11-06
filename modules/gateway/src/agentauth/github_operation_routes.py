"""The mediated GitHub operation endpoint (#5223).

One route, `POST /internal/v1/agent/self/github-operation`, on which a worker asks
for a *typed operation* instead of asking for a credential. The gateway holds the
installation token, re-authorizes, performs that one operation, and returns the
result. See :mod:`src.agentauth.github_operation_service` for why a token cannot
express "may push this branch, may not merge it".

## Why this path and not a new top-level one

`/internal/v1/agent/self/*` is reached deliberately:

- The worker IAM boundary already allows `internal/v1/agent/*` and denies
  everything else by `NotResource`, so no new IAM statement is needed to reach an
  authenticated place (#5195/#5210 cover the permission map for genuinely new
  internal routes).
- The prefix carries `require_agent_transport`, which is the two-proof
  requirement: the HMAC run credential says *which invocation and attempt*, and
  the projected workload token says *which pod*. SigV4 alone cannot distinguish
  workers — they share one role.
- `/self` names the actual authorization model: this acts on the caller's own
  assignment, derived from its credential, never on a run named in the request.

It is deliberately NOT in `BROKER_PATHS`. That set is for the legacy brokers
mounted outside this router, whose generic branch requires `body["user_id"]` to
match the row's authorized user; a non-user-scoped path added there would simply
be refused. This route authenticates through the router's own dependency and then
calls `authorize_operation` itself, which is the same check
`verify_broker_worker` performs and one fewer place for the two to drift.

## What the request may say

Almost nothing that matters. Repository, branch, installation, tenant, deadline
and accepted plan version are all derived from protected records. The request
carries the operation, the content, and *assertions* which are compared and never
adopted. There is no method field, no URL field, and no way to name another run.

Refusals collapse to one 404 for the same reason as the rest of this router: a
caller able to tell "not authorized" from "does not exist" learns what exists.
Conflicts and missing repair scope are exceptions reachable only after full
authorization. Missing scope is a fixed diagnostic about the caller's own
accepted plan, reauthorized again before disclosure; it grants no write access.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.github_operation_service import (
    authorize_operation,
    current_claim_generation,
    installation_token,
    operation_permissions,
)
from src.agentauth.github_operations import (
    MAX_OPERATION_REQUEST_BYTES,
    GitHubOperation,
    OperationRefusedError,
    RepairScopeMissingError,
    idempotency_key,
    request_hash,
)
from src.agentauth.github_provider import (
    ARCHIVE_SLICE_BYTES,
    MAX_ARCHIVE_BYTES,
    MAX_BLOB_BYTES,
    FileChange,
    GitHubProvider,
    ProviderConflictError,
    ProviderUnavailableError,
    reconcile_commit,
    reconcile_pull_request,
)
from src.agentauth.routes import AgentRuntime, get_agent_runtime, require_agent_transport
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError

logger = logging.getLogger("bedrockgateway.agentauth.github_operation_routes")

router = APIRouter(
    prefix="/internal/v1/agent/self",
    tags=["agent-authority"],
    dependencies=[Depends(require_agent_transport)],
)

# Bounds applied by pydantic before anything reaches the provider, so an oversized
# body is refused at parse time rather than after we have started uploading it.
_MAX_PATH_CHARS = 1024
# Derived from the provider's own blob cap rather than restated, so lowering that
# constant for the transport cannot leave a stale, larger bound admitting bodies the
# provider will then refuse. Base64 is 4 chars per 3 bytes, rounded up to the pad.
_MAX_CONTENT_CHARS = (MAX_BLOB_BYTES + 2) // 3 * 4
_MAX_FILES = 500
_MAX_TEXT_CHARS = 65_536


class FileChangeRequest(BaseModel):
    """One file's contribution to a commit, as git would record it.

    `content_base64` rather than a string: the helper publishes actual local
    changes, and those include binary files and files whose bytes are not valid
    UTF-8. Encoding at the boundary means the platform never has to guess a text
    encoding for content it is only transporting.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=_MAX_PATH_CHARS)
    content_base64: str | None = Field(default=None, max_length=_MAX_CONTENT_CHARS)
    mode: str = Field(default="100644", pattern=r"^\d{6}$")
    deleted: bool = False


class OperationRequest(BaseModel):
    """A typed operation request.

    Note what is absent: no method, no URL, no repository, no branch, no
    installation, no tenant, no expiry. `extra="forbid"` makes an attempt to add
    one a 422 rather than a silently ignored field.

    `repository` and `branch` are present but are *assertions*: the service
    compares them to the protected record and refuses on mismatch. They exist so a
    worker whose view of its own assignment has drifted gets a refusal instead of
    quietly writing somewhere it did not intend.
    """

    model_config = ConfigDict(extra="forbid")

    operation: GitHubOperation
    repository: str | None = Field(default=None, max_length=512)
    branch: str | None = Field(default=None, max_length=512)

    changes: list[FileChangeRequest] = Field(default_factory=list, max_length=_MAX_FILES)
    message: str | None = Field(default=None, max_length=_MAX_TEXT_CHARS)
    expected_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    task_cycle: int = Field(default=1, ge=1, le=1000)
    task_stage: str | None = Field(default=None, pattern=r"^(diagnose|repair|test|publish|deploy|verify)$")
    task_state: str | None = Field(default=None, pattern=r"^(in_progress|done|blocked)$")
    diagnosis_code: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")

    title: str | None = Field(default=None, max_length=_MAX_TEXT_CHARS)
    body: str | None = Field(default=None, max_length=_MAX_TEXT_CHARS)

    pull_number: int | None = Field(default=None, ge=1, le=1_000_000)
    review_event: str | None = Field(default=None, max_length=32)

    # Which ref to materialize, for FETCH_REPOSITORY_ARCHIVE. An assertion like
    # `repository` and `branch`: the provider refuses any value that is not the
    # assigned working branch or the assigned default branch, so naming a ref cannot
    # widen what this run may read.
    ref: str | None = Field(default=None, max_length=512)

    # Which window of the archive to return. Bounded here so an offset beyond any
    # permitted archive is a 422 rather than a provider fetch, and so `archive_length`
    # cannot ask for a response the deployed edge will not deliver: the provider caps
    # it at `ARCHIVE_SLICE_BYTES` regardless, this bound just refuses the request
    # earlier and states the contract in the schema.
    archive_offset: int = Field(default=0, ge=0, le=MAX_ARCHIVE_BYTES)
    archive_length: int | None = Field(default=None, ge=1, le=ARCHIVE_SLICE_BYTES)

    def file_changes(self) -> list[FileChange]:
        """Decode into the provider's own validated type.

        `FileChange.__post_init__` re-refuses traversing paths, unsupported modes,
        oversized content and deletion-with-content. The pydantic bounds above are
        a cheap first pass; the dataclass is where the security statement lives, so
        both run.
        """
        import base64
        import binascii

        decoded = []
        for change in self.changes:
            content = None
            if change.content_base64 is not None:
                try:
                    content = base64.b64decode(change.content_base64, validate=True)
                except (binascii.Error, ValueError):
                    raise OperationRefusedError("proposed change content is not decodable") from None
            decoded.append(FileChange(path=change.path, content=content, mode=change.mode, deleted=change.deleted))
        return decoded


async def _authenticated(runtime: AgentRuntime, request: Request):
    """Both proofs, verified now: which run/attempt, and which pod."""
    context = await run_in_threadpool(
        runtime.authenticate,
        request.headers.get("X-Adp-Run-Credential", ""),
        request.headers.get(WORKLOAD_HEADER, ""),
    )
    from src.agentauth.grants import AUTHORITY_PAID_DOMAIN_OPERATION

    if context[3].authority.kind == AUTHORITY_PAID_DOMAIN_OPERATION:
        raise BootstrapRefusedError("paid domain worker has no GitHub authority")
    return context


@router.post("/github-operation")
async def perform_github_operation(
    body: OperationRequest,
    request: Request,
    runtime: AgentRuntime = Depends(get_agent_runtime),
) -> JSONResponse:
    """Authorize and perform one typed GitHub operation for the calling run."""
    from src.shared.database import get_session_factory

    # Field caps do not bound the whole wire request: paths and messages can
    # expand under JSON escaping. The deployed edge already has a 10,000,000-byte
    # hard stop; this lower application bound also protects direct/non-worker
    # callers and keeps one stated contract on both sides of the transport.
    if len(await request.body()) > MAX_OPERATION_REQUEST_BYTES:
        raise HTTPException(413, "mediated operation request is too large")

    now = datetime.now(UTC)
    try:
        # `authenticate` returns the ExecutionRecord dataclass, which is what
        # `validate_flow` consumes. `authorize_worker_credential` and
        # `build_assignment` read the RAW protected item instead (DynamoDB
        # attribute-value shape), so the raw read below is not a duplicate: the
        # record cannot be `.get()`-ed, and the raw item carries the assignment
        # fields (installation_id, issue_number, orchestration_node_*) that the
        # record does not project. Same two-value pattern as
        # `broker_identity.verify_broker_worker`.
        _pod, caller, record, grant = await _authenticated(runtime, request)
        await runtime.validate_flow(record, grant)

        async with get_session_factory()() as session:
            # The generation is READ, never accepted from the request: see
            # `current_claim_generation`. Taking it from the body would let a stale
            # worker assert the generation it wished it still held.
            generation = await current_claim_generation(session, org_id=caller.tenant_id, invocation_id=caller.invocation_id)

        async def authorize():
            """Read current authority before each effect, including revocation.

            Grants are frozen snapshots of authority-store rows. Rechecking the
            initial object cannot observe its revocation. A fresh SQL session also
            prevents policy/claim objects retained in an identity map from masking
            an update made during the provider upload.
            """
            current_pod, current_caller, current_record, current_grant = await _authenticated(runtime, request)
            await runtime.validate_flow(current_record, current_grant)
            current_execution = await run_in_threadpool(
                runtime.store._read, f"TENANT#{current_caller.tenant_id}", f"EXEC#{current_caller.invocation_id}"
            )
            if not current_execution:
                raise OperationRefusedError("protected execution record is unavailable")
            async with get_session_factory()() as current_session:
                return await authorize_operation(
                    current_session,
                    execution=current_execution,
                    grant=current_grant,
                    operation=body.operation,
                    workload_binding=current_pod.uid,
                    claim_generation=generation,
                    asserted_repository=body.repository,
                    asserted_branch=body.branch,
                )

        authorized = await authorize()
        try:
            result = await _perform(body, authorized, authorize, now=now)
        except RepairScopeMissingError:
            # An own-assignment policy prerequisite is actionable, but a stale,
            # revoked or foreign assignment must still look exactly like 404.
            # Reauthorize before returning only gateway-owned static guidance.
            current = await authorize()
            if current != authorized or not current.operations_repair:
                raise OperationRefusedError("repair authority changed before diagnosis") from None
            raise HTTPException(
                403,
                detail={
                    "code": "repair_scope_not_accepted",
                    "message": "The accepted plan has neither repair_paths nor repair_scope='repository'.",
                    "next_action": (
                        "Ask the flow owner to review and accept a supported plan amendment with explicit repair paths "
                        "(or reviewed repository scope). Review approval changes and reapprove any invalidated budget/limit "
                        "supplements before retrying. Retrying unchanged cannot resolve this."
                    ),
                },
                headers={"Cache-Control": "no-store"},
            ) from None
        if authorized.operations_repair and body.operation is GitHubOperation.READ_REPOSITORY and body.pull_number is not None:
            from src.orchestration.operations_tasks import record_repair_pr

            if await authorize() != authorized:
                raise OperationRefusedError("repair authority changed before recording PR")
            async with get_session_factory()() as session:
                observation = await record_repair_pr(session, authorized.assignment, result["repair"])
                await session.commit()
            result["repair"].update(observation)
        if authorized.operations_repair and body.operation is GitHubOperation.MERGE_PULL_REQUEST:
            from src.orchestration.operations_repair import record_repair_merge

            await authorize()
            async with get_session_factory()() as session:
                await record_repair_merge(
                    session,
                    authorized.assignment,
                    pull_number=body.pull_number,
                    head=body.expected_head,
                    merged=result["merge"],
                    authorized=authorized,
                )
                await session.commit()

        logger.info(
            "Mediated GitHub operation performed",
            extra={
                "principal": caller.principal,
                "operation": body.operation.value,
                "repository_id": authorized.assignment.repository_id,
                "branch": authorized.assignment.branch,
                "plan_version": authorized.plan_version,
            },
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except ProviderConflictError:
        # Reachable only after full authorization, and the worker needs to know:
        # a conflict means reconcile and retry, not "give up, you are not allowed".
        logger.info("Mediated GitHub operation conflicted", extra={"operation": body.operation.value})
        raise HTTPException(409, "the assigned branch or pull request moved") from None
    except ProviderUnavailableError:
        raise HTTPException(503, "the provider is unavailable") from None
    except OperationRefusedError as exc:
        # One shape for every authorization failure. The reason is logged, not
        # returned; see the module docstring.
        logger.info("Mediated GitHub operation refused", extra={"operation": body.operation.value, "reason": str(exc)})
        raise HTTPException(404, "not found") from None
    except (BootstrapRefusedError, WorkloadRefusedError, CredentialError, ExecutionStateError):
        raise HTTPException(404, "not found") from None
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None


async def _perform(body: OperationRequest, authorized, authorize, *, now: datetime) -> dict:
    """Dispatch to the one typed operation, holding the token for its duration.

    The token is minted per operation with the narrowest permissions that can
    perform it, used inside this function, and never returned or logged. There is
    no branch here that forwards a caller-supplied method or URL — that absence is
    the control, not an omission.
    """
    assignment = authorized.assignment
    if body.operation is GitHubOperation.RECONCILE_WORK_BRANCH:
        # A first start or an already-current branch needs no write credential,
        # especially for installations that do not grant workflow editing.
        token = await installation_token(
            org_id=assignment.tenant_id,
            installation_id=assignment.installation_id,
            repository=assignment.repository,
            permissions=operation_permissions(GitHubOperation.READ_REPOSITORY),
        )
        async with GitHubProvider(token=token, assignment=assignment) as provider:
            reconciliation = await provider.inspect_work_branch()
        if await authorize() != authorized:
            raise OperationRefusedError("the protected assignment changed during reconciliation")
        if reconciliation["status"] != "needs_merge":
            return {"reconciliation": reconciliation}
    if authorized.operations_repair and body.operation in {
        GitHubOperation.VALIDATE_REPAIR_EDIT,
        GitHubOperation.RECONCILE_WORK_BRANCH,
        GitHubOperation.PUBLISH_COMMIT,
        GitHubOperation.UPSERT_PULL_REQUEST,
        GitHubOperation.MERGE_PULL_REQUEST,
    }:
        from src.agentauth.operations_repair import ensure_repair_readiness

        await ensure_repair_readiness(authorized, authorize)
    if body.operation in {GitHubOperation.READ_REPAIR_TASKS, GitHubOperation.RECORD_REPAIR_TASK}:
        from src.orchestration.operations_tasks import read_operations_tasks, record_operations_task
        from src.shared.database import get_session_factory

        if not authorized.operations_repair:
            raise OperationRefusedError("operations task requires an operations executor")
        if body.operation is GitHubOperation.RECORD_REPAIR_TASK and (body.task_stage is None or body.task_state is None):
            raise OperationRefusedError("operations task update requires stage and state")
        if body.operation is GitHubOperation.READ_REPAIR_TASKS and any((body.task_stage, body.task_state, body.diagnosis_code)):
            raise OperationRefusedError("operations task read cannot change progress")
        if await authorize() != authorized:
            raise OperationRefusedError("operations task authority changed")
        async with get_session_factory()() as session:
            if body.operation is GitHubOperation.RECORD_REPAIR_TASK:
                await record_operations_task(
                    session,
                    assignment,
                    stage=body.task_stage,
                    state=body.task_state,
                    diagnosis_code=body.diagnosis_code,
                    repair_cycle=body.task_cycle,
                )
                await session.commit()
            tasks = await read_operations_tasks(session, assignment)
            tasks["repair_paths"] = sorted(authorized.policy.repair_paths)
            if getattr(authorized.policy, "repair_scope", None) is not None:
                tasks["repair_scope"] = authorized.policy.repair_scope
            from src.orchestration.story_checkpoints import read_story_checkpoint

            checkpoint = await read_story_checkpoint(session, assignment)
            return {"operations_tasks": tasks, "story_checkpoint": checkpoint}
    if body.operation is GitHubOperation.VALIDATE_REPAIR_EDIT:
        from hashlib import sha256

        from src.agentauth.operations_repair import check_paths, check_proposed_workflows

        if not authorized.operations_repair:
            raise OperationRefusedError("repair editing requires an operations story")
        changes = body.file_changes()
        if len(changes) != 1 or changes[0].deleted or changes[0].mode != "100644" or changes[0].content is None:
            raise OperationRefusedError("repair edit must be one regular file")
        # The agent submits small exact replacements, while policy validation
        # must inspect the whole prospective source file, including workflows.
        if len(changes[0].content) > 256 * 1024:
            raise OperationRefusedError("repair edit exceeds workspace bound")
        check_paths([changes[0].path], authorized)
        if changes[0].path.startswith(".github/workflows/"):
            token = await installation_token(
                org_id=assignment.tenant_id,
                installation_id=assignment.installation_id,
                repository=assignment.repository,
                permissions=operation_permissions(GitHubOperation.READ_REPOSITORY),
            )
            async with GitHubProvider(token=token, assignment=assignment) as provider:
                await check_proposed_workflows(provider, changes)
        current = await authorize()
        if current != authorized:
            raise OperationRefusedError("repair scope changed during validation")
        return {"path": changes[0].path, "sha256": sha256(changes[0].content).hexdigest()}
    permissions = operation_permissions(body.operation)
    if authorized.operations_repair and (
        body.operation is GitHubOperation.MERGE_PULL_REQUEST or body.operation is GitHubOperation.READ_REPOSITORY and body.pull_number is not None
    ):
        # Repair readiness reads both check runs and the combined commit status.
        # GitHub's checks permission does not grant access to commit statuses.
        permissions = {**permissions, "statuses": "read"}
    if authorized.operations_repair and (
        body.operation is GitHubOperation.MERGE_PULL_REQUEST
        or body.operation is GitHubOperation.PUBLISH_COMMIT
        and any(c.path.startswith(".github/workflows/") for c in body.changes)
    ):
        permissions = {**permissions, "workflows": "write"}
    token = await installation_token(
        org_id=assignment.tenant_id,
        installation_id=assignment.installation_id,
        repository=assignment.repository,
        permissions=permissions,
    )
    key = idempotency_key(assignment=assignment, operation=body.operation, request_hash=request_hash(body.model_dump(mode="json")))

    async def reauthorize() -> None:
        current = await authorize()
        if current.assignment != assignment or (authorized.operations_repair and current != authorized):
            raise OperationRefusedError("the protected assignment changed during publication")

    async with GitHubProvider(token=token, assignment=assignment) as provider:
        if body.operation is GitHubOperation.READ_REPOSITORY and authorized.operations_repair and body.pull_number is not None:
            from src.agentauth.operations_repair import repair_status

            return {
                "repair": await repair_status(provider, body.pull_number, required_checks=getattr(authorized.policy, "repair_required_checks", ())),
                "idempotency_key": key,
            }

        if body.operation is GitHubOperation.READ_REPOSITORY:
            return {"repository": await provider.read_repository(), "branch": assignment.branch, "idempotency_key": key}

        if body.operation is GitHubOperation.RECONCILE_WORK_BRANCH:
            return {"reconciliation": await provider.reconcile_work_branch(reauthorize=reauthorize), "idempotency_key": key}
        if body.operation is GitHubOperation.FETCH_REPOSITORY_ARCHIVE:
            # Base64 in JSON keeps one response shape for every operation, so the
            # worker's transport, signing and error handling stay identical rather
            # than growing a second binary path.
            #
            # One SLICE per response, not the whole archive. The deployed edge is a
            # REST API Gateway whose 10 MB response limit is a hard, unraisable
            # service quota; base64 costs 4/3, so a whole-archive response was not
            # deliverable for any real repository (this repo's own tarball is
            # ~12.1 MiB raw / ~16.9 MB encoded). `ARCHIVE_SLICE_BYTES` is chosen so
            # an encoded slice plus this envelope stays inside that limit.
            import base64 as _base64

            sliced = await provider.fetch_repository_archive(ref=body.ref, offset=body.archive_offset, length=body.archive_length)
            return {
                "commit_sha": sliced.commit_sha,
                "branch": assignment.branch,
                "repository": assignment.repository,
                "archive_format": "tar.gz",
                # Whole-archive identity, repeated on every slice so the worker can
                # prove its reassembly is of one snapshot rather than spliced across
                # a push that happened between slice requests.
                "archive_total_bytes": sliced.total_bytes,
                "archive_digest": sliced.digest,
                "archive_digest_algorithm": "sha256",
                "archive_offset": sliced.offset,
                "archive_slice_bytes": len(sliced.content),
                "archive_complete": sliced.complete,
                "archive_base64": _base64.b64encode(sliced.content).decode("ascii"),
                "idempotency_key": key,
            }

        if body.operation is GitHubOperation.PUBLISH_COMMIT:
            changes = body.file_changes()
            _refuse_escalating_content(changes, authorized)
            if authorized.operations_repair:
                from src.agentauth.operations_repair import check_proposed_workflows

                await check_proposed_workflows(provider, changes)
            try:
                published = await provider.publish_commit(
                    changes=changes,
                    message=body.message or "",
                    expected_head=body.expected_head,
                    reauthorize=reauthorize,
                )
            except ProviderUnavailableError as exc:
                # A timeout is not evidence that nothing happened. Reconcile
                # before surfacing it, so the worker's retry cannot publish the
                # same change twice. Identity comes from the objects the provider
                # had already built (carried on the error); without them the
                # outcome stays unknown and the error surfaces unchanged.
                landed = await reconcile_commit(
                    provider,
                    message=body.message or "",
                    expected_parent=exc.prepared_parent,
                    expected_tree=exc.prepared_tree,
                )
                if landed is None:
                    raise
                published = landed
            return {
                "commit_sha": published.sha,
                "parent_sha": published.parent_sha,
                "branch": published.branch,
                "idempotency_key": key,
            }

        if body.operation is GitHubOperation.UPSERT_PULL_REQUEST:
            try:
                pull = await provider.upsert_pull_request(title=body.title or "", body=body.body or "", reauthorize=reauthorize)
            except ProviderUnavailableError:
                # Reconciled against the requested title/body, not against "a PR
                # exists": the update path PATCHes a PR that was already open, so
                # existence proves nothing about whether this change landed.
                pull = await reconcile_pull_request(provider, title=body.title or "", body=body.body or "")
                if pull is None:
                    raise
            return {"pull_request": pull, "idempotency_key": key}

        if body.operation is GitHubOperation.PUBLISH_REVIEW:
            if body.pull_number is None:
                raise OperationRefusedError("no pull request was named for the review")
            review = await provider.publish_review(
                pull_number=body.pull_number,
                body=body.body or "",
                event=body.review_event or "COMMENT",
                reauthorize=reauthorize,
            )
            return {"review": review, "idempotency_key": key}

        if body.operation is GitHubOperation.MERGE_PULL_REQUEST:
            # Reached only because `authorize_operation` confirmed `Action.MERGE`
            # is currently autonomous. Mediation performs a merge it is asked for;
            # it does not decide or schedule one — that is #5130's controller.
            if body.pull_number is None or body.expected_head is None:
                raise OperationRefusedError("a merge must name the pull request and the head it was reviewed at")
            if authorized.operations_repair:
                from src.agentauth.operations_repair import merge_repair

                merged = await merge_repair(provider, body, authorized, reauthorize)
                return {"merge": merged, "idempotency_key": key}
            merged = await provider.merge_pull_request(
                pull_number=body.pull_number,
                expected_head=body.expected_head,
                reauthorize=reauthorize,
            )
            return {"merge": merged, "idempotency_key": key}

    # Unreachable for enum members. A new operation must state its handling above
    # rather than fall through to something permissive.
    raise OperationRefusedError("operation has no mediated handler")


def _refuse_escalating_content(changes: list[FileChange], authorized) -> None:
    """Refuse content that could itself perform a gated action.

    A workflow definition is not just a file: merging it gives repository
    automation the ability to do things a human gate was supposed to hold. Branch
    naming is explicitly insufficient authority for that — the check consults the
    accepted policy document, not the branch the change is going to.
    """
    from src.agentauth.github_operations import escalating_paths

    if authorized.operations_repair:
        from src.agentauth.operations_repair import check_paths

        check_paths([change.path for change in changes], authorized)
        return
    offending = escalating_paths([change.path for change in changes], policy=authorized.policy)
    if offending:
        logger.info("Mediated commit refused for automation content", extra={"paths": list(offending)})
        raise OperationRefusedError("proposed content requires separately accepted authority")


__all__ = ["FileChangeRequest", "OperationRequest", "perform_github_operation", "router"]
