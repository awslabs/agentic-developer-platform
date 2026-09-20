"""Provisioning runs under an authorized operation, or it does not run.

Issue #5052 (U17a), EPIC #4910. R14, ADP half, acceptance 2 (logic half only).

This file is the story's smoke check:

    python3 -m pytest modules/domain-apps/superplane/tests/test_provisioning_adapter.py -q

## What a green run here does and does not establish

**Does:** the adapter cannot be reached without an operation binding; a
caller-supplied identity is refused; outcome is read through the facade rather
than from adapter state; the adapter touches no credential surface.

**Does not:** anything about real provisioning. Every facade in this file is a
**mock**, and `TestTheFacadeIsAMock` asserts that explicitly so a green run cannot
be misread as live execution. Per `acceptance-split.md` rule 2, mock success closes
no live criterion — **R14 acceptance 2 (live)** stays open, gated on a named
account/environment, B's facade actually being built, spend authorization and a
named cleanup owner, none of which are resolved.

That assertion is unusual and deliberate. The failure mode it addresses is a
reviewer or an operator seeing a green U17a suite and concluding provisioning
works. It does not: it establishes that *if* the facade behaves as B publishes,
the adapter's authority logic is correct.

## Why the mock is hand-written rather than `unittest.mock.Mock`

A bare `Mock()` satisfies any attribute access, so a test using one passes whether
the adapter calls `report_progress` or `reportProgress` or a method that no longer
exists. That makes the mock unable to detect the drift it is standing in for.

`MockOperationFacade` below implements exactly the `OperationFacade` protocol
surface and records its calls, so the tests can assert *ordering* (started before
finished, outcome read after) — which is what the contract actually constrains. It
is still unambiguously a mock: `is_mock` is `True`, and the class name says so.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from superplane_contracts import (
    FORBIDDEN_PARAMETER_KEYS,
    INCONCLUSIVE_STATES,
    PROVISION,
    REQUIRED_PERMISSION,
    TEARDOWN,
    TERMINAL_STATES,
    ContractViolation,
    OperationBinding,
    OperationFacade,
    OperationState,
    ProvisioningAdapter,
    ProvisioningIntent,
    ProvisioningProgress,
    ProvisioningProvider,
    ProvisioningRefused,
    ResolvedPrincipal,
    forbidden_parameters,
    summarize,
)

# A fixed, timezone-aware instant, matching conftest.py's OBSERVED_AT discipline:
# these tests assert on validation branches and ordering, never on "now".
NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)

# The bound tenant. Deliberately distinct values so a test asserting "the org came
# from the binding" cannot pass by coincidence against the workspace.
SUBJECT = "principal-abc"
ORG = "org-acme"
WORKSPACE = "ws-w1"

OPERATION_ID = "op-01JQ8ZKRW4X7N2VYB3M6E5T9DA"


def clock() -> datetime:
    """The receiver's clock, fixed. Injected so no test patches time."""
    return NOW


# ---------------------------------------------------------------------------
# The doubles. Both are mocks, and say so.
# ---------------------------------------------------------------------------


@dataclass
class MockOperationFacade:
    """A stand-in for B's scoped trusted-operation contract.

    **This is a mock.** B's operation facade does not exist in ADP today — there is
    no `modules/harness/jobs/` — so there is nothing real to integrate against.
    Recorded as a mock per `acceptance-split.md` rule 5.

    Records calls in order so tests can assert the adapter told the facade the
    operation started before it reported it finished, which is the property that
    makes the facade's record reflect reality rather than trail it.
    """

    is_mock: bool = True
    """Asserted by `TestTheFacadeIsAMock`. Not decoration — see the file docstring."""

    reported_state: OperationState = OperationState.SUCCEEDED
    reported_detail: str | None = None
    report_operation_id: str | None = None
    """Overridden by one test to simulate a facade reporting the wrong operation."""

    calls: list[str] = field(default_factory=list)

    def report_progress(self, operation_id: str) -> ProvisioningProgress:
        self.calls.append(f"report_progress:{operation_id}")
        return ProvisioningProgress(
            operation_id=self.report_operation_id or operation_id,
            state=self.reported_state,
            observed_at=NOW,
            detail=self.reported_detail,
        )

    def record_started(self, operation_id: str) -> None:
        self.calls.append(f"record_started:{operation_id}")

    def record_finished(self, operation_id: str, *, failed: bool) -> None:
        self.calls.append(f"record_finished:{operation_id}:failed={failed}")


@dataclass
class MockProvider:
    """A stand-in for the thing that would create real infrastructure.

    **Also a mock.** It provisions nothing. `parameters_seen` lets a test assert
    which parameters reached the provider — used to prove the adapter does not
    quietly forward an identity field it should have refused outright.
    """

    is_mock: bool = True
    raises: Exception | None = None
    calls: list[str] = field(default_factory=list)
    parameters_seen: list[dict[str, str]] = field(default_factory=list)
    bindings_seen: list[OperationBinding] = field(default_factory=list)

    def provision(
        self, parameters: dict[str, str], *, binding: OperationBinding
    ) -> None:
        self.calls.append("provision")
        self.parameters_seen.append(dict(parameters))
        self.bindings_seen.append(binding)
        if self.raises is not None:
            raise self.raises

    def teardown(
        self, parameters: dict[str, str], *, binding: OperationBinding
    ) -> None:
        self.calls.append("teardown")
        self.parameters_seen.append(dict(parameters))
        self.bindings_seen.append(binding)
        if self.raises is not None:
            raise self.raises


@pytest.fixture
def facade() -> MockOperationFacade:
    return MockOperationFacade()


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def adapter(facade: MockOperationFacade, provider: MockProvider) -> ProvisioningAdapter:
    return ProvisioningAdapter(facade=facade, provider=provider, clock=clock)


def principal(
    *, subject: str = SUBJECT, org_id: str = ORG, workspace_id: str = WORKSPACE
) -> ResolvedPrincipal:
    return ResolvedPrincipal(subject=subject, org_id=org_id, workspace_id=workspace_id)


def binding(
    *,
    action: str = PROVISION,
    expires_at: datetime | None = None,
    operation_id: str = OPERATION_ID,
) -> OperationBinding:
    """A binding as the facade would issue it.

    Constructed here rather than in each test because *how* a binding is obtained
    is the whole subject of this file: in production it comes from the facade, and
    a locally constructed one is not proof of provenance (the adapter cannot check
    that, which is part of why the live criterion stays open).
    """
    return OperationBinding(
        operation_id=operation_id,
        principal=principal(),
        action=action,
        permission=REQUIRED_PERMISSION,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Acceptance 2, first half: initiation is through the facade
# ---------------------------------------------------------------------------


class TestInitiatedThroughTheFacade:
    """The adapter runs under an authorized operation, with a resolved principal."""

    def test_provisioning_runs_under_an_operation_binding(
        self, adapter: ProvisioningAdapter, facade: MockOperationFacade, provider
    ) -> None:
        progress = adapter.run(binding(), ProvisioningIntent(action=PROVISION))

        assert provider.calls == ["provision"]
        assert progress.establishes_provisioned is True
        # The facade was told the operation started and finished, in that order,
        # and the outcome was read from it afterwards.
        assert facade.calls == [
            f"record_started:{OPERATION_ID}",
            f"record_finished:{OPERATION_ID}:failed=False",
            f"report_progress:{OPERATION_ID}",
        ]

    def test_teardown_also_runs_under_a_binding(
        self, adapter: ProvisioningAdapter, provider: MockProvider
    ) -> None:
        """Teardown is the same authority question with the opposite effect."""
        adapter.run(binding(action=TEARDOWN), ProvisioningIntent(action=TEARDOWN))
        assert provider.calls == ["teardown"]

    def test_the_principal_comes_from_the_binding(self) -> None:
        """There is no way to construct an intent that names a principal.

        Asserted structurally rather than behaviourally: the property is that the
        field does not exist, and a behavioural test could only show that one
        particular value was ignored.
        """
        intent_fields = set(inspect.signature(ProvisioningIntent).parameters)
        assert intent_fields == {"action", "parameters"}
        for forbidden in ("user_id", "org_id", "workspace_id", "principal", "subject"):
            assert forbidden not in intent_fields

    def test_direct_invocation_without_a_binding_is_refused(
        self, adapter: ProvisioningAdapter, provider: MockProvider
    ) -> None:
        """The story's required negative: no binding, no provisioning.

        Passing `None` is what untyped calling code does when it has no operation
        record. It must be a refusal, not an AttributeError a broad `except`
        upstream could swallow into a retry.
        """
        with pytest.raises(ProvisioningRefused):
            adapter.run(None, ProvisioningIntent(action=PROVISION))
        assert provider.calls == []

    def test_a_lookalike_binding_is_refused(
        self, adapter: ProvisioningAdapter, provider: MockProvider
    ) -> None:
        """A duck-typed object with the right attribute names is not a binding.

        This is the shape a caller reaches for to skip the facade: a small local
        object carrying an operation_id and a principal. Refusing it is what makes
        "through the facade" structural instead of advisory.
        """

        @dataclass
        class NotABinding:
            operation_id: str = OPERATION_ID
            principal: ResolvedPrincipal = field(default_factory=principal)
            action: str = PROVISION
            permission: str = REQUIRED_PERMISSION
            expires_at: datetime | None = None

            def is_expired(self, now: datetime) -> bool:
                return False

        with pytest.raises(ProvisioningRefused, match="issued by the facade"):
            adapter.run(NotABinding(), ProvisioningIntent(action=PROVISION))
        assert provider.calls == []

    def test_an_expired_authorization_is_refused(
        self, adapter: ProvisioningAdapter, provider: MockProvider
    ) -> None:
        expired = binding(expires_at=NOW - timedelta(seconds=1))
        with pytest.raises(ProvisioningRefused, match="expired"):
            adapter.run(expired, ProvisioningIntent(action=PROVISION))
        assert provider.calls == []

    def test_an_unexpired_authorization_runs(
        self, adapter: ProvisioningAdapter, provider: MockProvider
    ) -> None:
        live = binding(expires_at=NOW + timedelta(minutes=5))
        adapter.run(live, ProvisioningIntent(action=PROVISION))
        assert provider.calls == ["provision"]

    def test_an_unbounded_authorization_is_named_as_such(self) -> None:
        """No expiry is reported honestly rather than treated as safe.

        B publishes no expiry semantics for an operation binding, so the contract
        cannot require one. What it can do is refuse to let "no expiry" read as
        "bounded" — `unbounded_authority` is the field a consumer checks.
        """
        assert binding().unbounded_authority is True
        assert binding(expires_at=NOW).unbounded_authority is False

    def test_action_mismatch_between_binding_and_intent_is_refused(
        self, adapter: ProvisioningAdapter, provider: MockProvider
    ) -> None:
        """A teardown under a provision binding is a destructive mismatch."""
        with pytest.raises(ProvisioningRefused, match="does not match"):
            adapter.run(binding(action=PROVISION), ProvisioningIntent(action=TEARDOWN))
        assert provider.calls == []

    def test_the_adapter_rechecks_the_permission_it_depends_on(
        self, adapter: ProvisioningAdapter, provider: MockProvider
    ) -> None:
        """The adapter's own permission check holds if the constructor's is bypassed.

        `OperationBinding.__post_init__` already refuses a binding carrying a
        weaker permission, so this state is unreachable through normal
        construction. It is reachable via `object.__setattr__` on a frozen
        dataclass, which is what this test does — not because a caller would, but
        because it is the only way to exercise the adapter's independent check.

        That check is worth having and worth testing: `_check_binding`'s contract is
        "this binding authorizes provisioning", and it should not silently become a
        no-op if the constructor's validation is ever relaxed, reordered, or moved.
        A defense-in-depth branch nothing exercises is indistinguishable from one
        that has already stopped working.
        """
        smuggled = binding()
        object.__setattr__(smuggled, "permission", "workspace:read")

        with pytest.raises(ProvisioningRefused, match="does not authorize"):
            adapter.run(smuggled, ProvisioningIntent(action=PROVISION))
        assert provider.calls == []

    def test_a_binding_that_does_not_authorize_provisioning_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match=REQUIRED_PERMISSION):
            OperationBinding(
                operation_id=OPERATION_ID,
                principal=principal(),
                action=PROVISION,
                permission="workspace:read",
            )

    def test_the_required_permission_matches_u9s_authority_model(self) -> None:
        """The contract's permission string agrees with U9's enum.

        `contracts/` is standard-library-only and importable independently of
        `auth/`, so the string is duplicated rather than imported (see the comment
        on `REQUIRED_PERMISSION`). This test is what makes the duplicate safe: if
        U9 renames the permission, this fails instead of the two drifting into
        silently different authority models.
        """
        import sys
        from pathlib import Path

        auth_root = Path(__file__).resolve().parent.parent / "auth"
        if str(auth_root) not in sys.path:
            sys.path.insert(0, str(auth_root))
        from superplane_auth.policy import Permission

        assert REQUIRED_PERMISSION == Permission.PROVISION.value


# ---------------------------------------------------------------------------
# Acceptance 2, second half: a body-supplied identity is not authority
# ---------------------------------------------------------------------------


class TestBodySuppliedIdentityIsRejected:
    """A caller cannot name the tenant it provisions for.

    Design §6 lines 398-407 forbid a body-supplied user or org ID being authority.
    """

    @pytest.mark.parametrize(
        "key",
        ["user_id", "org_id", "workspace_id", "tenant_id", "on_behalf_of", "subject"],
    )
    def test_an_identity_parameter_is_refused(
        self, adapter: ProvisioningAdapter, provider: MockProvider, key: str
    ) -> None:
        intent = ProvisioningIntent(action=PROVISION, parameters=((key, "anything"),))
        with pytest.raises(ProvisioningRefused, match="may not assert an identity"):
            adapter.run(binding(), intent)
        assert provider.calls == []

    def test_rejected_even_when_the_value_matches_the_bound_principal(
        self, adapter: ProvisioningAdapter, provider: MockProvider
    ) -> None:
        """The story's required case, and the one that looks harmless.

        `org_id` here is exactly the bound principal's org. It is still refused.

        The reason is worth restating because "it matched, so no harm done" is the
        argument that deletes this test: a caller-supplied identity that agrees
        with the binding is still a code path that *reads the caller's value*. Once
        that path exists, only a comparison prevents a mismatched value being
        honoured — and comparisons get reordered, cached or made conditional.
        Refusing regardless means no such path exists at all.
        """
        intent = ProvisioningIntent(
            action=PROVISION,
            parameters=(("org_id", ORG), ("workspace_id", WORKSPACE)),
        )
        with pytest.raises(ProvisioningRefused, match="may not assert an identity"):
            adapter.run(binding(), intent)
        assert provider.calls == []
        assert provider.parameters_seen == []

    def test_case_variants_are_refused(
        self, adapter: ProvisioningAdapter, provider: MockProvider
    ) -> None:
        """`Org_Id` is the same smuggling attempt as `org_id`."""
        intent = ProvisioningIntent(action=PROVISION, parameters=(("Org_ID", ORG),))
        with pytest.raises(ProvisioningRefused):
            adapter.run(binding(), intent)

    def test_prefixed_identity_parameters_are_refused(
        self, adapter: ProvisioningAdapter
    ) -> None:
        """The rule is a prefix family, not a fixed list.

        Same reasoning U9's `strip_identity_headers` gives: "strip the ones we
        thought of" is the failure mode, because a key added later for a new plane
        is accepted by default unless the rule is stated as a prefix.
        """
        for key in ("x-caller-org", "adp_user", "auth_subject", "caller_identity"):
            intent = ProvisioningIntent(action=PROVISION, parameters=((key, "v"),))
            with pytest.raises(ProvisioningRefused):
                adapter.run(binding(), intent)

    def test_legitimate_parameters_are_forwarded(
        self, adapter: ProvisioningAdapter, provider: MockProvider
    ) -> None:
        """The check refuses identity, not shape.

        Provisioning genuinely needs an instance type and a region, and a rule so
        broad it blocked those would be worked around rather than followed.
        """
        intent = ProvisioningIntent(
            action=PROVISION,
            parameters=(("instance_type", "g5.xlarge"), ("region", "us-east-1")),
        )
        adapter.run(binding(), intent)
        assert provider.parameters_seen == [
            {"instance_type": "g5.xlarge", "region": "us-east-1"}
        ]

    def test_forbidden_parameters_names_the_offending_keys(self) -> None:
        """A refusal can say what to remove without echoing the values."""
        intent = ProvisioningIntent(
            action=PROVISION,
            parameters=(("region", "us-east-1"), ("org_id", ORG), ("user", "someone")),
        )
        assert forbidden_parameters(intent) == ("org_id", "user")

    def test_an_intent_with_no_identity_keys_is_clean(self) -> None:
        intent = ProvisioningIntent(
            action=PROVISION, parameters=(("instance_type", "g5.xlarge"),)
        )
        assert forbidden_parameters(intent) == ()

    def test_the_forbidden_set_covers_the_designs_named_fields(self) -> None:
        """The two fields the design names explicitly must both be present."""
        assert "user_id" in FORBIDDEN_PARAMETER_KEYS
        assert "org_id" in FORBIDDEN_PARAMETER_KEYS


# ---------------------------------------------------------------------------
# Acceptance 2, third half: progress is observed through the facade
# ---------------------------------------------------------------------------


class TestProgressIsObservedThroughTheFacade:
    """Outcome comes from the facade. Nothing here reads adapter state.

    No assertion in this class touches an attribute of `ProvisioningAdapter`. That
    is the story's constraint, and it is enforceable rather than aspirational
    because `ProvisioningAdapter` is a frozen dataclass with no status field —
    `test_the_adapter_holds_no_status_field` asserts there is nothing to read.
    """

    def test_the_adapter_holds_no_status_field(
        self, adapter: ProvisioningAdapter
    ) -> None:
        """There is no adapter-internal status a caller could read instead."""
        state_like = {
            name
            for name in vars(adapter)
            if any(
                token in name.lower()
                for token in ("status", "state", "progress", "result", "outcome")
            )
        }
        assert state_like == set()

    def test_outcome_is_read_from_the_facades_report(
        self, adapter: ProvisioningAdapter, facade: MockOperationFacade
    ) -> None:
        facade.reported_state = OperationState.FAILED
        progress = adapter.run(binding(), ProvisioningIntent(action=PROVISION))
        # The provider call did not raise, yet the reported outcome is FAILED,
        # because the facade is what determines the operation's outcome. An adapter
        # that concluded success from "the provider call returned" would report the
        # opposite here — which is the "reports success while the provider has
        # provisioned nothing" failure.
        assert progress.state is OperationState.FAILED
        assert progress.establishes_provisioned is False

    def test_observe_reads_current_progress_through_the_facade(
        self, adapter: ProvisioningAdapter, facade: MockOperationFacade
    ) -> None:
        facade.reported_state = OperationState.RUNNING
        progress = adapter.observe(binding())
        assert progress.state is OperationState.RUNNING
        assert progress.is_terminal is False
        assert facade.calls == [f"report_progress:{OPERATION_ID}"]

    def test_observing_with_an_expired_binding_is_refused(
        self, adapter: ProvisioningAdapter, facade: MockOperationFacade
    ) -> None:
        """Read authorization is checked, not inherited from having once run.

        Progress for a tenant's operation is information about that tenant's
        estate, so it gets its own check — the same reasoning `scoping.py` gives
        for not treating read scoping as the lesser half of write scoping.
        """
        with pytest.raises(ProvisioningRefused, match="expired"):
            adapter.observe(binding(expires_at=NOW - timedelta(seconds=1)))
        assert facade.calls == []

    def test_progress_for_a_different_operation_is_refused(
        self, adapter: ProvisioningAdapter, facade: MockOperationFacade
    ) -> None:
        """A report that names another operation must not be accepted.

        The named-vs-positional rule: with concurrent operations, accepting a
        mismatched report lets one operation's success be read as another's.
        """
        facade.report_operation_id = "op-someone-elses"
        with pytest.raises(ContractViolation, match="different operation"):
            adapter.run(binding(), ProvisioningIntent(action=PROVISION))

    def test_a_malformed_facade_report_is_refused(self, provider: MockProvider) -> None:
        """A facade returning the wrong type is a contract breach, surfaced here."""

        @dataclass
        class BadFacade:
            is_mock: bool = True

            def report_progress(self, operation_id: str) -> object:
                return {"state": "succeeded"}

            def record_started(self, operation_id: str) -> None: ...

            def record_finished(self, operation_id: str, *, failed: bool) -> None: ...

        bad = ProvisioningAdapter(facade=BadFacade(), provider=provider, clock=clock)
        with pytest.raises(ContractViolation, match="not a ProvisioningProgress"):
            bad.run(binding(), ProvisioningIntent(action=PROVISION))

    def test_a_provider_failure_is_recorded_and_reraised(
        self, facade: MockOperationFacade, provider: MockProvider
    ) -> None:
        """The facade's record reflects that the attempt ended, and the caller sees why."""
        provider.raises = RuntimeError("capacity unavailable")
        adapter = ProvisioningAdapter(facade=facade, provider=provider, clock=clock)

        with pytest.raises(RuntimeError, match="capacity unavailable"):
            adapter.run(binding(), ProvisioningIntent(action=PROVISION))

        assert facade.calls == [
            f"record_started:{OPERATION_ID}",
            f"record_finished:{OPERATION_ID}:failed=True",
        ]

    def test_unknown_is_not_a_failure(
        self, adapter: ProvisioningAdapter, facade: MockOperationFacade
    ) -> None:
        """An unresolved outcome is terminal but establishes nothing.

        A consumer collapsing UNKNOWN into failure either leaks resources it
        believes were never created, or retries a provision that succeeded.
        """
        facade.reported_state = OperationState.UNKNOWN
        facade.reported_detail = "provider API timed out after dispatch"
        progress = adapter.run(binding(), ProvisioningIntent(action=PROVISION))

        assert progress.state is OperationState.UNKNOWN
        assert progress.is_terminal is True
        assert progress.establishes_provisioned is False
        assert progress.state in INCONCLUSIVE_STATES

    def test_terminal_and_inconclusive_states_are_stated_once(self) -> None:
        """Both sets exist so a consumer never hand-rolls the classification."""
        assert TERMINAL_STATES == {
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.UNKNOWN,
        }
        # UNKNOWN is in both: no more progress is coming, and nothing is concluded.
        assert OperationState.UNKNOWN in TERMINAL_STATES & INCONCLUSIVE_STATES
        assert OperationState.SUCCEEDED not in INCONCLUSIVE_STATES

    def test_summarize_does_not_render_unknown_as_a_failure(self) -> None:
        unknown = ProvisioningProgress(
            operation_id=OPERATION_ID,
            state=OperationState.UNKNOWN,
            observed_at=NOW,
            detail="provider API timed out",
        )
        line = summarize(unknown)
        assert "not a failure" in line
        assert "provider API timed out" in line

    def test_summarize_leaks_no_principal(self) -> None:
        """Operation summaries are logged; a tenant id must not ride along."""
        line = summarize(
            ProvisioningProgress(
                operation_id=OPERATION_ID,
                state=OperationState.SUCCEEDED,
                observed_at=NOW,
            )
        )
        assert ORG not in line
        assert SUBJECT not in line
        assert WORKSPACE not in line

    def test_summarize_covers_every_state(self) -> None:
        for state in OperationState:
            detail = "unresolved" if state is OperationState.UNKNOWN else None
            line = summarize(
                ProvisioningProgress(
                    operation_id=OPERATION_ID,
                    state=state,
                    observed_at=NOW,
                    detail=detail,
                )
            )
            assert OPERATION_ID in line

    def test_an_unknown_outcome_must_explain_itself(self) -> None:
        """The least actionable report must at least say what is unresolved."""
        with pytest.raises(ContractViolation, match="detail"):
            ProvisioningProgress(
                operation_id=OPERATION_ID,
                state=OperationState.UNKNOWN,
                observed_at=NOW,
            )
        # Any other state needs no detail.
        assert (
            ProvisioningProgress(
                operation_id=OPERATION_ID, state=OperationState.FAILED, observed_at=NOW
            ).detail
            is None
        )


# ---------------------------------------------------------------------------
# The substitution line: no broader-authority surface is reached for
# ---------------------------------------------------------------------------


class TestNoCredentialSurfaceIsTouched:
    """The adapter reads no secret and calls no credential-management endpoint.

    `repo-path-allocation.md` is explicit that the vault's credential-management
    endpoints (`POST`/`DELETE /auth/credentials`) are **not** substitutable for B's
    contract: they administer a *user's* stored credentials and carry no run
    binding, no expiry and no run-tied revocation. Reuse of the vault *boundary* is
    not permission to substitute a broader-authority endpoint for the missing one.

    Asserted over the source text, which is the right level for an absence claim: a
    behavioural test can only show that the surfaces were not reached *on the paths
    the test exercised*, whereas this covers every path including ones no test
    calls.
    """

    @staticmethod
    def _sources() -> dict[str, str]:
        from pathlib import Path

        root = (
            Path(__file__).resolve().parent.parent
            / "contracts"
            / "superplane_contracts"
        )
        return {
            name: (root / name).read_text()
            for name in ("provisioning.py", "provisioning_adapter.py")
        }

    @staticmethod
    def _code_only(source: str) -> str:
        """Strip comments and docstrings, leaving executable text.

        Necessary because both modules *discuss* the endpoints they must not call,
        at length and on purpose. A naive substring search over the raw file would
        fail on the documentation explaining why the call is forbidden, which would
        make the honest thing to do — writing down the reason — the thing that
        breaks the test.
        """
        import ast

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                node.value.value = ""
        return ast.unparse(tree)

    @pytest.mark.parametrize(
        "forbidden",
        [
            "/auth/credentials",
            "secretsmanager",
            "get_secret_value",
            "SecretsManagerHelper",
            "vault_routes",
            "boto3",
            "requests",
            "httpx",
            "urllib",
        ],
    )
    def test_no_credential_or_transport_surface_in_code(self, forbidden: str) -> None:
        for name, source in self._sources().items():
            code = self._code_only(source)
            assert forbidden.lower() not in code.lower(), (
                f"{name} references {forbidden!r} in executable code. "
                "The adapter must consume B's trusted-operation contract, not a "
                "credential-management endpoint or a raw secret read."
            )

    def test_no_gateway_internal_import(self) -> None:
        """The vault is reused as a boundary, not imported into the domain module."""
        for name, source in self._sources().items():
            code = self._code_only(source)
            assert "from src." not in code, f"{name} imports gateway internals"
            assert "import src" not in code, f"{name} imports gateway internals"

    def test_no_domain_database_write(self) -> None:
        """Domain writes remain the upstream API's — this adapter performs none."""
        for name, source in self._sources().items():
            code = self._code_only(source).lower()
            for forbidden in (
                "insert into",
                "update ",
                "sqlalchemy",
                "session.",
                "cursor",
            ):
                assert forbidden not in code, f"{name} looks like it writes a record"

    def test_the_contract_confers_no_budget_authority(self) -> None:
        """No local budget verdict, even behind a flag (M6).

        The same line `observation.py` holds for `BudgetUsage`: this side reports
        and requests; admission-time enforcement is B's.
        """
        for name, source in self._sources().items():
            code = self._code_only(source).lower()
            for forbidden in ("budget_exceeded", "budget_ok", "spend_limit"):
                assert forbidden not in code, f"{name} asserts budget authority"


# ---------------------------------------------------------------------------
# The honesty assertion
# ---------------------------------------------------------------------------


class TestTheFacadeIsAMock:
    """A green run here is not evidence of live provisioning.

    The story requires this explicitly, and it is the most important class in the
    file for how the result gets *reported*. Everything above establishes the
    adapter's authority logic against a double. B's facade does not exist in ADP —
    there is no `modules/harness/jobs/` — so no test here can do more.
    """

    def test_the_facade_double_is_a_mock(self, facade: MockOperationFacade) -> None:
        assert facade.is_mock is True
        assert type(facade).__name__.startswith("Mock")

    def test_the_provider_double_is_a_mock(self, provider: MockProvider) -> None:
        assert provider.is_mock is True
        assert type(provider).__name__.startswith("Mock")

    def test_no_real_operation_facade_is_composed_into_this_app(self) -> None:
        """The premise of the mock, asserted rather than assumed.

        Fires when the real facade becomes *reachable from this app* — the correct
        trigger to revisit whether the mock is still the right double and whether R14
        acceptance 2 can be attempted. A mock whose premise silently expires is how a
        story stays mocked long after it needed to be.

        ## Why this no longer tests for the directory

        It used to assert `modules/harness/jobs/` does not exist. That fired when
        #5525 (w6-02) landed the shared store, and it was checked as the message
        asks: the package is built and tested, but **nothing composes it**. It opens
        no connection and is imported nowhere under `src/` or `contracts/`, so the
        adapter still runs against a double and every "does not establish" statement
        in this file's header still holds, unchanged.

        So the directory was the wrong thing to watch. A package existing is not a
        dependency; being imported is. Retargeted at the boundary that actually
        changes the meaning of a green run here rather than deleted, because deleting
        it would remove the trigger for the transition it exists to catch — and
        left as an existence check it would now fail forever, which trains a reader
        to ignore it.
        """
        import importlib.util

        composed = importlib.util.find_spec("harness_jobs")
        assert composed is None, (
            "`harness_jobs` is importable from this app, so B's operation facade may "
            "now be composed rather than mocked. Re-examine whether "
            "MockOperationFacade should be replaced and whether R14 acceptance 2 "
            "(live) can now be attempted. Note that importability is still not "
            "live evidence: a composed facade needs its schema applied and its "
            "delivery loop running before any live criterion is closable."
        )

    def test_the_mock_implements_exactly_the_consumed_protocol(
        self, facade: MockOperationFacade, provider: MockProvider
    ) -> None:
        """The double satisfies the protocol the adapter declares.

        `isinstance` against a `runtime_checkable` Protocol checks method presence
        only, not signatures — so this is a floor, not proof of fidelity. It is
        here to catch the mock drifting away from the declared surface, not to
        stand in for integration against the real thing.
        """
        assert isinstance(facade, OperationFacade)
        assert isinstance(provider, ProvisioningProvider)


# ---------------------------------------------------------------------------
# Contract well-formedness
# ---------------------------------------------------------------------------


class TestContractGuards:
    """Illegal states are unconstructible, following U8's discipline.

    Every guard raises `ContractViolation`, so a receiver catching that one type
    catches all of them.
    """

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"subject": ""}, "subject"),
            ({"subject": "   "}, "subject"),
            ({"org_id": ""}, "organization"),
            ({"workspace_id": ""}, "workspace"),
        ],
    )
    def test_a_principal_must_be_fully_resolved(self, kwargs, match) -> None:
        with pytest.raises(ContractViolation, match=match):
            principal(**kwargs)

    def test_an_empty_operation_id_is_refused(self) -> None:
        """Every progress report and cancellation would bind to the same target."""
        with pytest.raises(ContractViolation, match="operation_id"):
            OperationBinding(
                operation_id="",
                principal=principal(),
                action=PROVISION,
                permission=REQUIRED_PERMISSION,
            )

    def test_an_unknown_action_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="unknown provisioning action"):
            OperationBinding(
                operation_id=OPERATION_ID,
                principal=principal(),
                action="escalate",
                permission=REQUIRED_PERMISSION,
            )
        with pytest.raises(ContractViolation, match="unknown provisioning action"):
            ProvisioningIntent(action="escalate")

    def test_a_naive_expiry_is_refused(self) -> None:
        """Same clock ambiguity the lease contract refuses."""
        with pytest.raises(ContractViolation, match="timezone-aware"):
            OperationBinding(
                operation_id=OPERATION_ID,
                principal=principal(),
                action=PROVISION,
                permission=REQUIRED_PERMISSION,
                expires_at=datetime(2026, 9, 16, 12, 0, 0),
            )

    def test_is_expired_requires_an_aware_clock(self) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            binding().is_expired(datetime(2026, 9, 16, 12, 0, 0))

    def test_progress_must_name_its_operation(self) -> None:
        with pytest.raises(ContractViolation, match="name the operation"):
            ProvisioningProgress(
                operation_id="", state=OperationState.SUCCEEDED, observed_at=NOW
            )

    def test_progress_requires_an_aware_timestamp(self) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            ProvisioningProgress(
                operation_id=OPERATION_ID,
                state=OperationState.SUCCEEDED,
                observed_at=datetime(2026, 9, 16, 12, 0, 0),
            )

    def test_a_binding_carries_the_contract_version(self) -> None:
        """The version travels with the record, as U8's observations do."""
        from superplane_contracts import CONTRACT_VERSION

        assert binding().contract_version == CONTRACT_VERSION


# ---------------------------------------------------------------------------
# The golden fixture, executed
# ---------------------------------------------------------------------------


def _golden() -> dict:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent / "fixtures" / "operation-facade.golden.json"
    return json.loads(path.read_text())


class TestGoldenFixtureIsExecuted:
    """`operation-facade.golden.json` is run, not merely shipped.

    The executed-contract convention from `contracts/hitl-ticket/v1/`, whose own
    fixture states the reason: "A golden fixture prevents nothing if nothing
    executes it." A fixture that no test loads drifts from the code silently and
    still reads, in review, as though it were enforced.

    Provenance for every field asserted here is in
    `contracts/PROVISIONING-CONTRACT.md`.
    """

    def test_the_substrate_matches_what_b_published(self) -> None:
        """`name`/`version`/`owner` — the one part of B's envelope that is not aspirational.

        Asserted against the fixture rather than hardcoded in the adapter, because
        the adapter does not consume the substrate: it is the envelope B's registry
        needs, recorded here so the fixture is traceable to a published source
        instead of to this module's expectations.
        """
        substrate = {
            k: v for k, v in _golden()["substrate"].items() if not k.startswith("$")
        }
        # Compared exactly, minus `$comment` annotations (the hitl-ticket fixture
        # convention). Exact rather than a subset check: the substrate is defined as
        # the minimum the harness needs, so an extra field here would be this side
        # adding to B's envelope, which is the invention the provenance rule forbids.
        assert substrate == {"name": "job", "version": 1, "owner": "harness/jobs"}

    def test_the_fixture_records_every_accepted_report_as_constructible(self) -> None:
        """Each accepted shape builds a real ProvisioningProgress.

        This is the half that catches drift: if a guard is added or tightened, a
        fixture claiming these shapes are accepted starts failing here rather than
        quietly becoming false.
        """
        reports = _golden()["accepted_progress_reports"]
        assert len(reports) == 3

        for report in reports:
            progress = ProvisioningProgress(
                operation_id=report["operation_id"],
                state=OperationState(report["state"]),
                observed_at=datetime.fromisoformat(report["observed_at"]),
                detail=report["detail"],
            )
            assert progress.operation_id == report["operation_id"]

    def test_the_fixtures_unknown_report_establishes_nothing(self) -> None:
        """The fixture's UNKNOWN entry behaves as the contract says it must."""
        unknown = next(
            r for r in _golden()["accepted_progress_reports"] if r["state"] == "unknown"
        )
        progress = ProvisioningProgress(
            operation_id=unknown["operation_id"],
            state=OperationState(unknown["state"]),
            observed_at=datetime.fromisoformat(unknown["observed_at"]),
            detail=unknown["detail"],
        )
        assert progress.is_terminal is True
        assert progress.establishes_provisioned is False

    def test_rejected_variants_are_actually_rejected(self) -> None:
        """Each rejected shape the contract can express is refused by a guard.

        Only the variants this contract *can* express are executed — the
        credential-bearing and adapter-status variants describe fields no type here
        has, so their rejection is asserted structurally by
        `TestNoCredentialSurfaceIsTouched` and
        `test_the_adapter_holds_no_status_field` instead. Checked below so a variant
        cannot be added to the fixture and silently go unexercised.
        """
        variants = {
            v["variant"]: v for v in _golden()["rejected_variants"] if "variant" in v
        }

        # Expressible in this contract, and each must raise.
        with pytest.raises(ContractViolation):
            ProvisioningProgress(
                operation_id="", state=OperationState.SUCCEEDED, observed_at=NOW
            )
        assert "progress_without_operation_id" in variants

        with pytest.raises(ContractViolation):
            ProvisioningProgress(
                operation_id=OPERATION_ID, state=OperationState.UNKNOWN, observed_at=NOW
            )
        assert "unknown_without_detail" in variants

        naive = variants["naive_timestamp"]["shape"]["observed_at"]
        with pytest.raises(ContractViolation, match="timezone-aware"):
            ProvisioningProgress(
                operation_id=OPERATION_ID,
                state=OperationState.SUCCEEDED,
                observed_at=datetime.fromisoformat(naive),
            )

        smuggled = variants["binding_with_caller_supplied_principal"]["shape"]
        intent = ProvisioningIntent(
            action=smuggled["action"],
            parameters=tuple(smuggled["parameters"].items()),
        )
        assert forbidden_parameters(intent) == ("org_id", "user_id")

        weaker = variants["binding_authorizing_something_weaker"]["shape"]
        with pytest.raises(ContractViolation, match=REQUIRED_PERMISSION):
            OperationBinding(
                operation_id=weaker["operation_id"],
                principal=principal(),
                action=weaker["action"],
                permission=weaker["permission"],
            )

        # Not expressible as a field of this contract — asserted elsewhere, and
        # named here so the fixture's full variant list is accounted for.
        assert "adapter_reported_success" in variants
        assert "credential_bearing_binding" in variants

    def test_the_fixture_names_the_unresolved_fields(self) -> None:
        """The gap in B's published contract is recorded, not silently filled in.

        The story forbids fixtures derived from the adapter's own expected shape.
        This is the mechanism that keeps that honest: the operation-specific fields
        B has not published are enumerated as unresolved, each with what the adapter
        does instead. If someone later replaces one with an invented value, the
        entry has to be removed from here — which is a visible edit, not a silent
        one.
        """
        unresolved = _golden()["unresolved_in_bs_published_contract"]
        expected = {
            "operation_id_format",
            "expiry_semantics",
            "progress_state_vocabulary",
            "cancellation_surface",
            "principal_resolution_mechanism",
        }
        assert expected <= set(unresolved)
        for key in expected:
            assert unresolved[key]["published"] is False
            assert unresolved[key]["adapter_behaviour"].strip()

    def test_the_fixture_states_what_it_does_not_establish(self) -> None:
        """Including, explicitly, that it does not close the live criterion."""
        disclaimers = " ".join(_golden()["what_this_fixture_does_not_establish"])
        assert "R14 acceptance 2 (live)" in disclaimers
        assert "mock" in disclaimers.lower()


@pytest.mark.parametrize("action", [PROVISION, TEARDOWN])
def test_provider_receives_the_authorized_workspace(action, adapter, provider):
    from dataclasses import replace

    first = binding(action=action)
    second = replace(
        first,
        operation_id="other-operation",
        principal=principal(workspace_id="other-workspace"),
    )
    for operation in (first, second):
        adapter.run(operation, ProvisioningIntent(action=action))
    assert provider.bindings_seen == [first, second]
    assert [b.principal.workspace_id for b in provider.bindings_seen] == [
        WORKSPACE,
        "other-workspace",
    ]
    assert provider.parameters_seen == [{}, {}]


def test_missing_principal_refused_at_binding_boundary():
    from dataclasses import replace

    with pytest.raises(ContractViolation, match="resolved principal"):
        replace(binding(), principal=None)


@pytest.mark.parametrize("key", ["orgId", "userId", "workspace-id", "auth-header"])
def test_identity_spelling_variants_never_reach_provider(key, adapter, provider):
    with pytest.raises(ProvisioningRefused):
        adapter.run(binding(), ProvisioningIntent(PROVISION, ((key, "untrusted"),)))
    assert not provider.calls


@pytest.mark.parametrize(
    "parameters",
    [[("region", "x")], (("region", "x"), ("region", "y")), (("region", {}),)],
)
def test_unstable_or_ambiguous_parameters_refused(parameters):
    with pytest.raises(ContractViolation):
        ProvisioningIntent(PROVISION, parameters)


def test_unknown_contract_version_refused():
    from dataclasses import replace

    with pytest.raises(ContractViolation, match="version"):
        replace(binding(), contract_version="unsupported")


def test_unknown_progress_state_refused():
    with pytest.raises(ContractViolation, match="state"):
        ProvisioningProgress(OPERATION_ID, "succeeded", NOW)
