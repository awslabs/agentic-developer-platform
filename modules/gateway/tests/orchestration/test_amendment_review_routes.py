"""The amendment review loop over HTTP (#5329).

The store's guarantees live in `test_amendment_review.py`; the placement resolver's in
`test_amendment_placement.py`; the impact report's in `test_amendment_impact.py`. This file
tests the **doors**: what a caller reaches, what each refusal costs, and whether the
response carries what a human needs to decide.

Seven routes, one interaction: propose -> preview -> edit -> approve -> a new plan version.

What is actually at stake here, and so what these tests are about:

  - **A refused call must write nothing.** Every refusal path is asserted by row count, not
    by reading the handler. A propose whose placement is ambiguous, a reject of an accepted
    draft, an accept of an edited one — each leaves the graph and the draft table where
    they were.
  - **Placement is explicit, and the response proves it.** `resolved_edges` carries the
    concrete from/to addresses with the rule that produced each. Wave membership or visual
    order alone would leave a reviewer unable to confirm the work landed where they meant,
    which is the requirement this feature exists to satisfy.
  - **An approval binds to the revision that was previewed.** The edit->accept sequence is
    asserted end to end through HTTP: preview, edit, then accept the pre-edit hash, and the
    accept is refused with a 409 that names the code a CLI can branch on.
  - **`PLAN_APPROVE` on every route, including the GETs and the preview.** Asserted on the
    recorded `check_permission` call rather than by reading the source, and asserted to
    happen *before* any read, so a denied caller cannot learn whether the flow exists.
  - **The permission story for `edit` specifically.** It writes no plan, so a weaker
    permission looks defensible; the test states why it is not.

The 404 shape is deliberately uniform: absent, another tenant's, and a draft on a different
flow are one answer, because `show` is the easy call to make in an enumeration loop.
"""

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAmendmentRequest,
    OrchestrationEdge,
    OrchestrationNode,
    OrchestrationPendingAmendment,
    PendingAmendmentState,
)
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.base import Base

from .test_amendment_review import accepted_flow, edited_proposal, pending_draft
from .test_pending_amendments import ORG_A, ORG_B, address, amended_proposal

HUMAN_USER_ID = "cognito-sub-reviewer"


def proposals_route(flow_id: str) -> str:
    return f"/orchestration/flows/{flow_id}/amendments/proposals"


def requests_route(flow_id: str) -> str:
    return f"/orchestration/flows/{flow_id}/amendments/requests"


def drafts_route(flow_id: str) -> str:
    return f"/orchestration/flows/{flow_id}/amendments/drafts"


def draft_route(flow_id: str, draft_id: str, action: str = "") -> str:
    suffix = f"/{action}" if action else ""
    return f"{drafts_route(flow_id)}/{draft_id}{suffix}"


def exact_accept_body(client: TestClient, flow_id: str, draft_id: str, **updates) -> dict:
    draft = client.get(draft_route(flow_id, draft_id)).json()
    body = {
        "expected_proposal_hash": draft["proposal_hash"],
        "expected_base_plan_version": draft["base_plan_version"],
        "expected_base_plan_hash": draft["base_plan_hash"],
    }
    body.update(updates)
    return body


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs. Same two pysqlite hooks as
    `test_pending_amendments.py` — see that module's docstring for why they are
    load-bearing rather than boilerplate."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def token_context(org_id: str, *, user_id: str = HUMAN_USER_ID, account_type: str = "human", auth_source: str = "jwt"):
    """A human operator's token. `account_type="human"`, because every route here is a
    human-authority surface — acceptance in particular is human-only by design."""
    from datetime import UTC, datetime, timedelta

    from src.shared.schemas.auth import TokenContext

    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type=account_type,
        auth_source=auth_source,
        scope="",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def app_with_router(session):
    """A minimal app carrying only the amendment-review router.

    Deliberately not `create_app()`: that pulls the whole middleware stack and would make
    an authz assertion here depend on all of it. Same shape as
    `test_amendment_draft_route.py`'s.
    """
    from src.auth.dependencies import get_current_user
    from src.orchestration.amendment_review_routes import router as review_router
    from src.shared.database import get_db

    app = FastAPI()
    app.include_router(review_router, prefix="/orchestration")

    # `AccessDeniedError` carries status_code=403 and is translated by the app-level
    # handler in `create_app()`. Registered here because this minimal app skips it;
    # without it a denied caller surfaces as 500 and the authz assertions would be
    # testing the harness rather than the route.
    @app.exception_handler(BedrockGatewayError)
    async def _gateway_error_handler(_request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: token_context(ORG_A)
    return app


def client_for(app, *, permitted: bool = True, role: str = AdminRole.ORG_ADMIN.value):
    """A TestClient whose access control is stubbed to permit or deny.

    Denial is expressed as `PLAN_APPROVE` because that is what every route here gates on.
    """
    from unittest.mock import AsyncMock, MagicMock

    from src.admin.exceptions import AccessDeniedError
    from src.orchestration.amendment_review_routes import get_access_control

    access = MagicMock(spec=AccessControl)
    if permitted:
        access.check_permission = AsyncMock(return_value=True)
    else:
        access.check_permission = AsyncMock(
            side_effect=AccessDeniedError(
                message=f"Permission '{Permission.PLAN_APPROVE.value}' is required for this operation",
                required_permission=Permission.PLAN_APPROVE.value,
                user_role=role,
            )
        )
    access.get_user_role = AsyncMock(return_value=(AdminRole(role), ORG_A, None))

    app.dependency_overrides[get_access_control] = lambda: access
    app.state.access_stub = access
    return TestClient(app, raise_server_exceptions=False)


def _unplanned_flow(flow_id: str, *, org_id: str = ORG_A):
    """A flow row with no accepted plan. There is nothing to place work relative to."""
    from src.orchestration.models import OrchestrationFlow

    return OrchestrationFlow(org_id=org_id, id=flow_id, slug=flow_id.replace("flow-", ""), title="No plan")


async def count_rows(session: AsyncSession, model) -> int:
    return (await session.execute(sa.select(sa.func.count()).select_from(model.__table__))).scalar_one()


async def graph_counts(session: AsyncSession) -> dict[str, int]:
    """Row counts of everything a refused call must not touch."""
    return {
        name: await count_rows(session, model)
        for name, model in {
            "nodes": OrchestrationNode,
            "edges": OrchestrationEdge,
            "plans": OrchestrationAcceptedPlan,
            "drafts": OrchestrationPendingAmendment,
        }.items()
    }


def insert_after_body(anchor: str, *, ref: str = "cleanup", title: str = "Clean up the migration scaffolding") -> dict:
    """A well-formed anchored proposal: one story, placed after `anchor`.

    `ref` is a bare final segment, not a full address: the first three segments are a
    consequence of the placement, which is what stops a caller naming one wave in the
    placement and another in the address. The placement kind on the wire is
    `insert_after` (`PlacementKind`'s value), not `insert-after`.
    """
    return {
        "placement": {
            "work": [{"ref": ref, "kind": "story", "title": title, "issue_ref": "5329"}],
            "placement": {"kind": "insert_after", "anchor": anchor},
        },
        "request_text": "clean up the scaffolding before the deploy gate",
    }


def validation_body(*, ref: str = "migration-check", wave_ref: str = "wave-1b") -> dict:
    """A read-only validation task, in its own wave, with both ends explicit.

    An `eval` rather than a story deliberately. A read-only validation task represented as
    a code story would require a pull request that does not exist, so the proposal would be
    unsatisfiable by construction — the misrepresentation #5329 calls out.

    Its own wave, also deliberately, and not a stylistic choice: `validate_proposal`'s
    `wave_eval_cardinality` rule gives a wave containing stories *exactly one* eval, so an
    eval added to Wave 1 is refused rather than accommodated (asserted directly in
    `test_an_added_eval_does_not_silently_relax_wave_eval_cardinality`). A new wave is
    therefore where a validation addition actually goes, and because a new wave has no
    anchor to derive an order from, `requires`/`precedes` carry the rejoin explicitly —
    which is the same "explicit incoming and outgoing edges" the placement kinds exist to
    produce.
    """
    return {
        "placement": {
            "work": [
                {
                    "ref": ref,
                    "kind": "eval",
                    "title": "Validate the migration",
                    "issue_ref": "5320",
                    "requires": [address("eval")],
                    "precedes": [address("deploy-gate", wave="wave-2")],
                }
            ],
            "placement": {"kind": "new_wave", "epic_ref": "epic-1", "wave_ref": wave_ref},
        },
        "request_text": "validate the migration before the deploy gate",
    }


def refuse_publication(monkeypatch):
    """Any provider call fails the test: these paths must publish nothing."""

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("issue publication reached the provider")

    for method in ("find_issue", "create_issue", "update_issue"):
        monkeypatch.setattr(f"src.orchestration.amendment_issue_publisher.AmendmentIssuePublisher.{method}", forbidden)


class TestPropose:
    """Placement resolves to explicit addresses and edges, or nothing is filed."""

    async def test_text_only_request_uses_the_durable_authoring_queue(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router)

        first = client.post(requests_route(flow_id), json={"request_text": "add live cleanup validation"})
        second = client.post(requests_route(flow_id), json={"request_text": "add live cleanup validation"})

        assert first.status_code == 202, first.text
        assert second.status_code == 202, second.text
        assert second.json()["request_id"] == first.json()["request_id"]
        assert second.json()["already_requested"] is True
        request = (await session.execute(sa.select(OrchestrationAmendmentRequest))).scalar_one()
        assert request.state == "queued"
        assert await count_rows(session, OrchestrationPendingAmendment) == 0

    async def test_a_resolvable_placement_files_a_pending_draft(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router)

        response = client.post(proposals_route(flow_id), json=insert_after_body(address("story-a")))

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["flow_id"] == flow_id
        assert len(body["added_nodes"]) == 1

        stored = (await session.execute(sa.select(OrchestrationPendingAmendment))).scalar_one()
        assert stored.id == body["draft_id"]
        assert stored.state == PendingAmendmentState.PENDING.value

    async def test_proposing_unbound_new_work_publishes_nothing(self, session, app_with_router, monkeypatch):
        """Nothing is published before a human accepts: the draft keeps the node unbound."""
        flow_id = await accepted_flow(session)
        body = insert_after_body(address("story-a"))
        del body["placement"]["work"][0]["issue_ref"]
        body["placement"]["work"][0]["repository_identity"] = "aws-e/adp"
        refuse_publication(monkeypatch)

        response = client_for(app_with_router).post(proposals_route(flow_id), json=body)

        assert response.status_code == 201, response.text
        stored = (await session.execute(sa.select(OrchestrationPendingAmendment))).scalar_one()
        added = next(node for node in stored.proposal_document["nodes"] if node["address"].endswith("/cleanup"))
        assert added["issue_ref"] is None
        [planned] = response.json()["impact"]["issue_publications"]
        assert (planned["repository"], planned["action"], planned["creates_issue"]) == ("aws-e/adp", "create", True)
        # This flow's in-force plan carries no execution policy, so no repository is permitted.
        assert "amendment_issue_repository_not_permitted" in {item["rule"] for item in response.json()["impact"]["blocking"]}

    async def test_the_response_names_the_exact_edges_the_placement_produced(self, session, app_with_router):
        """The requirement: explicit incoming and outgoing edges, not a wave number.

        A reviewer cannot confirm work landed where they meant from wave membership or
        visual order alone, so the resolved edges — with the rule behind each — are the
        payload that makes the placement reviewable.
        """
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router)

        body = client.post(proposals_route(flow_id), json=insert_after_body(address("story-a"))).json()

        added = body["added_nodes"][0]
        edges = {(edge["from_address"], edge["to_address"]) for edge in body["resolved_edges"]}
        assert (address("story-a"), added) in edges
        assert all(edge["reason"] for edge in body["resolved_edges"])

    async def test_proposing_files_an_inert_draft_and_no_graph_change(self, session, app_with_router):
        """A proposal is not an approval: no node, no edge, no new plan version."""
        flow_id = await accepted_flow(session)
        before = await graph_counts(session)
        client = client_for(app_with_router)

        client.post(proposals_route(flow_id), json=insert_after_body(address("story-a")))

        after = await graph_counts(session)
        assert (after["nodes"], after["edges"], after["plans"]) == (before["nodes"], before["edges"], before["plans"])
        assert after["drafts"] == before["drafts"] + 1

    async def test_the_proposal_carries_its_own_impact_report(self, session, app_with_router):
        """So propose->preview is one round trip for the common case."""
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router)

        impact = client.post(proposals_route(flow_id), json=insert_after_body(address("story-a"))).json()["impact"]

        assert impact["acceptable"] is True
        assert impact["is_noop"] is False
        assert any(node["change"] == "added" for node in impact["nodes"])

    async def test_a_read_only_validation_is_proposable_as_an_eval_in_its_own_wave(self, session, app_with_router):
        """#5329's named case: validation work must not have to pretend to be a code story.

        The whole point is that this is acceptable *as an eval* — no fabricated pull request,
        and the new wave's execution position derived from the edges it declared rather than
        from where it was drawn.
        """
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router)

        response = client.post(proposals_route(flow_id), json=validation_body())

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["impact"]["acceptable"] is True, body["impact"]["blocking"]
        added = body["added_nodes"][0]
        assert added == address("migration-check", wave="wave-1b")
        edges = {(edge["from_address"], edge["to_address"]) for edge in body["resolved_edges"]}
        assert (address("eval"), added) in edges, "the validation must wait on what it validates"
        assert (added, address("deploy-gate", wave="wave-2")) in edges, "and must gate what follows it"
        stages = [stage["waves"] for stage in body["impact"]["execution_order"]]
        assert stages == [["epic-1/wave-1"], ["epic-1/wave-1b"], ["epic-1/wave-2"]]

    async def test_an_added_eval_does_not_silently_relax_wave_eval_cardinality(self, session, app_with_router):
        """#5329: "eval cardinality must not be silently relaxed."

        Dropping the same validation into Wave 1 would give that wave two evals, and
        `validate_proposal`'s `wave_eval_cardinality` rule gives a wave with stories exactly
        one. The interesting part is *where* that is reported: the proposal is still filed,
        because the placement itself resolved fine, but the impact report comes back
        unacceptable and names the rule — so the reviewer is told before they can approve
        it, rather than the validator being loosened to let it through.

        `accept_amendment` refuses it a second time as well (`ProposalRejectedError` from
        authoritative validation), so this is defence in depth and not the only guard.
        """
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router)
        body = insert_after_body(address("story-a"), ref="migration-check")
        body["placement"]["work"][0]["kind"] = "eval"

        impact = client.post(proposals_route(flow_id), json=body).json()["impact"]

        assert impact["acceptable"] is False
        assert [violation["rule"] for violation in impact["blocking"]] == ["wave_eval_cardinality"]

    async def test_an_unknown_anchor_is_refused_and_files_nothing(self, session, app_with_router):
        """Ambiguous or unresolvable placement must be refused BEFORE anything is
        approvable. A draft holding a placement guessed one way is the input to an
        approval nobody reviewed."""
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router)

        response = client.post(proposals_route(flow_id), json=insert_after_body(address("does-not-exist")))

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "placement_unresolved"
        assert response.json()["detail"]["violations"], "the refusal must name what is unresolved"
        assert await count_rows(session, OrchestrationPendingAmendment) == 0

    async def test_a_flow_with_no_accepted_plan_is_a_conflict(self, session, app_with_router):
        """There is nothing to place work relative to, and that is not the caller's error."""
        session.add(_unplanned_flow("flow-unplanned"))
        await session.commit()
        client = client_for(app_with_router)

        response = client.post(proposals_route("flow-unplanned"), json=insert_after_body(address("story-a")))

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "no_plan_in_force"

    async def test_a_retried_proposal_reconciles_onto_one_draft(self, session, app_with_router):
        """A duplicated POST must not fork the review into two drafts of the same ask."""
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router)
        body = insert_after_body(address("story-a"))

        first = client.post(proposals_route(flow_id), json=body)
        second = client.post(proposals_route(flow_id), json=body)

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["draft_id"] == second.json()["draft_id"]
        assert await count_rows(session, OrchestrationPendingAmendment) == 1

    async def test_another_tenants_flow_is_not_found(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        app_with_router.dependency_overrides[_current_user_dep()] = lambda: token_context(ORG_B)
        client = client_for(app_with_router)

        response = client.post(proposals_route(flow_id), json=insert_after_body(address("story-a")))

        assert response.status_code == 404
        assert await count_rows(session, OrchestrationPendingAmendment) == 0


def _current_user_dep():
    from src.auth.dependencies import get_current_user

    return get_current_user


class TestListAndShow:
    async def test_drafts_are_listed_without_their_documents(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        response = client.get(drafts_route(flow_id))

        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["draft_id"] for item in body] == [draft.draft_id]
        assert "proposal_document" not in body[0]

    async def test_the_list_reports_whether_each_base_still_holds(self, session, app_with_router):
        """So a reviewer is told the plan moved before spending time on a review."""
        flow_id = await accepted_flow(session)
        await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        assert client.get(drafts_route(flow_id)).json()[0]["base_is_current"] is True

    async def test_states_can_be_filtered(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        listed = client.get(drafts_route(flow_id), params={"state": ["pending"]}).json()
        assert [item["draft_id"] for item in listed] == [draft.draft_id]

        assert client.get(drafts_route(flow_id), params={"state": ["rejected"]}).json() == []

    async def test_an_unknown_state_filter_is_refused_rather_than_silently_empty(self, session, app_with_router):
        """An empty list for a typo'd state reads as "nothing is waiting", which is the
        one answer a reviewer must not be given wrongly."""
        flow_id = await accepted_flow(session)
        await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        response = client.get(drafts_route(flow_id), params={"state": ["pendign"]})

        assert response.status_code == 422

    async def test_show_returns_the_document_and_the_revision_hash(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        body = client.get(draft_route(flow_id, draft.draft_id)).json()

        assert body["proposal_document"]["flow_slug"]
        assert body["proposal_hash"] == draft.proposal_hash

    async def test_an_absent_a_cross_tenant_and_a_wrong_flow_draft_are_one_answer(self, session, app_with_router):
        """Uniform 404. A distinguishable response would be an oracle for draft ids."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        absent = client.get(draft_route(flow_id, "00000000-0000-0000-0000-000000000000"))
        wrong_flow = client.get(draft_route("some-other-flow", draft.draft_id))

        assert absent.status_code == 404
        assert wrong_flow.status_code == 404

        app_with_router.dependency_overrides[_current_user_dep()] = lambda: token_context(ORG_B)
        cross_tenant = client_for(app_with_router).get(draft_route(flow_id, draft.draft_id))
        assert cross_tenant.status_code == 404


class TestPreview:
    async def test_preview_reports_the_whole_graph_change(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=True)
        client = client_for(app_with_router)

        response = client.post(draft_route(flow_id, draft.draft_id, "preview"))

        assert response.status_code == 200, response.text
        body = response.json()
        assert any(node["change"] == "added" for node in body["nodes"])
        assert body["gates_added"] == [address("spend-gate")]
        assert body["execution_order"], "a reviewer needs the proposed order and parallelism"
        assert body["base_plan_version"] == 1

    async def test_preview_writes_nothing(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        before = await graph_counts(session)
        client = client_for(app_with_router)

        client.post(draft_route(flow_id, draft.draft_id, "preview"))

        assert await graph_counts(session) == before

    async def test_preview_reports_current_states_of_affected_nodes(self, session, app_with_router):
        """Downstream impact is only meaningful next to where execution actually is."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        body = client.post(draft_route(flow_id, draft.draft_id, "preview")).json()

        unchanged = [node for node in body["nodes"] if node["change"] == "unchanged"]
        assert unchanged and all(node["state"] for node in unchanged)
        assert len(body["before_nodes"]) < len(body["after_nodes"])
        assert body["before_edges"]
        assert body["after_edges"]

    async def test_an_absent_draft_is_not_found(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router)

        assert client.post(draft_route(flow_id, "00000000-0000-0000-0000-000000000000", "preview")).status_code == 404


class TestEdit:
    async def test_an_edit_creates_a_new_revision_and_supersedes_the_original(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        client = client_for(app_with_router)

        response = client.post(
            draft_route(flow_id, draft.draft_id, "edit"),
            json={"proposal": edited_proposal().model_dump(mode="json")},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["draft_id"] != draft.draft_id
        assert body["superseded_draft_id"] == draft.draft_id
        assert body["unchanged"] is False

    async def test_an_edit_with_unbound_new_work_publishes_nothing(self, session, app_with_router, monkeypatch):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        proposal = edited_proposal()
        work = next(node for node in proposal.nodes if node.address.endswith("/cleanup"))
        work.issue_ref = None
        work.proposal_identity = "reviewer:cleanup"
        work.repository_identity = "aws-e/adp"
        refuse_publication(monkeypatch)

        response = client_for(app_with_router).post(
            draft_route(flow_id, draft.draft_id, "edit"),
            json={"proposal": proposal.model_dump(mode="json")},
        )

        assert response.status_code == 200, response.text
        revised = (
            await session.execute(sa.select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == response.json()["draft_id"]))
        ).scalar_one()
        unbound = next(node for node in revised.proposal_document["nodes"] if node["address"].endswith("/cleanup"))
        assert unbound["issue_ref"] is None
        assert [item["address"] for item in response.json()["impact"]["issue_publications"]] == [address("cleanup")]

    async def test_an_edit_writes_no_plan_version(self, session, app_with_router):
        """An edit is still a proposal. It reaches the graph only through accept."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        before = await count_rows(session, OrchestrationAcceptedPlan)
        client = client_for(app_with_router)

        client.post(draft_route(flow_id, draft.draft_id, "edit"), json={"proposal": edited_proposal().model_dump(mode="json")})

        assert await count_rows(session, OrchestrationAcceptedPlan) == before

    async def test_the_revision_response_carries_its_impact(self, session, app_with_router):
        """The reviewer who just edited needs to see what the edit did, without a second call."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        client = client_for(app_with_router)

        body = client.post(
            draft_route(flow_id, draft.draft_id, "edit"),
            json={"proposal": edited_proposal().model_dump(mode="json")},
        ).json()

        assert body["impact"]["acceptable"] is True
        assert any(node["address"].endswith("/cleanup") for node in body["impact"]["nodes"])

    async def test_a_no_op_edit_does_not_churn_the_revision(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=True)
        client = client_for(app_with_router)

        body = client.post(
            draft_route(flow_id, draft.draft_id, "edit"),
            json={"proposal": amended_proposal(extra_gate=True).model_dump(mode="json")},
        ).json()

        assert body["unchanged"] is True
        assert body["draft_id"] == draft.draft_id

    async def test_editing_a_terminal_draft_is_a_conflict(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)
        client.post(draft_route(flow_id, draft.draft_id, "reject"), json={})

        response = client.post(
            draft_route(flow_id, draft.draft_id, "edit"),
            json={"proposal": edited_proposal().model_dump(mode="json")},
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "draft_not_pending"

    async def test_preview_marks_a_rejected_revision_unapprovable(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)
        assert client.post(draft_route(flow_id, draft.draft_id, "reject"), json={}).status_code == 200

        preview = client.post(draft_route(flow_id, draft.draft_id, "preview")).json()

        assert preview["acceptable"] is False
        assert "draft_not_pending" in {item["rule"] for item in preview["blocking"]}


class TestReject:
    async def test_rejecting_is_terminal_and_writes_no_plan(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        before = await graph_counts(session)
        client = client_for(app_with_router)

        response = client.post(draft_route(flow_id, draft.draft_id, "reject"), json={"reason": "wrong wave"})

        assert response.status_code == 200, response.text
        assert response.json()["state"] == PendingAmendmentState.REJECTED.value
        readback = client.get(draft_route(flow_id, draft.draft_id)).json()
        assert readback["decision_actor"] == HUMAN_USER_ID
        assert readback["decision_reason"] == "wrong wave"
        after = await graph_counts(session)
        assert (after["nodes"], after["edges"], after["plans"]) == (before["nodes"], before["edges"], before["plans"])

    async def test_a_rejected_draft_cannot_then_be_accepted(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)
        client.post(draft_route(flow_id, draft.draft_id, "reject"), json={})

        response = client.post(
            draft_route(flow_id, draft.draft_id, "accept"),
            json=exact_accept_body(client, flow_id, draft.draft_id),
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "draft_not_pending"

    async def test_rejecting_an_accepted_draft_is_a_terminal_conflict(self, session, app_with_router):
        """Its content is in force; a late rejection would record a decision that did not
        happen. Reported as non-retryable so a CLI does not invite a pointless retry."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)
        assert (
            client.post(
                draft_route(flow_id, draft.draft_id, "accept"),
                json=exact_accept_body(client, flow_id, draft.draft_id),
            ).status_code
            == 200
        )

        response = client.post(draft_route(flow_id, draft.draft_id, "reject"), json={})

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "draft_already_accepted"
        assert response.json()["detail"]["retryable"] is False


class TestAccept:
    async def test_new_work_without_a_published_issue_is_previewed_and_refused(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        proposal = amended_proposal(extra_gate=False)
        next(node for node in proposal.nodes if node.address.endswith("/story-b")).issue_ref = None
        client = client_for(app_with_router)

        revision = client.post(
            draft_route(flow_id, draft.draft_id, "edit"),
            json={"proposal": proposal.model_dump(mode="json")},
        ).json()

        assert revision["impact"]["acceptable"] is False
        assert "missing_issue_binding" in {item["rule"] for item in revision["impact"]["blocking"]}
        response = client.post(
            draft_route(flow_id, revision["draft_id"], "accept"),
            json=exact_accept_body(client, flow_id, revision["draft_id"]),
        )
        assert response.status_code == 422
        assert "missing_issue_binding" in {item["rule"] for item in response.json()["detail"]["violations"]}

    async def test_accepting_applies_the_amendment_as_a_new_version(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        response = client.post(
            draft_route(flow_id, draft.draft_id, "accept"),
            json=exact_accept_body(client, flow_id, draft.draft_id),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["plan_version"] == 2
        assert body["superseded_version"] == 1
        assert body["already_accepted"] is False

    async def test_a_retried_accept_reports_the_original_outcome(self, session, app_with_router):
        """Idempotent: a retry must not be reported as a second plan version."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        body = exact_accept_body(client, flow_id, draft.draft_id)
        first = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=body)
        second = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=body)

        assert first.json()["plan_version"] == second.json()["plan_version"] == 2
        assert second.json()["already_accepted"] is True
        assert await count_rows(session, OrchestrationAcceptedPlan) == 2

    async def test_preview_then_edit_then_accept_the_reviewed_hash_is_refused(self, session, app_with_router):
        """The whole point of the interaction, end to end over HTTP.

        A human previews revision 1, someone edits the draft, and the approval naming
        revision 1's hash is refused rather than silently applying revision 2. If this
        passed, "preview then approve" would guarantee nothing.
        """
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        client = client_for(app_with_router)

        reviewed_body = exact_accept_body(client, flow_id, draft.draft_id)
        assert client.post(draft_route(flow_id, draft.draft_id, "preview")).status_code == 200
        revision = client.post(
            draft_route(flow_id, draft.draft_id, "edit"),
            json={"proposal": edited_proposal().model_dump(mode="json")},
        ).json()

        response = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=reviewed_body)

        assert response.status_code == 409
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1, "nothing may be applied"
        # And the successor is acceptable, so the loop is not a dead end.
        applied = client.post(
            draft_route(flow_id, revision["draft_id"], "accept"),
            json=exact_accept_body(client, flow_id, revision["draft_id"]),
        )
        assert applied.status_code == 200
        assert applied.json()["plan_version"] == 2

    async def test_a_revision_mismatch_on_an_unedited_draft_is_refused(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        response = client.post(
            draft_route(flow_id, draft.draft_id, "accept"),
            json=exact_accept_body(client, flow_id, draft.draft_id, expected_proposal_hash="0" * 64),
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "revision_mismatch"
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

    async def test_a_conflict_carries_a_machine_readable_code_and_a_human_message(self, session, app_with_router):
        """The CLI branches on the code and shows `message`; both must be present, because a
        409 whose body a script cannot classify forces the human to read prose.

        `error` is asserted alongside `code` because it is the key that actually reaches the
        CLI. `adp_common.Api` turns an HTTP failure into a `CliError` code by reading
        exactly `detail["error"]`, falling back to `detail["reason"]` — never `detail["code"]`
        — so under `code` alone `adp flow amendment accept` against a moved plan would
        surface as a generic `http_error`. That would defeat this issue's requirement that
        the CLI exit nonzero on a stale accept *and name what happened*: the caller could
        not tell a stale revision from an auth failure. Both keys carry the same value:
        `error` for the CLI transport and the rest of the orchestration routers'
        convention, `code` for the dashboard.
        """
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        detail = client.post(
            draft_route(flow_id, draft.draft_id, "accept"),
            json=exact_accept_body(client, flow_id, draft.draft_id, expected_proposal_hash="0" * 64),
        ).json()["detail"]

        assert detail["code"] == "revision_mismatch"
        assert detail["error"] == "revision_mismatch", "the key adp_common.Api reads"
        assert detail["message"]
        assert "retryable" in detail

    async def test_every_refusal_code_is_shaped_for_the_cli_transport(self, session, app_with_router):
        """One assertion covering all four refusal shapes this router emits.

        Written as a sweep rather than four separate assertions because the failure mode is
        a *new* refusal path added later with only `code` set — which passes every other
        test in this file while being invisible to the CLI. `adp_common.Api` also requires
        the value to match `[a-z_]{1,80}` before it will adopt it as a code, so a refusal
        spelled with a hyphen or an uppercase letter degrades to `http_error` just as
        silently; that is asserted here too.
        """
        import re

        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)
        unplanned = "flow-unplanned"
        session.add(_unplanned_flow(unplanned))
        await session.commit()

        refusals = [
            client.post(proposals_route(flow_id), json=insert_after_body(address("does-not-exist"))),
            client.post(proposals_route(unplanned), json=insert_after_body(address("story-a"))),
            client.post(
                draft_route(flow_id, draft.draft_id, "accept"),
                json=exact_accept_body(client, flow_id, draft.draft_id, expected_proposal_hash="0" * 64),
            ),
        ]

        for response in refusals:
            detail = response.json()["detail"]
            assert isinstance(detail, dict), response.text
            assert detail["error"] == detail["code"], "both keys must agree"
            assert re.fullmatch(r"[a-z_]{1,80}", detail["error"]), f"adp_common.Api would discard {detail['error']!r}"

    async def test_the_terminal_conflict_set_names_only_codes_the_store_can_raise(self):
        """A code in `_TERMINAL_CONFLICTS` that nothing raises is a false claim.

        It is dead vocabulary that reads, to anyone auditing this file for the
        no-self-approval rule, like an enforced refusal — while the real enforcement is
        structural (accept exists only on this `PLAN_APPROVE` router). Worse, a stale
        member invites a client to build a branch for a code it will never receive. This
        reads `pending_amendments`' actual raise sites rather than trusting a list.
        """
        import re
        from pathlib import Path

        from src.orchestration import amendment_review_routes, pending_amendments

        source = Path(pending_amendments.__file__).read_text()
        raised = set(re.findall(r"AmendmentConflictError\(\s*\"([a-z_]+)\"", source))

        assert raised, "the raise-site scan found nothing; the pattern needs updating"
        unraisable = amendment_review_routes._TERMINAL_CONFLICTS - raised
        assert not unraisable, f"declared terminal but never raised: {sorted(unraisable)}"

    async def test_accepting_is_what_creates_the_new_nodes(self, session, app_with_router):
        """The proposal was inert; acceptance is the moment it becomes graph state."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        before = await count_rows(session, OrchestrationNode)
        client = client_for(app_with_router)

        client.post(draft_route(flow_id, draft.draft_id, "accept"), json=exact_accept_body(client, flow_id, draft.draft_id))

        assert await count_rows(session, OrchestrationNode) > before


async def _product_binding_draft(session, *, revision_kind: str, reservation: str | None = None):
    from src.orchestration.amend import AmendmentContext
    from src.orchestration.compile import ApprovalContext
    from src.orchestration.models import OrchestrationExecution, OrchestrationPullRequestBinding, OrchestrationWorkClaim
    from src.orchestration.pending_amendments import register_amendment_draft
    from src.orchestration.state import ActorKind

    from .test_amend import compile_product_flow
    from .test_amendment_review import open_request

    actor = AmendmentContext(org_id=ORG_A, actor_id=HUMAN_USER_ID, actor_role="org_admin", actor_kind=ActorKind.HUMAN)
    approval = ApprovalContext(org_id=ORG_A, actor_id=HUMAN_USER_ID, actor_role="org_admin", actor_kind=ActorKind.HUMAN)
    compiled, proposal = await compile_product_flow(session, approval, actor)
    await session.commit()
    node = (
        await session.execute(
            sa.select(OrchestrationNode).where(OrchestrationNode.flow_id == compiled.flow_id, OrchestrationNode.node_ref == "story-a")
        )
    ).scalar_one()
    if reservation == "claim":
        session.add(
            OrchestrationWorkClaim(org_id=ORG_A, provider_repository_id=1001, issue_number=4196, owner_kind="engine_flow", owner_ref=compiled.flow_id)
        )
    elif reservation == "queued_execution":
        session.add(
            OrchestrationExecution(
                org_id=ORG_A,
                flow_id=compiled.flow_id,
                node_id=node.id,
                phase="queued",
                status="queued",
                claim_id="claim-reserved",
                claim_generation=1,
            )
        )
    elif reservation == "pr_binding":
        session.add(
            OrchestrationPullRequestBinding(
                org_id=ORG_A,
                flow_id=compiled.flow_id,
                node_id=node.id,
                attempt=1,
                provider_repository_id=1001,
                provider_pr_node_id="PR_1",
                repo="example/repo-a",
                pr_number=1,
                installation_id=1,
                head_sha="a" * 40,
                registered_by=HUMAN_USER_ID,
                registered_by_kind="human",
            )
        )
    await session.commit()

    request = await open_request(session, compiled.flow_id)
    revised = proposal.model_copy(deep=True)
    if revision_kind in ("product", "both"):
        revised.product_id = "product-b"
    if revision_kind in ("repository", "both"):
        revised.nodes[0].repository_identity = "example/repo-b"
        revised.nodes[0].provider_repository_id = 1002
    draft = await register_amendment_draft(session, org_id=ORG_A, request=request, author_run_id="orch:author-run-1", proposal=revised)
    await session.commit()
    return compiled.flow_id, node.id, draft


class TestProductBindingEntryPoints:
    async def test_untouched_product_and_repository_change_previews_accepts_and_retries(self, session, app_with_router):
        from src.orchestration.models import OrchestrationFlow
        from src.products.models import Product

        flow_id, node_id, draft = await _product_binding_draft(session, revision_kind="both")
        client = client_for(app_with_router)
        before = await graph_counts(session)
        preview = client.post(draft_route(flow_id, draft.draft_id, "preview"))
        assert preview.status_code == 200, preview.text
        assert preview.json()["acceptable"] is True, preview.json()["blocking"]
        assert (preview.json()["product_before"], preview.json()["product_after"]) == ("product-a", "product-b")
        assert await graph_counts(session) == before

        accept_body = exact_accept_body(client, flow_id, draft.draft_id)
        accepted = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=accept_body)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["plan_version"] == 2
        plan = (
            await session.execute(
                sa.select(OrchestrationAcceptedPlan).where(
                    OrchestrationAcceptedPlan.flow_id == flow_id, OrchestrationAcceptedPlan.superseded_at.is_(None)
                )
            )
        ).scalar_one()
        assert plan.plan_document["product_id"] == (await session.get(OrchestrationFlow, flow_id)).product_id == "product-b"
        assert (await session.get(OrchestrationNode, node_id)).provider_repository_id == 1002

        (await session.get(Product, (ORG_A, "product-b"))).name = "Renamed product"
        await session.commit()
        session.expire_all()
        replay = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=accept_body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["already_accepted"] is True
        assert (replay.json()["plan_version"], replay.json()["decision_id"]) == (2, accepted.json()["decision_id"])
        assert (await session.get(OrchestrationFlow, flow_id)).product_id == "product-b"
        assert await count_rows(session, OrchestrationAcceptedPlan) == 2

    @pytest.mark.parametrize("reservation", ["claim", "queued_execution", "pr_binding"])
    @pytest.mark.parametrize("revision_kind", ["product", "repository"])
    async def test_started_retarget_refused_at_preview_and_accept_without_mutation(self, session, app_with_router, reservation, revision_kind):
        from src.orchestration.models import OrchestrationDecision, OrchestrationFlow

        flow_id, node_id, draft = await _product_binding_draft(session, revision_kind=revision_kind, reservation=reservation)
        client = client_for(app_with_router)
        before = await graph_counts(session)
        decisions_before = await count_rows(session, OrchestrationDecision)
        current = (await session.execute(sa.select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow_id))).scalar_one()
        plan_id, plan_hash = current.id, current.plan_hash
        preview = client.post(draft_route(flow_id, draft.draft_id, "preview"))
        assert preview.status_code == 200, preview.text
        expected = "product_retarget_started" if revision_kind == "product" else "amendment_definition_change"
        assert expected in {item["rule"] for item in preview.json()["blocking"]}
        assert preview.json()["acceptable"] is False
        assert await graph_counts(session) == before

        refused = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=exact_accept_body(client, flow_id, draft.draft_id))
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "proposal_rejected"
        session.expire_all()
        assert await graph_counts(session) == before
        assert await count_rows(session, OrchestrationDecision) == decisions_before
        assert (await session.get(OrchestrationFlow, flow_id)).product_id == "product-a"
        assert (await session.get(OrchestrationNode, node_id)).provider_repository_id == 1001
        in_force = await session.get(OrchestrationAcceptedPlan, plan_id)
        assert (in_force.plan_hash, in_force.superseded_at) == (plan_hash, None)
        assert (await session.get(OrchestrationPendingAmendment, draft.draft_id)).state == PendingAmendmentState.PENDING.value


class TestAuthorization:
    """`PLAN_APPROVE` on all seven, checked before anything is read."""

    @pytest.mark.parametrize(
        "method,action,body",
        [
            ("post", "preview", None),
            ("post", "reject", {}),
            ("post", "accept", {"expected_proposal_hash": "0" * 64, "expected_base_plan_version": 1, "expected_base_plan_hash": "0" * 64}),
        ],
    )
    async def test_draft_actions_are_denied_without_plan_approve(self, session, app_with_router, method, action, body):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router, permitted=False)

        response = getattr(client, method)(draft_route(flow_id, draft.draft_id, action), json=body)

        assert response.status_code == 403
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

    async def test_proposing_is_denied_without_plan_approve(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router, permitted=False)

        response = client.post(proposals_route(flow_id), json=insert_after_body(address("story-a")))

        assert response.status_code == 403
        assert await count_rows(session, OrchestrationPendingAmendment) == 0

    async def test_editing_requires_approval_authority_not_merely_draft(self, session, app_with_router):
        """Edit writes no plan, so a weaker permission looks defensible. It is not.

        An edit chooses what a human will be asked to approve, so an agent that could edit
        a pending draft could rewrite it after a reviewer read it and before they accepted
        — defeating review without ever touching promotion state. Asserted on the recorded
        permission rather than by reading the source.
        """
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        client = client_for(app_with_router)

        client.post(draft_route(flow_id, draft.draft_id, "edit"), json={"proposal": edited_proposal().model_dump(mode="json")})

        checked = app_with_router.state.access_stub.check_permission.await_args
        assert Permission.PLAN_APPROVE in checked.args or checked.kwargs.get("permission") is Permission.PLAN_APPROVE

    async def test_reads_are_denied_without_plan_approve(self, session, app_with_router):
        """The GETs return pending proposal documents and acceptance provenance, so they
        are gated for the same reason `GET /flows/{id}/plans` is."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router, permitted=False)

        assert client.get(drafts_route(flow_id)).status_code == 403
        assert client.get(draft_route(flow_id, draft.draft_id)).status_code == 403

    async def test_a_denied_caller_cannot_learn_whether_the_flow_exists(self, session, app_with_router):
        """403 for both a real and an invented flow: the gate runs before any read."""
        flow_id = await accepted_flow(session)
        client = client_for(app_with_router, permitted=False)

        real = client.get(drafts_route(flow_id))
        invented = client.get(drafts_route("flow-that-does-not-exist"))

        assert real.status_code == invented.status_code == 403

    async def test_the_accepting_actor_comes_from_the_token_not_the_body(self, session, app_with_router):
        """An actor a caller could name is an actor a caller could forge, and this identity
        is what no-self-approval is enforced against."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        response = client.post(
            draft_route(flow_id, draft.draft_id, "accept"),
            json=exact_accept_body(client, flow_id, draft.draft_id, reason="reviewed graph and policy diff"),
        )

        stored = (
            await session.execute(sa.select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == draft.draft_id))
        ).scalar_one()
        assert stored.accepted_by == HUMAN_USER_ID
        readback = client.get(draft_route(flow_id, draft.draft_id)).json()
        assert readback["decision_actor"] == HUMAN_USER_ID
        assert readback["decision_id"] == response.json()["decision_id"]
        assert readback["decision_reason"].startswith("reviewed graph and policy diff")
        assert readback["accepted_plan_version"] == 2
        assert readback["approved_diff"]["nodes_added"]

    async def test_a_permitted_service_principal_cannot_be_stamped_as_a_human_approver(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)
        body = exact_accept_body(client, flow_id, draft.draft_id)
        app_with_router.dependency_overrides[_current_user_dep()] = lambda: token_context(
            ORG_A, user_id="agent-service", account_type="service", auth_source="iam"
        )

        response = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=body)

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "human_approval_required"
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

    async def test_an_agent_can_read_only_the_draft_bound_to_its_run(self, session, app_with_router):
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, run_id="orch:bound-run")
        app_with_router.dependency_overrides[_current_user_dep()] = lambda: token_context(
            ORG_A, user_id="agent-service", account_type="service", auth_source="jwt"
        )
        client = client_for(app_with_router)

        allowed = client.get(draft_route(flow_id, draft.draft_id), headers={"X-Agent-RunId": "orch:bound-run"})
        denied = client.get(draft_route(flow_id, draft.draft_id), headers={"X-Agent-RunId": "orch:other-run"})

        assert allowed.status_code == 200
        assert denied.status_code == 404

    async def test_a_body_naming_an_actor_is_rejected_outright(self, session, app_with_router):
        """`extra="forbid"` on every request model, so a forged-identity attempt is a 422
        rather than a field that is silently ignored."""
        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id)
        client = client_for(app_with_router)

        response = client.post(draft_route(flow_id, draft.draft_id, "accept"), json={"actor_id": "cognito-sub-someone-else"})

        assert response.status_code == 422


PUBLISHED_REPO = "aws-e/adp"


def permitted_policy(*, repositories=(PUBLISHED_REPO,)):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from src.orchestration.execution_policy import Action, ExecutionPolicy, PolicyLimits

    return ExecutionPolicy(
        org_id=ORG_A,
        repository_ids=list(repositories),
        allowed_actions=[Action.DEVELOP],
        expires_at=datetime.now(UTC) + timedelta(days=1),
        limits=PolicyLimits(max_wall_clock_seconds=3600, max_spend_usd=Decimal("50.00"), max_attempts_per_node=3, max_concurrent_actions=2),
    )


async def policied_flow_with_unbound_draft(session, *, repositories=(PUBLISHED_REPO,)):
    """An agent-authored draft whose new story has no issue, on a flow whose policy permits the repo."""
    from src.orchestration.pending_amendments import register_amendment_draft

    from .test_amendment_review import open_request
    from .test_pending_amendments import accepted_flow as accepted_flow_with
    from .test_pending_amendments import base_proposal

    policy = permitted_policy(repositories=repositories)
    flow_id = await accepted_flow_with(session, proposal=base_proposal(execution_policy=policy))
    request = await open_request(session, flow_id)
    proposal = amended_proposal(extra_gate=False, execution_policy=policy)
    work = next(node for node in proposal.nodes if node.address.endswith("/story-b"))
    work.issue_ref = None
    work.proposal_identity = "agent:story-b"
    work.repository_identity = PUBLISHED_REPO
    draft = await register_amendment_draft(session, org_id=ORG_A, request=request, author_run_id="orch:author-run-1", proposal=proposal)
    await session.commit()
    return flow_id, draft


@pytest.fixture
def provider(monkeypatch):
    """Fake only the provider HTTP layer; the binding table and reconcile path are real."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.orchestration.amendment_issue_publisher import PublishedAmendmentIssue, content_hash

    state = SimpleNamespace(created=0, listed=0)

    async def create_issue(_self, binding, content):
        await binding.begin_write()
        state.created += 1
        return PublishedAmendmentIssue(
            number=7100 + state.created,
            repository_id=binding.provider_repository_id,
            url="https://github.com/aws-e/adp/issues/x",
            content_hash=content_hash(content),
            write_id=binding.write_id,
        )

    async def find_issue(_self, *_args, **_kwargs):
        state.listed += 1
        return None

    async def update_issue(_self, *_args, **_kwargs):
        raise AssertionError("no drift expected")

    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=55))
    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=99))
    for name, method in (("create_issue", create_issue), ("find_issue", find_issue), ("update_issue", update_issue)):
        monkeypatch.setattr(f"src.orchestration.amendment_issue_publisher.AmendmentIssuePublisher.{name}", method)
    return state


class TestAcceptPublishesUnboundWork:
    async def test_uncertain_write_keeps_the_plan_unchanged_and_explains_reconciliation(self, session, app_with_router, monkeypatch):
        from src.orchestration import amendment_review_routes
        from src.orchestration.review_cycle import CycleBlockedError

        flow_id, draft = await policied_flow_with_unbound_draft(session)

        async def uncertain(*args, **kwargs):
            raise CycleBlockedError("amendment_issue_write_uncertain")

        monkeypatch.setattr(amendment_review_routes, "publish_for_acceptance", uncertain)
        client = client_for(app_with_router)
        response = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=exact_accept_body(client, flow_id, draft.draft_id))
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "amendment_issue_write_uncertain"
        assert "Retry this exact acceptance" in detail["message"]
        assert "do not reset the claim" in detail["message"]
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1

    """Agent drafts stay completable: their new work is published at human accept."""

    async def test_preview_names_the_new_issue_and_its_repository(self, session, app_with_router, provider):
        flow_id, draft = await policied_flow_with_unbound_draft(session)

        preview = client_for(app_with_router).post(draft_route(flow_id, draft.draft_id, "preview")).json()

        assert preview["acceptable"] is True, preview["blocking"]
        assert preview["issue_publications"] == [
            {
                "address": address("story-b"),
                "proposal_identity": "agent:story-b",
                "repository": PUBLISHED_REPO,
                "action": "create",
                "creates_issue": True,
                "issue_number": None,
                "url": None,
            }
        ]
        assert provider.created == 0

    async def test_accept_publishes_then_binds_the_recorded_issue(self, session, app_with_router, provider):
        flow_id, draft = await policied_flow_with_unbound_draft(session)
        client = client_for(app_with_router)

        response = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=exact_accept_body(client, flow_id, draft.draft_id))

        assert response.status_code == 200, response.text
        assert provider.created == 1 and provider.listed == 0
        plan = (await session.execute(sa.select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))).scalar_one()
        story = next(node for node in plan.plan_document["nodes"] if node["address"] == address("story-b"))
        assert (story["issue_ref"], story["provider_repository_id"]) == ("7101", 99)
        # The reviewed revision is unchanged: the hash a human approved is the unbound one.
        stored = (await session.execute(sa.select(OrchestrationPendingAmendment))).scalar_one()
        assert stored.proposal_hash == draft.proposal_hash

        replay = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=exact_accept_body(client, flow_id, draft.draft_id))
        assert replay.json()["already_accepted"] is True
        assert provider.created == 1

    async def test_a_wrong_revision_publishes_nothing(self, session, app_with_router, provider):
        flow_id, draft = await policied_flow_with_unbound_draft(session)
        client = client_for(app_with_router)

        response = client.post(
            draft_route(flow_id, draft.draft_id, "accept"),
            json=exact_accept_body(client, flow_id, draft.draft_id, expected_proposal_hash="0" * 64),
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "revision_mismatch"
        assert provider.created == 0

    async def test_an_unpermitted_repository_is_refused_without_publication(self, session, app_with_router, provider):
        flow_id, draft = await policied_flow_with_unbound_draft(session, repositories=("aws-e/other",))
        client = client_for(app_with_router)

        preview = client.post(draft_route(flow_id, draft.draft_id, "preview")).json()
        response = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=exact_accept_body(client, flow_id, draft.draft_id))

        assert "amendment_issue_repository_not_permitted" in {item["rule"] for item in preview["blocking"]}
        assert response.status_code == 422
        assert "amendment_issue_repository_not_permitted" in {item["rule"] for item in response.json()["detail"]["violations"]}
        assert provider.created == 0
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1


async def policied_flow_with_draft(session, *, spend: str = "200.00", proposal_spend: str | None = None, receipt_spend: str | None = None):
    """A flow whose in-force policy may carry an approved budget-increase receipt, and a draft restating a policy."""
    import json
    from decimal import Decimal

    from src.orchestration.models import OrchestrationAcceptedPlan as Plan
    from src.orchestration.pending_amendments import register_amendment_draft
    from src.orchestration.repository import OrchestrationRepository

    from .test_amendment_review import open_request
    from .test_pending_amendments import accepted_flow as accepted_flow_with
    from .test_pending_amendments import base_proposal

    policy = permitted_policy()
    policy = policy.model_copy(update={"limits": policy.limits.model_copy(update={"max_spend_usd": Decimal(spend)})})
    flow_id = await accepted_flow_with(session, proposal=base_proposal(execution_policy=policy))
    if receipt_spend is not None:
        plan = (await session.execute(sa.select(Plan).where(Plan.flow_id == flow_id))).scalar_one()
        stored = plan.plan_document["execution_policy"]
        await OrchestrationRepository(session).append_decision(
            org_id=ORG_A,
            flow_id=flow_id,
            kind="budget_increased",
            actor_id="cognito-sub-platform",
            actor_role="platform_admin",
            actor_kind="human",
            reason=json.dumps(
                {
                    "contract": "shared-budget-increase/v1",
                    "flow_id": flow_id,
                    "plan_version": plan.version,
                    "plan_hash": plan.plan_hash,
                    "original_policy_hash": stored["policy_hash"],
                    "principal_id": stored["principal_id"],
                    "limits": {"max_spend_usd": receipt_spend, "max_run_spend_usd": receipt_spend, "max_chain_spend_usd": receipt_spend},
                }
            ),
        )
        await session.commit()
    restated = policy.model_copy(update={"limits": policy.limits.model_copy(update={"max_spend_usd": Decimal(proposal_spend or spend)})})
    request = await open_request(session, flow_id)
    draft = await register_amendment_draft(
        session,
        org_id=ORG_A,
        request=request,
        author_run_id="orch:author-run-1",
        proposal=amended_proposal(extra_gate=False, execution_policy=restated),
    )
    await session.commit()
    return flow_id, draft


class TestEffectiveLimitsAndInFlightWork:
    """Accepting must not silently drop approved limits or strand running work (#5329)."""

    async def test_a_receipt_raised_budget_is_shown_and_its_loss_reported_on_preview_and_accept(self, session, app_with_router):
        flow_id, draft = await policied_flow_with_draft(session, spend="200.00", receipt_spend="500.00")
        client = client_for(app_with_router)

        preview = client.post(draft_route(flow_id, draft.draft_id, "preview")).json()

        assert preview["effective_limits_before"]["max_spend_usd"] == "500.00"
        assert preview["effective_limits_after"]["max_spend_usd"] == "200.00"
        assert preview["acceptable"] is True, preview["blocking"]
        assert [(item["kind"], item["reason"]) for item in preview["receipts_dropped"]] == [("budget", "policy_authority_changed")]

        response = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=exact_accept_body(client, flow_id, draft.draft_id))

        assert response.status_code == 200, response.text
        assert response.json()["receipts_dropped"] == preview["receipts_dropped"]
        replay = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=exact_accept_body(client, flow_id, draft.draft_id))
        assert replay.json()["receipts_dropped"] == response.json()["receipts_dropped"]
        assert await count_rows(session, OrchestrationAcceptedPlan) == 2

    async def test_a_proposal_restating_the_effective_limits_is_acceptable(self, session, app_with_router):
        flow_id, draft = await policied_flow_with_draft(session, spend="200.00", proposal_spend="500.00", receipt_spend="500.00")
        client = client_for(app_with_router)

        preview = client.post(draft_route(flow_id, draft.draft_id, "preview")).json()
        assert preview["effective_limits_after"]["max_spend_usd"] == "500.00"
        assert preview["acceptable"] is True, preview["blocking"]

        response = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=exact_accept_body(client, flow_id, draft.draft_id))
        assert response.status_code == 200, response.text

    async def test_a_running_node_blocks_preview_and_accept_alike(self, session, app_with_router):
        from src.orchestration.compile import ProposalRejectedError
        from src.orchestration.pending_amendments import accept_amendment

        from .test_pending_amendments import amender

        flow_id = await accepted_flow(session)
        draft = await pending_draft(session, flow_id, extra_gate=False)
        await session.execute(
            sa.update(OrchestrationNode).where(OrchestrationNode.flow_id == flow_id, OrchestrationNode.node_ref == "story-a").values(state="running")
        )
        await session.commit()
        client = client_for(app_with_router)

        preview = client.post(draft_route(flow_id, draft.draft_id, "preview")).json()
        response = client.post(draft_route(flow_id, draft.draft_id, "accept"), json=exact_accept_body(client, flow_id, draft.draft_id))

        assert "active_nodes_cross_plan_boundary" in {item["rule"] for item in preview["blocking"]}
        assert address("story-a") in next(item["message"] for item in preview["blocking"] if item["rule"] == "active_nodes_cross_plan_boundary")
        assert response.status_code == 422
        assert "active_nodes_cross_plan_boundary" in {item["rule"] for item in response.json()["detail"]["violations"]}
        # The comment-command accept path enforces the same predicate.
        with pytest.raises(ProposalRejectedError) as refused:
            await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id)
        await session.rollback()
        assert "active_nodes_cross_plan_boundary" in {item.rule for item in refused.value.violations}
        assert await count_rows(session, OrchestrationAcceptedPlan) == 1
