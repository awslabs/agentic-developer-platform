"""Authenticate a shared worker's current review assignment for GitHub minting."""

from fastapi import HTTPException

from src.internal.credential_binding import InstallationBinding
from src.shared.database import get_session_factory


async def verify_shared_review_worker(request):
    from src.agentauth.routes import require_agent_transport
    from src.orchestration.execution_policy import Action
    from src.orchestration.review_cycle import CycleBlockedError
    from src.orchestration.run_reports import RunReportError, authenticate_run_report
    from src.orchestration.shared_cycle import validate_current_report_assignment
    from src.orchestration.shared_policy import authorize_shared_model

    await require_agent_transport(request)
    body = await request.json()
    try:
        async with get_session_factory()() as session:
            row = await authenticate_run_report(session, request.headers.get("X-Adp-Report-Credential", ""), lock=False)
            await validate_current_report_assignment(session, row)
            if (
                body.get("identity") != "review"
                or body.get("invocation_id") != row.run_id
                or body.get("installation_id") != row.installation_id
                or f"{body.get('repo_owner', '')}/{body.get('repo_name', '')}" != row.repo
                or (row.dispatch_metadata.get("review_cycle_input") or {}).get("action") != Action.REVIEW.value
                or row.terminal_receipt
                or not row.worker_receipt
            ):
                raise HTTPException(404, "not found")
            await authorize_shared_model(session, row)
            # Issue #5663 (A09): carry the repository this review assignment is
            # actually for. `row` is the authenticated run-report row and `row.repo`
            # was just compared against the request above, so it is server-owned
            # state, not a caller assertion.
            #
            # Omitting it made this legitimate binding look "unbound" to the token
            # route's repository check, so a valid shared-review mint was refused
            # under strict denial while every other check on it passed. The
            # repository is known here and there is no reason to drop it.
            request.state.agent_installation_binding = InstallationBinding(
                tenant_id=row.org_id,
                installation_id=row.installation_id,
                repo=row.repo,
            )
            request.state.agent_authorized_action = Action.REVIEW
            request.state.agent_github_permissions = {"contents": "read", "pull_requests": "write", "metadata": "read"}
            request.state.shared_review_identity = True
    except (RunReportError, CycleBlockedError):
        raise HTTPException(404, "not found") from None
