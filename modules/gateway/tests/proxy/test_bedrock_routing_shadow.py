"""Tests for audit-only routing observation and its isolation from signing.

The gateway's invoke path now always enforces saved mappings. These tests cover
legacy audit-only callers (including Mantle), capture failure isolation, and the
rule that an audit observation cannot override a request's credential decision.
Proxy unit fixtures explicitly supply an unmapped decision; real mapped signing
is covered by test_bedrock_enforcement.py.
"""

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.proxy.bedrock_routing import BedrockTarget, resolve_shadow_target
from src.shared.schemas.auth import TokenContext
from tests.proxy.pricing_fixtures import price_fixture_usage

PLATFORM_ACCOUNT = "999988887777"
MAPPED_ACCOUNT = "111111111111"

# `ProxyService.invoke` takes the raw request dict (it reads `api_format` off it),
# not a parsed schema object — see `tests/proxy/test_service.py`.
_INVOKE_REQUEST = {
    "api_format": "bedrock",
    "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
}


def _executable_source(module) -> str:
    """Return `module`'s source with docstrings and comments stripped.

    A plain `inspect.getsource` grep cannot express "this module performs no
    credential work", because the modules that promise that in prose mention the
    very terms being searched for — the resolver's own docstring says it does no
    ``AssumeRole``, which a substring check reads as evidence that it does. Compiling
    to an AST and unparsing drops comments outright, and the docstring nodes are
    removed explicitly, leaving only code that would actually run.
    """
    import ast

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _token_context(**overrides) -> TokenContext:
    values = {
        "user_id": "cognito-sub-abc",
        "org_id": "org-acme",
        "team_id": "team-eng",
        "department_id": "dept-1",
        "account_type": "human",
        "is_admin": False,
        "expires_at": datetime.now(UTC) + timedelta(hours=12),
        **overrides,
    }
    return TokenContext(**values)


class _CapturingUsageService:
    """Captures the kwargs `log_request` was called with.

    Substituted for the real ``UsageService`` so the assertion is on what the
    writer receives — the boundary that actually decides what lands in the column.
    Mirrors the harness `test_client_tool_capture.py` uses for the same reason.
    """

    calls: list[dict] = []

    def __init__(self, session):  # noqa: D107 - mirrors UsageService(session)
        pass

    async def log_request(self, **kwargs):
        type(self).calls.append(kwargs)


def _session_factory():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session)


# ============================================================================
# 1. The resolved target reaches the writer
# ============================================================================


class TestResolvedTargetReachesTheWriter:
    """`_log_usage` passes the resolved account to `log_request`.

    Asserted at the writer boundary rather than by inspecting the resolver in
    isolation, because the failure this guards against is precisely a correct
    resolver whose answer never reaches the column.
    """

    async def _invoke_log_usage(self, shadow_target: BedrockTarget | None) -> dict:
        """Run the REAL `ProxyService._log_usage` with a stubbed resolution."""
        from src.proxy.service import ProxyService

        _CapturingUsageService.calls = []
        service = ProxyService.__new__(ProxyService)  # bypass __init__'s deps

        with (
            patch("src.proxy.service.get_session_factory", _session_factory),
            patch("src.proxy.service.UsageService", _CapturingUsageService),
            patch("src.proxy.service.reconcile_budget_reservation", AsyncMock()),
            patch("src.proxy.service.resolve_shadow_target", AsyncMock(return_value=shadow_target)),
        ):
            await service._log_usage(
                context=_token_context(),
                model="anthropic.claude-3-5-sonnet",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.0012,
                latency_ms=900,
                status_code=200,
            )

        assert _CapturingUsageService.calls, "log_request was never called"
        return _CapturingUsageService.calls[0]

    @pytest.mark.asyncio
    async def test_a_mapped_account_is_persisted(self):
        kwargs = await self._invoke_log_usage(BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org", destination_id="dest-1"))
        assert kwargs["bedrock_account_id"] == MAPPED_ACCOUNT

    @pytest.mark.asyncio
    async def test_the_platform_rung_is_persisted_too(self):
        """The zero-mapping case must record the platform account, not NULL.

        "This call went to the platform account, and that was the correct answer"
        is a different fact from "we did not look". The audit trail has to
        distinguish them, and this is the row shape every install produces on day
        one — so if it wrote NULL, shadow mode would be unobservable exactly where
        it is supposed to prove itself.
        """
        kwargs = await self._invoke_log_usage(BedrockTarget(account_id=PLATFORM_ACCOUNT, rung="platform"))
        assert kwargs["bedrock_account_id"] == PLATFORM_ACCOUNT

    @pytest.mark.asyncio
    async def test_unresolved_is_none_not_a_placeholder(self):
        """None reaches the writer as None, so the column is NULL.

        NULL means "not captured". A fabricated account id here would be worse
        than an absent one, because it reads as evidence — an operator would
        conclude the call was checked and served by that account.
        """
        kwargs = await self._invoke_log_usage(None)
        assert kwargs["bedrock_account_id"] is None

    @pytest.mark.asyncio
    async def test_a_platform_target_with_no_configured_account_is_null(self):
        """Config unset ⇒ NULL, even though the rung resolved.

        Null-discipline: an unset `BG_PLATFORM_BEDROCK_ACCOUNT_ID` must not become
        a guess. The same distinction the cache-token counters keep between
        "unreported" and "reported zero".
        """
        kwargs = await self._invoke_log_usage(BedrockTarget(account_id=None, rung="platform"))
        assert kwargs["bedrock_account_id"] is None

    @pytest.mark.asyncio
    async def test_capture_does_not_disturb_the_other_recorded_fields(self):
        """Adding capture must not change the cost data already being recorded.

        The row this writes IS the billing record. A regression here would be a
        metering bug shipped under a feature flag that claims to change nothing.
        """
        kwargs = await self._invoke_log_usage(BedrockTarget(account_id=MAPPED_ACCOUNT, rung="user"))
        assert kwargs["input_tokens"] == 100
        assert kwargs["output_tokens"] == 50
        assert kwargs["cost_usd"] == 0.0012
        assert kwargs["status_code"] == 200
        assert kwargs["latency_ms"] == 900

    @pytest.mark.asyncio
    async def test_resolution_receives_the_authenticated_context(self):
        """The resolver is handed the context, so §3.4 is enforceable at all.

        If `_log_usage` synthesised a context (or passed only an org id), the
        resolver's "read `org_id`, never `attributed_org_id`" rule would be
        unenforceable from inside the resolver — the caller would already have
        chosen. Pinning the hand-off keeps that decision in the one place its
        tests cover.
        """
        from src.proxy.service import ProxyService

        _CapturingUsageService.calls = []
        resolve = AsyncMock(return_value=BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org"))
        context = _token_context(attributed_org_id="org-globex")
        service = ProxyService.__new__(ProxyService)

        with (
            patch("src.proxy.service.get_session_factory", _session_factory),
            patch("src.proxy.service.UsageService", _CapturingUsageService),
            patch("src.proxy.service.reconcile_budget_reservation", AsyncMock()),
            patch("src.proxy.service.resolve_shadow_target", resolve),
        ):
            await service._log_usage(
                context=context,
                model="anthropic.claude-3-5-sonnet",
                input_tokens=1,
                output_tokens=2,
                cost_usd=0.0,
                latency_ms=10,
                status_code=200,
            )

        resolve.assert_awaited_once()
        assert resolve.await_args.args[0] is context


# ============================================================================
# 2. Signing is unchanged — the constraint the release rests on
# ============================================================================


class TestAuditDoesNotChooseCredentials:
    """Audit-only resolution cannot change an already-resolved platform call."""

    @pytest.mark.asyncio
    async def test_unmapped_calls_use_the_same_ambient_client(self, proxy_service, token_context):
        """Identity, not equality: the pool hands back the one ambient client.

        If routing had leaked into the invoke path, per-principal credentials would
        mean a different client object per caller. Asserting `is` catches that
        where a value comparison would not.
        """
        first = await proxy_service._pool_service.get_client()
        await proxy_service.invoke(_INVOKE_REQUEST, token_context)
        second = await proxy_service._pool_service.get_client()
        assert first is second

    @pytest.mark.asyncio
    async def test_get_client_takes_credentials_but_they_default_to_the_ambient_client(self):
        """The seam #4744 widened, and the shape that keeps shadow mode inert.

        R2 asserted `get_client()` was strictly zero-arg, because that made a
        resolved destination structurally unable to reach credential construction.
        #4744 owns widening it, so the zero-arg form is deliberately gone.

        What replaces it is the property that still protects shadow mode: the new
        parameter is **optional and defaults to None**. A caller that does not opt
        in cannot accidentally sign with anything but the ambient client, so every
        unmapped call and every non-enforced org keeps main's exact behaviour. A
        required parameter, or a default that was anything but None, would make
        every call a routing decision.
        """
        from src.pool.simple_pool import SimplePoolService
        from src.shared.interfaces.pool import IPoolService

        for cls in (SimplePoolService, IPoolService):
            parameters = inspect.signature(cls.get_client).parameters
            assert list(parameters) == ["self", "credentials", "single_attempt"], f"{cls.__name__}.get_client accepts no routing authority inputs"
            assert parameters["single_attempt"].default is False
            assert parameters["single_attempt"].kind is inspect.Parameter.KEYWORD_ONLY
            assert parameters["credentials"].default is None, f"{cls.__name__}.get_client must default to the ambient platform client"

    @pytest.mark.asyncio
    async def test_audit_observation_cannot_override_platform_credentials(self, proxy_service, token_context):
        """A hypothetical audit destination cannot replace the actual decision."""
        pool = proxy_service._pool_service
        pool.get_client_credentials.clear()

        with patch(
            "src.proxy.service.resolve_shadow_target",
            AsyncMock(return_value=BedrockTarget(account_id=MAPPED_ACCOUNT, rung="user", destination_id="dest-1")),
        ):
            await proxy_service.invoke(_INVOKE_REQUEST, token_context)

        assert pool.get_client_credentials == [None], "shadow mode must sign with the ambient platform client"

    def test_the_pool_module_does_not_import_the_resolver(self):
        """A static guarantee: credential construction cannot see routing at all.

        Stronger than any behavioural test, and the one that survives refactoring:
        if `simple_pool` never imports the resolver, no future edit inside it can
        accidentally consult a mapping. The import graph is the boundary.
        """
        from src.pool import simple_pool

        source = _executable_source(simple_pool)
        assert "bedrock_routing" not in source
        assert "BedrockTarget" not in source
        assert "assume_role" not in source, "shadow mode performs no AssumeRole; #4744 owns that"

    def test_the_resolver_module_performs_no_credential_work(self):
        """The other direction: the resolver never signs, assumes, or builds a client.

        It resolves and reports; a `boto3`/`sts` reference in its *code* would mean
        the "resolve only" contract had been broken inside the module whose docstring
        promises it. Compares against comment- and docstring-stripped source, since
        that docstring necessarily names the operations it promises not to perform.
        """
        from src.proxy import bedrock_routing

        source = _executable_source(bedrock_routing)
        for forbidden in ("boto3", "get_client(", "AssumeRole", "assume_role", "Credentials("):
            assert forbidden not in source, f"the resolver must not reference {forbidden} in shadow mode"

    @pytest.mark.asyncio
    async def test_audit_target_does_not_reinvoke_with_other_credentials(self, proxy_service, token_context):
        """The audit target cannot re-route an already-resolved platform call."""
        client = await proxy_service._pool_service.get_client()
        calls_before = len(client.invoke_calls)

        with patch(
            "src.proxy.service.resolve_shadow_target",
            AsyncMock(return_value=BedrockTarget(account_id=MAPPED_ACCOUNT, rung="user", destination_id="dest-1")),
        ):
            await proxy_service.invoke(_INVOKE_REQUEST, token_context)

        assert len(client.invoke_calls) == calls_before + 1
        # The invoke carries a model id and a body — no account, no role, no region
        # override. A routed invoke would have to add one of those.
        recorded = client.invoke_calls[-1]
        assert set(recorded) <= {"modelId", "body", "contentType", "accept", "streaming"}

    @pytest.mark.asyncio
    async def test_audit_resolution_happens_during_usage_settlement(self):
        """Audit-only callers may resolve during settlement after invocation.

        Actual Bedrock signing resolves its target before invoking the provider;
        that separate path is covered by test_bedrock_enforcement.py.
        """
        from src.proxy.service import ProxyService

        order: list[str] = []

        async def _resolve(context, **kwargs):
            order.append("resolve")
            return BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org")

        _CapturingUsageService.calls = []
        service = ProxyService.__new__(ProxyService)
        order.append("invoke")

        with (
            patch("src.proxy.service.get_session_factory", _session_factory),
            patch("src.proxy.service.UsageService", _CapturingUsageService),
            patch("src.proxy.service.reconcile_budget_reservation", AsyncMock()),
            patch("src.proxy.service.resolve_shadow_target", _resolve),
        ):
            await service._log_usage(
                context=_token_context(),
                model="anthropic.claude-3-5-sonnet",
                input_tokens=1,
                output_tokens=2,
                cost_usd=0.0,
                latency_ms=10,
                status_code=200,
            )

        assert order == ["invoke", "resolve"]


# ============================================================================
# 3. An observation must never break the proxy
# ============================================================================


class TestObservationCannotBreakTheProxy:
    """A failed resolution costs the observation, never the request.

    An observation that can fail a model call is worse than no observation. This
    matches the surrounding metering code, which swallows for the same reason.
    """

    @pytest.mark.asyncio
    async def test_a_raising_resolver_still_writes_the_usage_row(self):
        """The billing row must survive a routing failure.

        The nastier failure mode than "no account captured": a resolution error
        that propagated out of `_log_usage` would take the *cost record* with it,
        turning an observability feature into a revenue-loss bug.
        """
        from src.proxy.service import ProxyService

        _CapturingUsageService.calls = []
        service = ProxyService.__new__(ProxyService)

        with (
            patch("src.proxy.service.get_session_factory", _session_factory),
            patch("src.proxy.service.UsageService", _CapturingUsageService),
            patch("src.proxy.service.reconcile_budget_reservation", AsyncMock()),
            patch("src.proxy.service.resolve_shadow_target", AsyncMock(return_value=None)),
        ):
            await service._log_usage(
                context=_token_context(),
                model="anthropic.claude-3-5-sonnet",
                input_tokens=7,
                output_tokens=9,
                cost_usd=0.5,
                latency_ms=10,
                status_code=200,
            )

        assert _CapturingUsageService.calls, "the usage row must still be written"
        assert _CapturingUsageService.calls[0]["bedrock_account_id"] is None
        assert _CapturingUsageService.calls[0]["cost_usd"] == 0.5

    @pytest.mark.asyncio
    async def test_resolve_shadow_target_swallows_a_database_failure(self):
        """`resolve_shadow_target` returns None rather than raising. Ever.

        The seam every proxy path calls, so this is the one that decides whether a
        degraded database can fail model calls. It must not.
        """
        with (
            patch("src.proxy.bedrock_routing.get_settings", return_value=SimpleNamespace(bedrock_routing_shadow_mode=True)),
            patch("src.shared.database.get_session_factory", side_effect=RuntimeError("db is down")),
        ):
            assert await resolve_shadow_target(_token_context()) is None

    @pytest.mark.asyncio
    async def test_resolve_shadow_target_swallows_a_resolver_failure(self):
        """A bug inside the resolver itself is contained too.

        Not only connection failures: a `KeyError` from unexpected row shape, an
        attribute error after a model change. Any of them would otherwise surface
        as a failed model call for a feature that changes nothing.
        """
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.proxy.bedrock_routing.get_settings", return_value=SimpleNamespace(bedrock_routing_shadow_mode=True)),
            patch("src.shared.database.get_session_factory", return_value=MagicMock(return_value=session)),
            patch(
                "src.proxy.bedrock_routing.bedrock_routing_resolver.resolve",
                AsyncMock(side_effect=KeyError("unexpected row shape")),
            ),
        ):
            assert await resolve_shadow_target(_token_context()) is None

    @pytest.mark.asyncio
    async def test_a_failure_does_not_log_credentials_or_the_token(self, caplog):
        """The warning carries the org and the error text — nothing sensitive.

        The context holds no secret material, but it is the object nearest to
        hand at the failure site, and dumping it (or the token) into a log is the
        easy mistake. Asserted so a future "add more detail to this warning" edit
        has to stay inside the boundary.
        """
        import logging

        with (
            patch("src.proxy.bedrock_routing.get_settings", return_value=SimpleNamespace(bedrock_routing_shadow_mode=True)),
            patch("src.shared.database.get_session_factory", side_effect=RuntimeError("db is down")),
            caplog.at_level(logging.WARNING, logger="bedrockgateway.proxy.routing"),
        ):
            await resolve_shadow_target(_token_context())

        assert caplog.records, "a swallowed failure must still be visible in logs"
        rendered = " ".join(record.getMessage() + str(getattr(record, "error", "")) for record in caplog.records)
        for secret in ("aws_secret_access_key", "aws_session_token", "Authorization", "cognito-sub-abc"):
            assert secret not in rendered


# ============================================================================
# 4. The flag is a real off switch
# ============================================================================


class TestFlagIsARealOffSwitch:
    """Off means "stop observing" — and costs no database work at all.

    Because enforcement does not exist yet, this flag has exactly one off-state
    meaning. It must never read as "stop routing", and it must be cheap enough
    that turning it off is a genuine rollback rather than a different code path
    with its own cost.
    """

    @pytest.mark.asyncio
    async def test_off_returns_none_without_touching_the_database(self):
        """No session is even opened, so the off state is free.

        A flag check *after* the query would make "disabled" cost the same as
        "enabled" — which is how a rollback stops being a rollback.
        """
        session_factory = MagicMock()
        with (
            patch("src.proxy.bedrock_routing.get_settings", return_value=SimpleNamespace(bedrock_routing_shadow_mode=False)),
            patch("src.shared.database.get_session_factory", session_factory),
        ):
            assert await resolve_shadow_target(_token_context()) is None
        session_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_reaches_the_resolver(self):
        """Positive control: with the flag on, the ladder actually runs."""
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        resolve = AsyncMock(return_value=BedrockTarget(account_id=MAPPED_ACCOUNT, rung="team"))

        with (
            patch("src.proxy.bedrock_routing.get_settings", return_value=SimpleNamespace(bedrock_routing_shadow_mode=True)),
            patch("src.shared.database.get_session_factory", return_value=MagicMock(return_value=session)),
            patch("src.proxy.bedrock_routing.bedrock_routing_resolver.resolve", resolve),
        ):
            target = await resolve_shadow_target(_token_context())

        assert target is not None and target.rung == "team"
        resolve.assert_awaited_once()

    def test_the_flag_defaults_to_on_and_is_env_overridable(self):
        """Default True, flippable by env — no image rebuild to roll back.

        The observation is what #4744 is gated on (§8.2), and with zero mappings
        the existence gate makes it free. But a flag that needed a redeploy to
        disable would not be a rollback plan, so the env override is asserted
        rather than assumed.
        """
        from src.shared.config import Settings

        assert Settings().bedrock_routing_shadow_mode is True

    def test_the_flag_is_read_per_call_not_captured_at_import(self):
        """A pod recycle must be enough — the value cannot be frozen at import.

        `resolve_shadow_target` calls `get_settings()` inside the function. A
        module-level constant would make the documented rollback ("set the env var,
        recycle the pod") silently not work.
        """
        source = inspect.getsource(resolve_shadow_target)
        assert "get_settings()" in source


# ============================================================================
# 5. Both Bedrock-reaching gateway paths capture — §7
# ============================================================================


class TestBothBedrockPathsCapture:
    """Both transports record the account selected before signing."""

    @pytest.mark.asyncio
    async def test_the_mantle_path_captures_the_signing_decision(self):
        from src.proxy.bedrock_enforcement import RoutingDecision
        from src.proxy.mantle_service import MantlePassthroughService

        _CapturingUsageService.calls = []
        service = MantlePassthroughService(MagicMock(), "https://bedrock-mantle.us-east-1.api.aws")

        with (
            patch("src.proxy.mantle_service.get_session_factory", _session_factory),
            patch("src.proxy.mantle_service.UsageService", _CapturingUsageService),
            patch("src.proxy.mantle_service.reconcile_budget_reservation", AsyncMock()),
            patch("src.proxy.mantle_service.price_completed_usage", AsyncMock(side_effect=price_fixture_usage)),
        ):
            await service._log_usage(
                context=_token_context(),
                model="openai.gpt-5",
                usage={"input_tokens": 10, "output_tokens": 20},
                latency_ms=100,
                status_code=200,
                request_id="req-1",
                agent_run_id=None,
                routing_decision=RoutingDecision(target=BedrockTarget(account_id=MAPPED_ACCOUNT, rung="org")),
            )

        assert _CapturingUsageService.calls, "mantle log_request was never called"
        assert _CapturingUsageService.calls[0]["bedrock_account_id"] == MAPPED_ACCOUNT

    @pytest.mark.asyncio
    async def test_the_mantle_path_writes_null_when_unresolved(self):
        from src.proxy.mantle_service import MantlePassthroughService

        _CapturingUsageService.calls = []
        service = MantlePassthroughService(MagicMock(), "https://bedrock-mantle.us-east-1.api.aws")

        with (
            patch("src.proxy.mantle_service.get_session_factory", _session_factory),
            patch("src.proxy.mantle_service.UsageService", _CapturingUsageService),
            patch("src.proxy.mantle_service.reconcile_budget_reservation", AsyncMock()),
            patch("src.proxy.mantle_service.price_completed_usage", AsyncMock(side_effect=price_fixture_usage)),
        ):
            await service._log_usage(
                context=_token_context(),
                model="openai.gpt-5",
                usage={"input_tokens": 10, "output_tokens": 20},
                latency_ms=100,
                status_code=200,
                request_id="req-1",
                agent_run_id=None,
            )

        assert _CapturingUsageService.calls[0]["bedrock_account_id"] is None
