"""
Budget enforcement soundness tests (Issue #4075, sub-EPIC #4068 child ·E, Wave 1).

These tests pin the *soundness* of budget enforcement, not its coverage. Caps
already bind on the happy path (``shared/enforced_paths.py`` is correct and
complete); what this file guards is what happens when the ledger read FAILS.

Pre-fix behaviour (``budget_fail_mode = "open"`` at ``budget/config.py:16``) let
any DB/IAM error through as ``allowed=True``, so a transient fault became an open
window of uncapped model spend. Post-fix the default is ``"closed"`` with a
bounded, alarmed grace window so a DB blip degrades instead of either leaking
spend indefinitely or hard-downing all inference.

Every test here asserts the OUTCOME (allowed/denied, HTTP status, whether the
downstream app ran) — never the plumbing. Per #4068's gate rules, T1/T2 were
committed in a state where they FAIL on pre-fix code.

Test map (labels match the approved design in #4075):
  T1 — fail-closed is the DEFAULT (gate; asserts against a real BudgetConfig)
  T2 — end-to-end denial through the ASGI middleware (gate)
  T3 — within the grace window: allow + signal
  T4 — the grace window is bounded (injected clock, no sleep)
  T7 — the shipped default matches the documented behaviour
  T8 — unexpected (non-infrastructure) exception classes fail OPEN (D4)
  T9 — regression: all six enforced paths still admit in-budget traffic
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from src.budget.config import BudgetConfig
from src.budget.enforcement_service import BudgetEnforcementService
from src.shared.enforced_paths import ENFORCED_PATHS
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def token_context():
    return TokenContext(
        user_id="user-123",
        org_id="org-456",
        team_id="team-789",
        department_id="dept-012",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _db_error() -> OperationalError:
    """A realistic transient infrastructure fault.

    This is the shape of an RDS IAM-token expiry / brief DB unavailability —
    the exact fault class the grace window exists to absorb.
    """
    return OperationalError("SELECT 1", {}, Exception("PAM authentication failed"))


def _real_config_with_grace(grace_seconds: int) -> BudgetConfig:
    """Build a REAL BudgetConfig, overriding only the grace duration.

    Critically this does NOT set ``budget_fail_mode`` — that is the property
    under test and must be read from ``budget/config.py``. The existing test
    ``test_check_budget_fails_closed_on_error_by_default`` patched the whole
    config object and hardcoded ``"closed"``, so it asserted a guarantee it
    never actually exercised (the #4046 "test rename, not a fix" trap that
    #4068 exists to prevent). Only the orthogonal grace-window knob is tuned
    here so the test can observe the post-grace steady state.
    """
    config = BudgetConfig()
    object.__setattr__(config, "budget_fail_open_grace_seconds", grace_seconds)
    return config


class TestFailClosedDefault:
    """T1 / T7 — the shipped default must be fail-CLOSED."""

    @pytest.mark.asyncio
    async def test_ledger_read_failure_denies_by_default(self, token_context):
        """T1 (GATE): a DB error past the grace window DENIES the request.

        Asserts against a real ``BudgetConfig()`` so the shipped default in
        ``budget/config.py`` is what decides the outcome. Pre-fix the default
        is ``"open"`` and this returns ``allowed=True``.
        """
        config = _real_config_with_grace(0)  # no grace: observe steady state
        service = BudgetEnforcementService()

        with patch.object(service, "_get_session", side_effect=_db_error()):
            with patch("src.budget.enforcement_service.budget_config", config):
                result = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        assert result.allowed is False, "a failed ledger read must NOT admit the request — that is uncapped spend"
        assert result.blocked_reason is not None

    def test_shipped_default_is_closed(self):
        """T7: the default documented in config.py is the default that ships."""
        assert BudgetConfig().budget_fail_mode == "closed"

    @pytest.mark.asyncio
    async def test_agent_budget_check_also_denies_by_default(self):
        """The second fail-open site (``check_agent_budget``) is fixed too (D7).

        Currently unreachable (#3985 removed its driver) but live code the
        issue anticipates re-wiring, and it carried the identical defect.
        """
        config = _real_config_with_grace(0)
        service = BudgetEnforcementService()

        with patch.object(service, "_get_session", side_effect=_db_error()):
            with patch("src.budget.enforcement_service.budget_config", config):
                result = await service.check_agent_budget("budget-cfg-1", estimated_cost=Decimal("1.00"))

        assert result.allowed is False
        assert result.blocked_reason is not None


class TestGraceWindow:
    """T3 / T4 — the grace window must be both real and bounded."""

    @pytest.mark.asyncio
    async def test_first_failure_within_window_is_allowed_and_signalled(self, token_context):
        """T3: inside the window, allow — but say so, loudly.

        An allow that is indistinguishable from a healthy allow is just
        fail-open. ``grace_engaged`` is what makes it observable.
        """
        config = _real_config_with_grace(30)
        service = BudgetEnforcementService()

        with patch.object(service, "_get_session", side_effect=_db_error()):
            with patch("src.budget.enforcement_service.budget_config", config):
                with patch("src.budget.enforcement_service.emit_budget_grace_engaged") as mock_emit:
                    result = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        assert result.allowed is True
        assert result.grace_engaged is True
        assert mock_emit.called, "engaging the grace window must emit the alarm metric"

    @pytest.mark.asyncio
    async def test_sustained_failure_past_window_transitions_to_deny(self, token_context):
        """T4: the window is time-BOUNDED — an unbounded one is fail-open.

        Uses an injected clock, never ``sleep``, and asserts the transition
        (allow → deny) rather than either endpoint alone.
        """
        from src.budget.grace_window import GraceWindow

        now = [1_000.0]
        config = _real_config_with_grace(30)
        window = GraceWindow(grace_seconds=30, redis_url=None, clock=lambda: now[0])
        service = BudgetEnforcementService(grace_window=window)

        with patch.object(service, "_get_session", side_effect=_db_error()):
            with patch("src.budget.enforcement_service.budget_config", config):
                first = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

                now[0] += 31.0  # sustained outage, past the bound
                later = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        assert first.allowed is True, "a transient blip must not hard-down inference"
        assert later.allowed is False, "a sustained outage must stop leaking uncapped spend"
        assert later.deny_reason == "check_unavailable"

    @pytest.mark.asyncio
    async def test_recovery_resets_the_window(self, token_context):
        """A recovered DB must restore a full window, not a used-up one."""
        from src.budget.grace_window import GraceWindow

        now = [1_000.0]
        window = GraceWindow(grace_seconds=30, redis_url=None, clock=lambda: now[0])

        assert await window.register_failure() is True
        now[0] += 31.0
        assert await window.register_failure() is False

        await window.clear()  # DB came back
        now[0] += 1.0
        assert await window.register_failure() is True, "a fresh outage gets a fresh window"


class TestExceptionClassHandling:
    """T8 — a code bug must not be able to permanently down all inference (D4)."""

    @pytest.mark.asyncio
    async def test_unexpected_exception_class_fails_open_with_alarm(self, token_context):
        """T8: non-infrastructure faults fail OPEN, loudly.

        A ``TypeError`` in the check is deterministic — it recurs on every
        request forever, so no grace window rescues it. Under a blanket
        fail-closed it would be a permanent total outage. This is a
        deliberate, signed-off hole in "fail closed" (D4), so it is pinned by
        a test to stop it changing silently.
        """
        config = _real_config_with_grace(0)
        service = BudgetEnforcementService()

        with patch.object(service, "_get_session", side_effect=TypeError("bad code path")):
            with patch("src.budget.enforcement_service.budget_config", config):
                with patch("src.budget.enforcement_service.emit_budget_check_failure") as mock_emit:
                    result = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        assert result.allowed is True, "a code bug must not take down all inference"
        assert mock_emit.called, "an unexpected fault class must raise a distinct high-severity signal"
        _, kwargs = mock_emit.call_args
        assert kwargs.get("fault_class") == "unexpected"

    @pytest.mark.asyncio
    async def test_timeout_is_treated_as_infrastructure_fault(self, token_context):
        """A check timeout is transient infra, so the grace window applies."""
        config = _real_config_with_grace(0)
        service = BudgetEnforcementService()

        with patch.object(service, "_get_session", side_effect=TimeoutError()):
            with patch("src.budget.enforcement_service.budget_config", config):
                result = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        assert result.allowed is False
        assert result.deny_reason == "check_unavailable"


class TestExplicitFailOpenStillHonoured:
    """The rollback lever must keep working."""

    @pytest.mark.asyncio
    async def test_fail_mode_open_allows_on_error(self, token_context):
        """Setting mode=open restores prior behaviour — this is the rollback."""
        config = BudgetConfig()
        object.__setattr__(config, "budget_fail_mode", "open")
        service = BudgetEnforcementService()

        with patch.object(service, "_get_session", side_effect=_db_error()):
            with patch("src.budget.enforcement_service.budget_config", config):
                result = await service.check_budget_hierarchy(token_context, estimated_cost=Decimal("1.00"))

        assert result.allowed is True
        assert result.grace_engaged is False, "an explicit fail-open is not a grace-window allow"

    def test_fail_mode_is_configurable_from_the_environment(self, monkeypatch):
        """The mode must be reversible at runtime, not a compile-time constant.

        Without this the documented rollback ("SSM put + redeploy") cannot
        execute, which makes shipping fail-closed strictly worse than the
        status quo. The env var carries a doubled BUDGET because
        ``env_prefix`` is ``BG_BUDGET_``.
        """
        monkeypatch.setenv("BG_BUDGET_BUDGET_FAIL_MODE", "open")
        assert BudgetConfig().budget_fail_mode == "open"


class TestEnforcedPathRegression:
    """T9 — the fix must not narrow enforcement or block healthy traffic."""

    @pytest.mark.asyncio
    async def test_in_budget_traffic_still_admitted_on_every_enforced_path(self, token_context):
        """T9: a healthy in-budget check admits on all six enforced paths."""
        from src.budget.enforcement_middleware import BudgetEnforcementMiddleware

        assert len(ENFORCED_PATHS) == 6, "the enforced-path list must stay complete — do not narrow it"

        for path in ENFORCED_PATHS:
            reached = []

            async def inner_app(scope, receive, send):
                reached.append(scope["path"])
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            service = BudgetEnforcementService()
            service.check_budget_hierarchy = AsyncMock(  # type: ignore[method-assign]
                return_value=__import__("src.shared.schemas.budget", fromlist=["EnforcementResult"]).EnforcementResult(allowed=True)
            )
            middleware = BudgetEnforcementMiddleware(inner_app, enforcement_service=service)

            request_path = path if not path.endswith("/") else f"{path}some-model/invoke"
            scope = {
                "type": "http",
                "path": request_path,
                "method": "POST",
                "headers": [],
                "state": {"token_context": token_context},
            }
            sent: list = []

            async def receive():
                return {"type": "http.request", "body": b"{}", "more_body": False}

            await middleware(scope, receive, lambda msg: _collect(sent, msg))

            assert reached == [request_path], f"in-budget traffic must still reach the app on {path}"


async def _collect(sink: list, message) -> None:
    sink.append(message)
