"""Tests for POST /internal/v1/github-installation-token — the GitHub-token gatekeeper.

Issue #4272 (·A-3 Phase 1). The platform GitHub App private key used to be
exported into every agent subprocess so the worker could re-mint before the
1-hour expiry. This route replaces that: the key stays in the gateway, and a run
gets a short-lived token scoped to its OWN installation and the one repo it was
assigned.

Coverage, in order of what actually protects the tenant boundary:
  - 403 when the run's registry row binds a DIFFERENT installation (confused
    deputy — the caller's installation_id is attacker-influenceable)
  - 403 when the row carries NO installation_id (fail-closed on absence; the
    attribute is written conditionally at ingress, so this state is reachable)
  - 403 when the row is missing entirely, and when no invocation_id is sent
  - The binding does NOT consult ENFORCE_CREDENTIAL_BINDING (false on at least
    one live environment — a control gated on it silently shadows)
  - 409 when the installation resolves cleanly to another tenant
  - 200 returns expires_at from GITHUB's response, not a local now+1h guess
  - The mint request is least-privilege: repositories == [this repo] and a
    permissions map no broader than AGENT_RUN_PERMISSIONS
  - Audit row written on mint AND on denial
  - 403 unauthenticated
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.routes import router
from src.knowledge.github_app_service import ReviewerIdentityUnavailableError
from src.orchestration.execution_policy import Action
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_VALID_CALLER = "test-service-principal"

_OWNER_TENANT = "org-acme"
_OTHER_TENANT = "org-globex"
_BOUND_INSTALLATION = 555001
_FOREIGN_INSTALLATION = 555002

_INVOCATION_ID = "evt-abc-123"

# The repository this run is assigned, as recorded on its originating event. The
# request body's repo_owner/repo_name must agree with it (#5663).
_BOUND_REPO = "acme/widgets"
_FOREIGN_REPO = "acme/secrets"

_GITHUB_EXPIRES_AT = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

# Not a key — the mint is stubbed, so this only has to be a distinguishable string.
_FAKE_REVIEW_KEY = "-----BEGIN RSA PRIVATE KEY-----\nreview-not-a-real-key\n-----END RSA PRIVATE KEY-----"


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_engine():
    return create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def engine():
    eng = _make_engine()
    async with eng.begin() as conn:
        import src.shared.models.audit  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        owner = Organization(
            id=_OWNER_TENANT,
            name="Acme",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[str(_BOUND_INSTALLATION)],
            cognito_client_ids=[],
            github_org_id="acme",
        )
        other = Organization(
            id=_OTHER_TENANT,
            name="Globex",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[str(_FOREIGN_INSTALLATION)],
            cognito_client_ids=[],
            github_org_id="globex",
        )
        session.add_all([owner, other])
        # channel_tenant_map is the CORROBORATED record of ownership — written
        # server-side from what GitHub supplied at install. github_installation_ids
        # alone is client-writable, so the resolver treats it as a self-assertion
        # and returns UNATTESTABLE on the unattested path. Both records must exist
        # for a real installation, so both are seeded here.
        session.add_all(
            [
                ChannelTenantMap(
                    provider="github",
                    provider_scope_id="acme-account",
                    org_id=_OWNER_TENANT,
                    installation_id=str(_BOUND_INSTALLATION),
                ),
                ChannelTenantMap(
                    provider="github",
                    provider_scope_id="globex-account",
                    org_id=_OTHER_TENANT,
                    installation_id=str(_FOREIGN_INSTALLATION),
                ),
            ]
        )
        await session.commit()
        yield session


def _make_app(db_session: AsyncSession, *, authorized_action: Action | None = None) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _set_authorized_action(request, call_next):
        request.state.agent_authorized_action = authorized_action
        return await call_next(request)

    app.include_router(router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    from src.internal.auth_deps import verify_internal_or_irsa
    from src.internal.credential_binding import resolve_installation_binding

    async def authenticated_fixture(request: Request):
        # Route-unit fixture: the authenticated run is fixed independently of
        # request selectors. Real broker/auth wiring is exercised separately.
        if request.headers.get("X-Caller-Identity") != _VALID_CALLER:
            raise HTTPException(403, "fixture caller mismatch")
        body = await request.json()
        if body.get("invocation_id") != _INVOCATION_ID:
            raise HTTPException(403, "fixture run mismatch")
        binding = resolve_installation_binding(
            invocation_id=_INVOCATION_ID,
            requested_installation_id=body["installation_id"],
            settings=_settings_mock(),
        )
        request.state.agent_installation_binding = binding

    app.dependency_overrides[verify_internal_or_irsa] = authenticated_fixture
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock(*, enforce_credential_binding: bool = False) -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_CALLER
    s.aws_region = "us-east-1"
    s.webhook_events_table = "adp-test-webhook-events"
    # The installation binding must be independent of this flag. Tests set it
    # False (its real value on embark1) precisely so that a guard accidentally
    # gated on it would fail these tests instead of shadowing in production.
    s.enforce_credential_binding = enforce_credential_binding
    # Issue #5663 (A09): the repository binding reads NO setting. It used to take
    # two, and a MagicMock's truthy auto-stub for an unset attribute is exactly how
    # a fixture ends up testing a configuration that does not ship — so the absence
    # of any repo-binding knob here is deliberate, not an omission. If someone
    # reintroduces a flag, the unconditional tests below fail rather than silently
    # start reading a stub.
    return s


def _ddb_table_mock(item: dict | None) -> MagicMock:
    """Mock the webhook-events DDB Table. ``None`` -> row absent."""
    table = MagicMock()
    table.query.return_value = {"Items": [item] if item is not None else []}
    return table


def _bound_row(
    *,
    installation_id: int | None = _BOUND_INSTALLATION,
    tenant_id: str = _OWNER_TENANT,
    repo: str | None = _BOUND_REPO,
) -> dict:
    """A webhook-events row as ingress writes it for a GitHub-originated run.

    Issue #5663 (A09): ``repo`` is part of the default shape because every
    GitHub-delivered event carries one (``log_event`` writes it whenever non-empty,
    and the handler always derives it from the payload). Pass ``repo=None`` to model
    the EventBridge/scheduled case, where ``target.repo`` is optional and the
    attribute is therefore genuinely absent.
    """
    row: dict = {"tenant_id": tenant_id, "arrived_at": "2026-08-27T15:00:00Z"}
    if installation_id is not None:
        row["installation_id"] = installation_id
    if repo is not None:
        row["repo"] = repo
    return row


def _body(installation_id: int = _BOUND_INSTALLATION, **over) -> dict:
    payload = {
        "installation_id": installation_id,
        "repo_owner": "acme",
        "repo_name": "widgets",
        "invocation_id": _INVOCATION_ID,
    }
    payload.update(over)
    return payload


def _post(
    db_session: AsyncSession,
    *,
    row: dict | None = _bound_row(),
    body: dict | None = None,
    mint: AsyncMock | None = None,
    settings: MagicMock | None = None,
    headers: dict | None = None,
    reviewer: AsyncMock | None = None,
    authorized_action: Action | None = None,
):
    """POST the route with DDB + mint + credential resolution stubbed out.

    ``reviewer`` stubs the #5350 reviewer-identity resolver. Its default models
    today's live state: no distinct reviewer App is registered, so the resolver
    raises ReviewerIdentityUnavailableError and the route must fall back.
    """
    settings = settings or _settings_mock()
    mint = mint or AsyncMock(return_value=("ghs_brokered_token", _GITHUB_EXPIRES_AT))
    reviewer = reviewer or AsyncMock(side_effect=ReviewerIdentityUnavailableError("not configured"))
    client = _make_app(db_session, authorized_action=authorized_action)

    with (
        patch("src.internal.routes.get_settings", return_value=settings),
        patch("src.internal.auth_deps.get_settings", return_value=settings),
        patch(
            "src.internal.credential_binding._get_dynamodb_table",
            return_value=_ddb_table_mock(row),
        ),
        patch(
            "src.internal.routes.resolve_tenant_app_credentials",
            new=AsyncMock(return_value=("99001", "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----")),
        ),
        patch("src.internal.routes.resolve_reviewer_app_credentials", new=reviewer),
        patch("src.internal.routes.mint_installation_token_with_expiry", new=mint),
    ):
        resp = client.post(
            "/internal/v1/github-installation-token",
            json=body if body is not None else _body(),
            headers=headers if headers is not None else {"X-Caller-Identity": _VALID_CALLER},
        )
    return resp, mint


async def _audit_rows(db: AsyncSession, event_type: str) -> list[AuditLog]:
    result = await db.execute(select(AuditLog).where(AuditLog.event_type == event_type))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Confused-deputy protection — the reason this route exists
# ---------------------------------------------------------------------------


class TestInstallationBindingEnforced:
    @pytest.mark.asyncio
    async def test_mismatched_installation_is_rejected(self, db):
        """A run bound to installation A cannot mint for installation B.

        The worker takes installation_id verbatim from the SQS envelope, so it is
        attacker-influenceable. Without this check the gatekeeper would be a
        cleaner privilege-escalation primitive than the bug it replaces.
        """
        resp, mint = _post(db, body=_body(installation_id=_FOREIGN_INSTALLATION))

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "installation_binding_mismatch"
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_row_without_installation_id_is_rejected(self, db):
        """Fail-closed on absence, not fail-open.

        installation_id is written conditionally into the webhook-events row, so
        a row legitimately may not carry one. That must be a 403, not a 200.
        """
        resp, mint = _post(db, row=_bound_row(installation_id=None))

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "installation_binding_failed"
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_registry_row_is_rejected(self, db):
        resp, mint = _post(db, row=None)

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_invocation_id_is_rejected(self, db):
        body = _body()
        body.pop("invocation_id")
        resp, mint = _post(db, body=body)

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_binding_enforces_even_when_enforce_flag_is_false(self, db):
        """The guard must not be gated on ENFORCE_CREDENTIAL_BINDING.

        That flag is false on at least one live environment. A control behind it
        shadows instead of enforcing — which would leave this route mintable for
        an arbitrary installation while every unit test still passed.
        """
        resp, mint = _post(
            db,
            body=_body(installation_id=_FOREIGN_INSTALLATION),
            settings=_settings_mock(enforce_credential_binding=False),
        )

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_denial_is_audited(self, db):
        """An ownership denial leaves a trail — org_id is claimed by nobody here."""
        # Row binds an installation that no organization row claims -> NOT_FOUND.
        resp, mint = _post(
            db,
            row=_bound_row(installation_id=999999),
            body=_body(installation_id=999999),
        )

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()
        rows = await _audit_rows(db, "github_installation_token_denied")
        assert len(rows) == 1
        assert rows[0].details["reason"] == "ownership_check_failed"


class TestOwnershipCheck:
    @pytest.mark.asyncio
    async def test_installation_owned_by_another_tenant_is_conflict(self, db):
        """Row claims tenant T for an installation that provably belongs to U.

        Distinct from a binding mismatch: the request agrees with the row, but the
        row itself is wrong. Fails closed rather than trusting the row.
        """
        resp, mint = _post(
            db,
            row=_bound_row(installation_id=_FOREIGN_INSTALLATION, tenant_id=_OWNER_TENANT),
            body=_body(installation_id=_FOREIGN_INSTALLATION),
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "installation_not_owned"
        mint.assert_not_awaited()


# ---------------------------------------------------------------------------
# Happy path — expiry passthrough and least privilege
# ---------------------------------------------------------------------------


class TestMintHappyPath:
    @pytest.mark.asyncio
    async def test_returns_token_and_githubs_expires_at(self, db):
        """expires_at must be GitHub's value, not a locally-computed now+1h.

        The TS TokenManager schedules its refresh off this. A guess drifts, and
        the run dies mid-flight at the real expiry.
        """
        resp, mint = _post(db)

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert resp.headers["cache-control"] == "no-store"
        assert payload["token"] == "ghs_brokered_token"
        assert payload["expires_at"] == _GITHUB_EXPIRES_AT
        # The App ID is public (only the private key is secret) and the worker still
        # needs it: it sets GH_APP_ID, which the TS canInitTokenManager() requires in
        # both modes, and it forms the bot commit identity. In broker mode the pod
        # does no vault read, so this response is the only place it can come from —
        # omitting it recreates the same silent 1-hour token-refresh death.
        assert payload["app_id"] == "99001"
        mint.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mint_is_scoped_to_the_single_repo_and_minimal_permissions(self, db):
        """Least privilege: this repo only, and only the verbs an agent run needs."""
        from src.knowledge.github_app_service import AGENT_RUN_PERMISSIONS

        resp, mint = _post(db)

        assert resp.status_code == 200, resp.text
        kwargs = mint.await_args.kwargs
        assert kwargs["repositories"] == ["widgets"], "token must be scoped to the run's one repo, not all installation repos"

        permissions = kwargs["permissions"]
        assert permissions, "a mint with no permissions map inherits the App's full grant"
        assert set(permissions) <= set(AGENT_RUN_PERMISSIONS)
        # Nothing that would let a hijacked run change the org or its automation.
        for forbidden in ("administration", "members", "secrets", "workflows", "actions"):
            assert forbidden not in permissions

    @pytest.mark.asyncio
    async def test_mint_uses_the_bound_installation_not_the_request(self, db):
        """The minted installation comes from the registry row, positionally."""
        resp, mint = _post(db)

        assert resp.status_code == 200, resp.text
        assert mint.await_args.args[2] == _BOUND_INSTALLATION

    @pytest.mark.asyncio
    async def test_mint_is_audited(self, db):
        resp, _ = _post(db)

        assert resp.status_code == 200, resp.text
        rows = await _audit_rows(db, "github_installation_token_minted")
        assert len(rows) == 1
        assert rows[0].org_id == _OWNER_TENANT
        assert rows[0].details["installation_id"] == _BOUND_INSTALLATION
        assert rows[0].details["repositories"] == ["widgets"]
        assert rows[0].details["expires_at"] == _GITHUB_EXPIRES_AT
        # The token itself must never be audited.
        assert "token" not in rows[0].details


class TestMintFailure:
    @pytest.mark.asyncio
    async def test_github_rejection_is_502_and_audited(self, db):
        resp, _ = _post(db, mint=AsyncMock(side_effect=RuntimeError("GitHub said no")))

        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"]["error"] == "mint_failed"
        rows = await _audit_rows(db, "github_installation_token_denied")
        assert len(rows) == 1
        assert rows[0].details["reason"] == "mint_failed"

    @pytest.mark.asyncio
    async def test_no_token_in_error_response(self, db):
        """A mint failure must not echo GitHub's body (it can carry credentials)."""
        resp, _ = _post(db, mint=AsyncMock(side_effect=RuntimeError("ghs_leaked_secret")))

        assert resp.status_code == 502
        assert "ghs_leaked_secret" not in resp.text


class TestRepoBinding:
    """Issue #5663 (A09): the mint is bound to the run's OWN repository.

    Installation binding proves "this run's webhook named installation X for tenant
    T"; Postgres proves T owns X. Neither says which repository INSIDE X the run was
    assigned, so before this change a run dispatched to acme/widgets could obtain a
    write-capable token listing acme/secrets — same installation, same tenant, both
    prior layers satisfied.

    These tests drive the mounted route and assert the refusal REASON, so a schema
    rejection or an unrelated 403 cannot be mistaken for the repository check firing.
    """

    @pytest.mark.asyncio
    async def test_repo_other_than_the_runs_own_is_refused(self, db):
        """The escalation this check exists to close."""
        resp, mint = _post(
            db,
            row=_bound_row(repo=_BOUND_REPO),
            body=_body(repo_owner="acme", repo_name="secrets"),
        )

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "repo_binding_mismatch"
        mint.assert_not_awaited(), "a refused request must not reach GitHub"

    @pytest.mark.asyncio
    async def test_the_runs_own_repo_still_mints(self, db):
        """The legitimate caller — the shape every worker actually sends."""
        resp, mint = _post(db, row=_bound_row(repo=_BOUND_REPO), body=_body())

        assert resp.status_code == 200, resp.text
        mint.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_case_difference_is_not_a_denial(self, db):
        """GitHub treats owner/repo case-insensitively; a case split is not an
        authorization difference, and refusing it would be a self-inflicted outage.
        The row's casing comes from the webhook payload, the request's from the
        worker's env, so they legitimately differ."""
        resp, mint = _post(
            db,
            row=_bound_row(repo="Acme/Widgets"),
            body=_body(repo_owner="acme", repo_name="widgets"),
        )

        assert resp.status_code == 200, resp.text
        mint.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_holds_with_the_authority_flag_absent(self, db, monkeypatch):
        """Regression guard required by #5663's acceptance.

        The equivalent compare already existed in verify_broker_worker, but that runs
        only when the caller presents a run credential, is flagged
        requires_run_identity, or AGENT_AUTHORITY_ENABLED is true — and that flag is
        false in live environments, so on the default path the compare never ran.
        This asserts the refusal with the flag ABSENT and with its old default, i.e.
        it fails if a legacy non-verified path is ever reintroduced.
        """
        monkeypatch.delenv("AGENT_AUTHORITY_ENABLED", raising=False)
        resp, _ = _post(db, row=_bound_row(repo=_BOUND_REPO), body=_body(repo_name="secrets"))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "repo_binding_mismatch"

        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
        resp, _ = _post(db, row=_bound_row(repo=_BOUND_REPO), body=_body(repo_name="secrets"))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "repo_binding_mismatch"

    @pytest.mark.asyncio
    async def test_a_run_with_no_recorded_repository_is_refused_on_the_default_path(self, db):
        """No server-side repository evidence must not resolve to "allowed".

        This test previously asserted HTTP 200 for this exact shape, on the theory
        that EventBridge/scheduled dispatch produces legitimate repo-less runs. That
        left the caller's own repo_owner/repo_name as the only input deciding which
        repository received a write-capable token — the escalation the check exists
        to close — so it is now a refusal on the shipped configuration, with no flag
        able to turn it back into a pass.

        The scheduled-caller worry does not survive contact with the producers: the
        worker parses source_ref.repo (required) and calls repo.split("/", 1) BEFORE
        any mint, so a repo-less run fails at bootstrap and never gets here. See
        _assert_token_repo_binding for the full per-producer evidence.
        """
        resp, mint = _post(db, row=_bound_row(repo=None), body=_body())

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "repo_binding_failed"
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_blank_recorded_repository_is_also_refused(self, db):
        """An empty-string repo is absent evidence, not a repository named "".

        DynamoDB will hold a blank string if a producer ever writes one, and a
        truthiness bug here would compare "" to the request and refuse as a
        *mismatch*, or worse normalise to None and be waved through by a
        reintroduced allowance. Either way the run has no repository.
        """
        resp, mint = _post(db, row=_bound_row(repo=""), body=_body())

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "repo_binding_failed"
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_setting_can_turn_the_repository_check_back_off(self, db, capsys):
        """Regression guard: the refusal must not be reachable through configuration.

        The earlier revision allowed a cross-repository mint whenever
        enforce_token_repo_binding was false, and allowed an unbound one whenever
        enforce_unbound_repo_token_denial was false. Both flags are gone. A Settings
        object that still carries them — or any other attribute someone adds later,
        since MagicMock auto-stubs every name truthily — must not change the outcome.
        """
        permissive = _settings_mock()
        permissive.enforce_token_repo_binding = False
        permissive.enforce_unbound_repo_token_denial = False

        mismatch, mismatch_mint = _post(db, row=_bound_row(repo=_BOUND_REPO), body=_body(repo_name="secrets"), settings=permissive)
        assert mismatch.status_code == 403, mismatch.text
        assert mismatch.json()["detail"]["error"] == "repo_binding_mismatch"
        mismatch_mint.assert_not_awaited()

        unbound, unbound_mint = _post(db, row=_bound_row(repo=None), body=_body(), settings=permissive)
        assert unbound.status_code == 403, unbound.text
        assert unbound.json()["detail"]["error"] == "repo_binding_failed"
        unbound_mint.assert_not_awaited()

        # Denials are counted as real denials, not as would-denies: the outcome the
        # counter reports has to match the outcome the caller got.
        emitted = capsys.readouterr().out
        assert '"InternalIdentityBindingDenied": 1' in emitted
        assert '"InternalIdentityBindingWouldDeny": 0' in emitted
        assert '"Route": "github-installation-token"' in emitted

    @pytest.mark.asyncio
    async def test_repo_is_read_from_the_row_not_the_request(self, db):
        """The projection must actually fetch repo, or the check is vacuous.

        Without `repo` in the ProjectionExpression the attribute is absent from every
        row, every run looks unbound, and the mismatch case above can never fire —
        the check would pass its own tests while enforcing nothing in production.
        """
        table = _ddb_table_mock(_bound_row())
        with patch("src.internal.credential_binding._get_dynamodb_table", return_value=table):
            from src.internal.credential_binding import resolve_installation_binding

            binding = resolve_installation_binding(
                invocation_id=_INVOCATION_ID,
                requested_installation_id=_BOUND_INSTALLATION,
                settings=_settings_mock(),
            )

        assert "repo" in table.query.call_args.kwargs["ProjectionExpression"]
        assert binding.repo == _BOUND_REPO


class TestAuthn:
    @pytest.mark.asyncio
    async def test_unauthenticated_is_rejected(self, db):
        resp, mint = _post(db, headers={})

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wrong_internal_key_is_rejected(self, db):
        resp, mint = _post(db, headers={"X-Caller-Identity": "nope"})

        assert resp.status_code == 403, resp.text
        mint.assert_not_awaited()


class TestReviewerIdentity:
    """Issue #5350 — the reviewer identity, and the honesty of its fallback.

    The defect: every PR the engine opens is authored by the tenant's GitHub App,
    and every reviewer the engine dispatched authenticated as that SAME App. GitHub
    answers 422 "Can not approve your own pull request" to both APPROVE and
    REQUEST_CHANGES, so no engine-authored PR could ever receive a verdict and
    `reviewDecision` stayed empty for the entire life of the engine.

    These tests cover the gateway half: minting as a DISTINCT reviewer App when one
    is configured, and — crucially, because it is today's live state — reporting
    truthfully when one is not. The `identity` field in the response is what lets the
    reviewer discover, before it tries, that a formal verdict is impossible. If that
    field silently said "review" while handing back the authoring identity, the whole
    fallback would be a lie and the reviewer would go on downgrading in silence.
    """

    _REVIEW_INSTALLATION = 777001

    def test_review_action_value_matches_the_policy_enum(self):
        """The internal plane compares the authorized action by STRING, so pin the value.

        `src/internal/` must not import `src.orchestration` (agent pods can call every
        internal route, so promotion state stays unreachable from that plane — see
        tests/orchestration/test_internal_plane_guard.py). The route therefore matches
        `Action.REVIEW` by its value. Renaming the enum value without updating the
        constant would silently stop granting the reviewer identity to real reviewers,
        with no test failing anywhere near the change — so assert they agree.
        """
        from src.knowledge.github_app_service import REVIEW_ACTION_VALUE
        from src.orchestration.execution_policy import Action

        assert REVIEW_ACTION_VALUE == Action.REVIEW.value

    @pytest.mark.asyncio
    async def test_default_identity_is_unchanged_for_existing_callers(self, db):
        """A body with no `identity` mints exactly as before, on the bound installation."""
        resp, mint = _post(db)

        assert resp.status_code == 200, resp.text
        assert resp.json()["identity"] == "default"
        assert mint.await_args.args[2] == _BOUND_INSTALLATION

    @pytest.mark.asyncio
    async def test_configured_reviewer_app_mints_on_its_own_installation(self, db):
        """The reviewer App's OWN installation id is used, not the authoring one.

        A second GitHub App has a separate installation on the org. Minting the
        reviewer key against the authoring App's installation id would fail at
        GitHub, so this asserts the installation actually travels with the identity.
        """
        reviewer = AsyncMock(return_value=("88002", _FAKE_REVIEW_KEY, self._REVIEW_INSTALLATION))
        resp, mint = _post(db, body=_body(identity="review"), reviewer=reviewer, authorized_action=Action.REVIEW)

        assert resp.status_code == 200, resp.text
        assert resp.json()["identity"] == "review"
        assert resp.json()["app_id"] == "88002"
        assert mint.await_args.args[0] == "88002"
        assert mint.await_args.args[2] == self._REVIEW_INSTALLATION

    @pytest.mark.asyncio
    async def test_absent_reviewer_app_falls_back_and_says_so(self, db):
        """Today's live state: no reviewer App, so the response must admit it.

        The run still gets a working token — the review attempt should proceed and
        collect the 422 rather than the run dying — but `identity` comes back
        "default", which is the caller's signal to name the pending human approval.
        """
        resp, mint = _post(db, body=_body(identity="review"), authorized_action=Action.REVIEW)

        assert resp.status_code == 200, resp.text
        assert resp.json()["identity"] == "default"
        assert resp.json()["app_id"] == "99001"
        assert mint.await_args.args[2] == _BOUND_INSTALLATION

    @pytest.mark.asyncio
    async def test_fallback_is_recorded_in_the_audit_trail(self, db):
        """Requested vs granted are both audited, so the gap is answerable later."""
        resp, _ = _post(db, body=_body(identity="review"), authorized_action=Action.REVIEW)

        assert resp.status_code == 200, resp.text
        rows = await _audit_rows(db, "github_installation_token_minted")
        assert len(rows) == 1
        assert rows[0].details["identity_requested"] == "review"
        assert rows[0].details["identity_granted"] == "default"
        assert rows[0].details["authorized_action"] == "review"

    @pytest.mark.asyncio
    async def test_broken_reviewer_credentials_do_not_degrade_to_authoring(self, db):
        """A PRESENT but malformed reviewer secret is a misconfiguration, not a fallback.

        Silently falling back here would hide a real deployment fault behind the
        expected "not registered yet" path, so the run fails loudly instead.
        """
        reviewer = AsyncMock(side_effect=ValueError("installation_id missing from reviewer secret"))
        resp, mint = _post(db, body=_body(identity="review"), reviewer=reviewer, authorized_action=Action.REVIEW)

        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"]["error"] == "app_credentials_unavailable"
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_identity_is_refused_not_defaulted(self, db):
        """A caller asking for an identity we do not implement gets no token at all.

        Answering a typo with a usable authoring token would hand back the one
        identity that cannot review while the caller believed otherwise.
        """
        resp, mint = _post(db, body=_body(identity="reviewer"))

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "unknown_identity"
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_implementation_action_cannot_mint_reviewer_credentials(self, db):
        """A developer-controlled body cannot select the distinct reviewer App."""
        reviewer = AsyncMock(return_value=("88002", _FAKE_REVIEW_KEY, self._REVIEW_INSTALLATION))

        resp, mint = _post(
            db,
            body=_body(identity="review"),
            reviewer=reviewer,
            authorized_action=Action.DEVELOP,
        )

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "review_identity_not_authorized"
        mint.assert_not_awaited()
        reviewer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_review_runs_bootstrap_mint_keeps_the_authoring_identity(self, db):
        """A review run's OTHER mints must not be routed to the reviewer App.

        A reviewer run mints more than once: its bootstrap mint (entrypoint.py, which
        sends no `identity`) clones the repo and drives the check run, and it needs the
        authoring App's broader grant. Selecting the reviewer App from the action alone
        would point that mint at a review-only App and ask GitHub for permissions that
        App was never granted — GitHub refuses, so the run would die at startup instead
        of reviewing anything. The authority check stays server-side; the request still
        has to say which identity it wants.
        """
        reviewer = AsyncMock(return_value=("88002", _FAKE_REVIEW_KEY, self._REVIEW_INSTALLATION))

        resp, mint = _post(db, reviewer=reviewer, authorized_action=Action.REVIEW)

        assert resp.status_code == 200, resp.text
        assert resp.json()["identity"] == "default"
        assert resp.json()["app_id"] == "99001"
        assert mint.await_args.args[2] == _BOUND_INSTALLATION
        reviewer.assert_not_awaited()
        # The authoring grant is intact for the work this mint actually does.
        assert mint.await_args.kwargs["permissions"].get("issues") == "write"

    @pytest.mark.asyncio
    async def test_reviewer_mint_asks_only_for_permissions_a_review_app_holds(self, db):
        """A reviewer App is not granted `issues`/`checks`; the mint must not ask.

        The run's authorized permission set is computed for the AUTHORING App. GitHub
        refuses an access-token request naming a permission its App was never granted,
        so passing that set through unchanged would make a correctly configured reviewer
        App fail to mint. Narrowing also keeps the reviewer token least-privilege: it can
        record a verdict and nothing else.
        """
        reviewer = AsyncMock(return_value=("88002", _FAKE_REVIEW_KEY, self._REVIEW_INSTALLATION))

        resp, mint = _post(db, body=_body(identity="review"), reviewer=reviewer, authorized_action=Action.REVIEW)

        assert resp.status_code == 200, resp.text
        assert resp.json()["identity"] == "review"
        permissions = mint.await_args.kwargs["permissions"]
        assert permissions.get("pull_requests") == "write", "a reviewer must be able to submit the review"
        assert "issues" not in permissions
        assert "checks" not in permissions

    @pytest.mark.asyncio
    async def test_reviewer_identity_still_requires_the_ownership_layers(self, db):
        """Asking for the reviewer identity is not a way around the tenant boundary.

        The identity choice is consulted only AFTER binding and ownership pass, so a
        run bound to another tenant's installation is refused exactly as before.
        """
        reviewer = AsyncMock(return_value=("88002", "key", self._REVIEW_INSTALLATION))
        resp, mint = _post(
            db,
            row=_bound_row(installation_id=_FOREIGN_INSTALLATION, tenant_id=_OTHER_TENANT),
            body=_body(identity="review"),
            reviewer=reviewer,
            authorized_action=Action.REVIEW,
        )

        assert resp.status_code in (403, 409), resp.text
        mint.assert_not_awaited()
        reviewer.assert_not_awaited()
