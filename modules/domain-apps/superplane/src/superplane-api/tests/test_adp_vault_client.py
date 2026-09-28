"""Contract tests for the ADP vault adapter — Issue #5528 (Wave 6 / w6-05).

What these tests are for
------------------------
The adapter is the only thing standing between a Gateway HTTP response and an
authorization decision in the domain. Three classes of bug are possible here and
each gets its own section:

* **Refusal colour.** The reader port declares ``NONE_MEANS_UNVERIFIED``: a denial
  must return ``None`` and unavailability must raise, because the consumer maps those
  onto 403 and 503 respectively. Getting this backwards reports a denial as an outage
  or an outage as a denial, and those send an operator in opposite directions.
* **The ``expires_at`` mapping.** The contract's field is non-optional and the
  consumer 403s when it is in the past, but the vault sends ``null`` for a
  non-expiring credential. A naive mapping silently denies every long-lived API key —
  a total outage that no test of the happy path would catch.
* **Fail-open on the delivery half.** ``revocation_state`` must answer "revoked" when
  it cannot reach the vault. Answering "still valid" would let work proceed on a
  credential that may have been revoked.

These tests use a stub transport rather than a live Gateway. That is stated plainly
because it bounds what they prove: they verify this adapter's mapping and refusal
behaviour, not that a real Gateway answers this way. The live pairing is deferred to
the named evaluation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pickle
from datetime import datetime, timedelta, timezone

import pytest
from superplane_contracts.connections import CredentialReference, VaultOwnership
from superplane_contracts.delivery import (
    DELIVERY_PERMISSION,
    REVOCATION_LIMITATION,
    DeliveryLease,
    DeliveryRefused,
    ExecutorIdentity,
    RunBinding,
    SecretMaterial,
)

from app.adapters.adp_vault_client import (
    EVIDENCE_PATH,
    NON_EXPIRING_HORIZON,
    AdpVaultClient,
    build_vault_client,
)
from app.adapters.executor_vault_channel import (
    DELIVERY_PATH,
    PREFLIGHT_PATH,
    ExecutorVaultChannel,
)
from app.services.credential_evidence import VerifiedCredentialEvidence

ORG = "org-acme"
WORKSPACE = "ws-1"
CRED = "cred-1"
SECRET_VALUE = "sk-live-do-not-log-this-0123456789"
PRINCIPAL = "user:alice"
DIGEST = "a" * 64


# ---------------------------------------------------------------------------
# Stub transport
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code: int, body, *, raise_on_json: bool = False):
        self.status_code = status_code
        self._body = body
        self._raise = raise_on_json

    def json(self):
        if self._raise:
            # A body that cannot be parsed. Carries the secret so the test can prove
            # the adapter does not surface it when decoding fails.
            raise ValueError(f"unparseable body containing {SECRET_VALUE}")
        return self._body


class _StubClient:
    """Records requests and replays queued responses, sync or async.

    One class for both colours because the adapter uses `httpx.Client` and
    `httpx.AsyncClient` through the same two call sites; a stub that only supported
    one would silently stop covering the other half.
    """

    def __init__(self, responses, recorder):
        self._responses = list(responses)
        self._recorder = recorder

    # sync context manager
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # async context manager
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _next(self, path, json_body, headers):
        self._recorder.append({"path": path, "json": json_body, "headers": headers})
        if not self._responses:
            raise AssertionError(f"no stubbed response for {path}")
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def post(self, path, json=None, headers=None):
        return self._next(path, json, headers)


class _AsyncStubClient(_StubClient):
    async def post(self, path, json=None, headers=None):
        return self._next(path, json, headers)


def _client(responses, *, is_async: bool = False, api_key: str = "internal-key"):
    """Build an adapter wired to a stub transport, plus the recorded requests."""
    recorder: list[dict] = []
    cls = _AsyncStubClient if is_async else _StubClient

    def factory():
        return cls(responses, recorder)

    class DeliveryTransport:
        def post(self, lease, payload):
            return _StubClient(responses, recorder).post(
                DELIVERY_PATH, json=payload, headers={}
            )

        def preflight(self, lease, payload):
            return _StubClient(responses, recorder).post(
                PREFLIGHT_PATH, json=payload, headers={}
            )

    adapter = (
        AdpVaultClient(
            base_url="http://gateway.internal", api_key=api_key, client_factory=factory
        )
        if is_async
        else ExecutorVaultChannel(transport=DeliveryTransport())
    )
    return adapter, recorder


def _reference() -> CredentialReference:
    return CredentialReference(credential_id=CRED, service="openai", label="default")


def _evidence_body(**overrides):
    body = {
        "org_id": ORG,
        "workspace_id": WORKSPACE,
        "credential_id": CRED,
        "service": "openai",
        "label": "default",
        "owner_principal": PRINCIPAL,
        "owner_scope": "user",
        "delegated_to_workspaces": [WORKSPACE],
        "current_version_id": "v1",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        "attested_report_digest": None,
        "report_checked_at": None,
    }
    body.update(overrides)
    return body


def _lease(*, workspace_id: str = WORKSPACE, org_id: str = ORG) -> DeliveryLease:
    """A lease assembled from the contract's own types.

    Assembled rather than faked so the contract's ``__post_init__`` invariants run —
    workspace/principal agreement, provider/service agreement, required expiry. A
    hand-rolled stand-in would let this suite pass against leases the real contract
    would refuse to construct.
    """
    from superplane_contracts.provisioning import ResolvedPrincipal

    principal = ResolvedPrincipal(
        subject=PRINCIPAL, org_id=org_id, workspace_id=workspace_id
    )
    binding = RunBinding(
        operation_id="op-1",
        job_id="job-1",
        attempt_id="att-1",
        provider="openai",
        provider_account_id="acct-1",
        operation="launch",
        principal=principal,
        recipient=ExecutorIdentity(executor_id="exec-1"),
        permission=DELIVERY_PERMISSION,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    return DeliveryLease(
        lease_id="lease-1",
        reference=_reference(),
        workspace_id=workspace_id,
        binding=binding,
        provenance={"trusted_delivery": "live"},
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestSeparatedVaultProtocols:
    """The control-plane key cannot be a dependency of the executor channel."""

    def test_is_a_trusted_delivery_channel(self):
        from superplane_contracts.delivery import TrustedDeliveryChannel

        adapter, _ = _client([])
        assert isinstance(adapter, TrustedDeliveryChannel)

    def test_read_is_a_coroutine_function(self):
        """The reader port declares `async read`; a sync one would never be awaited."""
        import inspect

        adapter, _ = _client([], is_async=True)
        assert inspect.iscoroutinefunction(adapter.read)

    def test_it_exposes_no_way_to_read_a_credential_by_id(self):
        """The absent raw-read surface, asserted rather than assumed.

        The contract notes the vault's management endpoints are unreachable from this
        surface "by construction, not by a check". This pins that: a future
        convenience method taking a bare credential id would be exactly the raw-read
        path the design forbids, and it would not otherwise fail any test.
        """
        adapter, _ = _client([])
        for forbidden in (
            "read_credential",
            "register_credential",
            "delete_credential",
            "get_secret",
        ):
            assert not hasattr(adapter, forbidden), (
                f"{forbidden} reintroduces a raw-read surface"
            )

    def test_construction_without_a_key_fails_fast(self):
        """Unconfigured must not look like "the vault denied us" at runtime."""
        with pytest.raises(ValueError):
            AdpVaultClient(base_url="http://gw", api_key="")
        with pytest.raises(ValueError):
            AdpVaultClient(base_url="", api_key="k")


# ---------------------------------------------------------------------------
# Reader half: refusal colour
# ---------------------------------------------------------------------------


class TestReaderRefusalColour:
    """`None` for denial, raise for unavailability. Backwards is a false outage."""

    def test_a_403_returns_none_and_does_not_raise(self):
        adapter, _ = _client(
            [_Response(403, {"detail": {"error": "denied"}})], is_async=True
        )
        result = asyncio.run(
            adapter.read(
                org_id=ORG,
                workspace_id=WORKSPACE,
                reference=_reference(),
                principal=PRINCIPAL,
                report_digest=None,
            )
        )
        assert result is None

    def test_a_503_raises_so_the_consumer_reports_unavailable(self):
        adapter, _ = _client([_Response(503, {"detail": "unavailable"})], is_async=True)
        with pytest.raises(RuntimeError):
            asyncio.run(
                adapter.read(
                    org_id=ORG,
                    workspace_id=WORKSPACE,
                    reference=_reference(),
                    principal=PRINCIPAL,
                    report_digest=None,
                )
            )

    def test_a_transport_failure_raises_rather_than_denying(self):
        """A network error is not a decision the vault made."""
        adapter, _ = _client([ConnectionError("refused")], is_async=True)
        with pytest.raises(RuntimeError):
            asyncio.run(
                adapter.read(
                    org_id=ORG,
                    workspace_id=WORKSPACE,
                    reference=_reference(),
                    principal=PRINCIPAL,
                    report_digest=None,
                )
            )

    def test_an_unparseable_body_never_surfaces_its_contents(self):
        """The decode error's message holds the secret; the raised error must not."""
        adapter, _ = _client([_Response(200, None, raise_on_json=True)], is_async=True)
        result = asyncio.run(
            adapter.read(
                org_id=ORG,
                workspace_id=WORKSPACE,
                reference=_reference(),
                principal=PRINCIPAL,
                report_digest=None,
            )
        )
        # Unreadable body -> unestablished, and no exception carrying the value.
        assert result is None


class TestReaderRejectsAMismatchedAnswer:
    """The adapter re-checks identity rather than trusting the envelope."""

    @pytest.mark.parametrize(
        "override",
        [
            {"org_id": "org-other"},
            {"workspace_id": "ws-other"},
            {"credential_id": "cred-other"},
            {"owner_principal": ""},
            {"owner_principal": None},
        ],
        ids=[
            "foreign-tenant",
            "foreign-workspace",
            "different-credential",
            "blank-owner",
            "null-owner",
        ],
    )
    def test_a_response_describing_something_else_is_refused(self, override):
        adapter, _ = _client(
            [_Response(200, _evidence_body(**override))], is_async=True
        )
        result = asyncio.run(
            adapter.read(
                org_id=ORG,
                workspace_id=WORKSPACE,
                reference=_reference(),
                principal=PRINCIPAL,
                report_digest=None,
            )
        )
        assert result is None

    def test_unresolved_ownership_is_refused_at_both_layers(self):
        """The owner check is doubled, and the second layer is the invisible one.

        `VaultOwnership.__post_init__` also refuses a blank `owner_principal`, and the
        client catches that and returns `None` — so deleting the client's own check
        leaves the parametrized cases above green. That makes the arrangement worth
        naming rather than leaving implicit: an operator reading only those cases would
        believe the client is what refuses unowned credentials.

        Both layers are asserted here. If the contract ever stopped refusing a blank
        owner, the second assertion fails and the client's guard becomes the single
        thing standing between "the vault could not say who owns this" and an
        authorization decision.
        """
        from superplane_contracts.health import ContractViolation

        adapter, _ = _client(
            [_Response(200, _evidence_body(owner_principal="   "))], is_async=True
        )
        assert (
            asyncio.run(
                adapter.read(
                    org_id=ORG,
                    workspace_id=WORKSPACE,
                    reference=_reference(),
                    principal=PRINCIPAL,
                    report_digest=None,
                )
            )
            is None
        )

        with pytest.raises(ContractViolation):
            VaultOwnership(credential_id=CRED, owner_principal="")

    def test_a_claimed_attestation_must_come_back_identical(self):
        """Asked with a digest, answered with a different one → refused."""
        body = _evidence_body(
            attested_report_digest="b" * 64,
            report_checked_at=datetime.now(timezone.utc).isoformat(),
        )
        adapter, _ = _client([_Response(200, body)], is_async=True)
        result = asyncio.run(
            adapter.read(
                org_id=ORG,
                workspace_id=WORKSPACE,
                reference=_reference(),
                principal=PRINCIPAL,
                report_digest=DIGEST,
            )
        )
        assert result is None

    def test_an_attestation_without_a_readable_observation_time_is_refused(self):
        """The consumer refuses a missing `report_checked_at`; don't hand it one."""
        body = _evidence_body(attested_report_digest=DIGEST, report_checked_at=None)
        adapter, _ = _client([_Response(200, body)], is_async=True)
        result = asyncio.run(
            adapter.read(
                org_id=ORG,
                workspace_id=WORKSPACE,
                reference=_reference(),
                principal=PRINCIPAL,
                report_digest=DIGEST,
            )
        )
        assert result is None

    def test_an_unclaimed_attestation_is_not_invented(self):
        """No digest asked for → none asserted, even if the vault volunteered one.

        Otherwise the adapter would hand the consumer an attestation the caller never
        requested and the consumer never verified against a report it holds.
        """
        body = _evidence_body(
            attested_report_digest=DIGEST,
            report_checked_at=datetime.now(timezone.utc).isoformat(),
        )
        adapter, _ = _client([_Response(200, body)], is_async=True)
        result = asyncio.run(
            adapter.read(
                org_id=ORG,
                workspace_id=WORKSPACE,
                reference=_reference(),
                principal=PRINCIPAL,
                report_digest=None,
            )
        )
        assert result is not None
        assert result.attested_report_digest is None
        assert result.report_checked_at is None


class TestReaderHappyPath:
    def test_maps_a_vault_answer_onto_the_contract_type(self):
        adapter, recorder = _client([_Response(200, _evidence_body())], is_async=True)
        result = asyncio.run(
            adapter.read(
                org_id=ORG,
                workspace_id=WORKSPACE,
                reference=_reference(),
                principal=PRINCIPAL,
                report_digest=None,
            )
        )
        assert isinstance(result, VerifiedCredentialEvidence)
        assert result.org_id == ORG
        assert result.reference == _reference()
        assert isinstance(result.ownership, VaultOwnership)
        assert result.ownership.owner_principal == PRINCIPAL
        assert result.ownership.delegated_to_workspaces == frozenset({WORKSPACE})
        # The consumer requires an aware datetime.
        assert result.expires_at.tzinfo is not None
        assert result.expires_at.utcoffset() is not None
        assert recorder[0]["path"] == EVIDENCE_PATH

    def test_the_api_key_travels_in_a_header_and_not_the_path(self):
        """A URL reaches access logs even where headers do not."""
        adapter, recorder = _client(
            [_Response(200, _evidence_body())],
            is_async=True,
            api_key="super-secret-key",
        )
        asyncio.run(
            adapter.read(
                org_id=ORG,
                workspace_id=WORKSPACE,
                reference=_reference(),
                principal=PRINCIPAL,
                report_digest=None,
            )
        )
        assert recorder[0]["headers"]["X-Internal-Api-Key"] == "super-secret-key"
        assert "super-secret-key" not in recorder[0]["path"]

    def test_a_confirmed_attestation_is_carried_through(self):
        checked = datetime.now(timezone.utc) - timedelta(minutes=5)
        body = _evidence_body(
            attested_report_digest=DIGEST, report_checked_at=checked.isoformat()
        )
        adapter, _ = _client([_Response(200, body)], is_async=True)
        result = asyncio.run(
            adapter.read(
                org_id=ORG,
                workspace_id=WORKSPACE,
                reference=_reference(),
                principal=PRINCIPAL,
                report_digest=DIGEST,
            )
        )
        assert result.attested_report_digest == DIGEST
        assert result.report_checked_at is not None
        assert result.report_checked_at.tzinfo is not None


# ---------------------------------------------------------------------------
# The expires_at mismatch
# ---------------------------------------------------------------------------


class TestNonExpiringCredentialsAreNotSilentlyDenied:
    """The bug this adapter exists to prevent. See the module docstring.

    The contract's ``expires_at`` is non-optional and ``_current()`` 403s when it has
    passed; the vault sends ``null`` for a credential with no expiry. Every test here
    would pass against a mapping that denied all of them, EXCEPT the first — which is
    why the first asserts on the consumer's actual predicate rather than on the field.
    """

    def _read_with_expiry(self, raw):
        adapter, _ = _client(
            [_Response(200, _evidence_body(expires_at=raw))], is_async=True
        )
        return asyncio.run(
            adapter.read(
                org_id=ORG,
                workspace_id=WORKSPACE,
                reference=_reference(),
                principal=PRINCIPAL,
                report_digest=None,
            )
        )

    def test_a_null_expiry_survives_the_consumers_currency_check(self):
        """Drives the consumer's own `_current()`, not a restatement of it.

        Asserting ``expires_at == NON_EXPIRING_HORIZON`` would only prove the constant
        is what I wrote. This proves the value actually passes the predicate that
        would otherwise 403 — which is the failure being prevented.
        """
        from app.routers.provider_connections import _current

        result = self._read_with_expiry(None)
        assert result is not None
        _current(result)  # must not raise

    def test_the_substituted_horizon_is_aware_utc(self):
        """The consumer refuses a naive or offset-less expiry."""
        result = self._read_with_expiry(None)
        assert result.expires_at.tzinfo is not None
        assert result.expires_at.utcoffset() is not None

    def test_the_horizon_tolerates_arithmetic(self):
        """Why the horizon is not `datetime.max`, nor pressed up against it.

        A consumer computing a remaining lifetime is a plausible future change, and
        `datetime.max + timedelta` raises OverflowError. A horizon in year 9999 has
        the same defect for any delta past a day, so the requirement is real
        headroom, not merely "not literally datetime.max". This first caught a
        9999-01-01 horizon that failed on `+ 365 days`.
        """
        result = self._read_with_expiry(None)
        for delta in (
            timedelta(days=1),
            timedelta(days=365),
            timedelta(days=365 * 100),
        ):
            assert result.expires_at + delta > result.expires_at
        assert (result.expires_at - datetime.now(timezone.utc)).days > 365 * 100

    def test_a_real_expiry_is_preserved_and_not_replaced(self):
        """The substitution must apply ONLY to null, or it erases real expiries."""
        real = datetime.now(timezone.utc) + timedelta(days=7)
        result = self._read_with_expiry(real.isoformat())
        assert result.expires_at != NON_EXPIRING_HORIZON
        assert abs((result.expires_at - real).total_seconds()) < 1

    def test_an_expired_credential_is_still_reported_as_expired(self):
        """The mapping must not rescue a genuinely expired credential."""
        from fastapi import HTTPException

        from app.routers.provider_connections import _current

        past = datetime.now(timezone.utc) - timedelta(days=1)
        result = self._read_with_expiry(past.isoformat())
        assert result is not None
        with pytest.raises(HTTPException):
            _current(result)

    def test_a_naive_expiry_is_read_as_utc_rather_than_refused(self):
        """SQLite-backed rows can round-trip without an offset; Postgres keeps it."""
        naive = (datetime.now(timezone.utc) + timedelta(days=3)).replace(tzinfo=None)
        result = self._read_with_expiry(naive.isoformat())
        assert result is not None
        assert result.expires_at.tzinfo is not None

    @pytest.mark.parametrize(
        "raw",
        ["not-a-date", "", 12345, {}],
        ids=["garbage", "blank", "number", "object"],
    )
    def test_an_unusable_expiry_is_a_refusal_not_a_guess(self, raw):
        """Present but unparseable → None. Distinct from absent, which is "no expiry"."""
        assert self._read_with_expiry(raw) is None


# ---------------------------------------------------------------------------
# Delivery half
# ---------------------------------------------------------------------------


class TestRevocationStateFailsClosed:
    def test_reports_admitted_work_when_the_vault_says_so(self):
        adapter, recorder = _client(
            [_Response(200, {"admits_work": True, "limitation": ""})]
        )
        state = adapter.revocation_state(_lease())
        assert state.admits_work is True
        assert recorder[0]["path"] == PREFLIGHT_PATH

    def test_a_negative_answer_carries_the_limitation(self):
        adapter, _ = _client(
            [
                _Response(
                    200, {"admits_work": False, "limitation": REVOCATION_LIMITATION}
                )
            ]
        )
        state = adapter.revocation_state(_lease())
        assert state.admits_work is False
        assert "revoked at the provider" in state.limitation

    @pytest.mark.parametrize(
        "response",
        [
            ConnectionError("unreachable"),
            _Response(500, {}),
            _Response(200, {}),
            _Response(200, {"admits_work": "yes"}),
            _Response(200, None, raise_on_json=True),
        ],
        ids=[
            "transport-error",
            "server-error",
            "empty-body",
            "non-boolean",
            "unparseable",
        ],
    )
    def test_anything_it_cannot_establish_means_revoked(self, response):
        """Fail-closed: "I could not check" must never read as "still valid"."""
        adapter, _ = _client([response])
        state = adapter.revocation_state(_lease())
        assert state.admits_work is False
        assert state.limitation, (
            "a refusal with no limitation would violate the contract"
        )

    def test_a_negative_answer_missing_its_limitation_still_gets_one(self):
        """`RevocationState` raises without one; that must not become the failure mode."""
        adapter, _ = _client([_Response(200, {"admits_work": False, "limitation": ""})])
        state = adapter.revocation_state(_lease())
        assert state.admits_work is False
        assert state.limitation == REVOCATION_LIMITATION


class TestFetchMaterial:
    def test_returns_redacting_material_on_success(self):
        adapter, recorder = _client(
            [_Response(200, {"value": SECRET_VALUE, "credential_type": "api_key"})]
        )
        material = adapter.fetch_material(_lease())
        assert isinstance(material, SecretMaterial)
        assert material.reveal() == SECRET_VALUE
        assert recorder[0]["path"] == DELIVERY_PATH

    def test_the_material_refuses_to_render_itself(self):
        """Every rendering path, not just repr — the protection must not end at `%`."""
        adapter, _ = _client([_Response(200, {"value": SECRET_VALUE})])
        material = adapter.fetch_material(_lease())
        assert SECRET_VALUE not in repr(material)
        assert SECRET_VALUE not in str(material)
        assert SECRET_VALUE not in f"{material}"
        assert SECRET_VALUE not in f"{material:>40}"
        assert SECRET_VALUE not in "{}".format(material)  # noqa: UP032
        assert SECRET_VALUE not in "%s" % (material,)  # noqa: UP031
        assert SECRET_VALUE not in json.dumps({"m": repr(material)})

    def test_the_material_cannot_be_pickled_into_a_queue_or_cache(self):
        """Pickling is how a secret reaches a queue message, a cache or a tool result.

        `ContractViolation` specifically, not any exception: a `TypeError` from some
        unrelated future change to `__slots__` would also stop the pickle, and
        accepting it would let this test pass without the deliberate refusal being
        present. The message must not carry the value either — it is raised from a
        method that holds it.
        """
        from superplane_contracts.health import ContractViolation

        adapter, _ = _client([_Response(200, {"value": SECRET_VALUE})])
        material = adapter.fetch_material(_lease())
        with pytest.raises(ContractViolation) as caught:
            pickle.dumps(material)
        assert SECRET_VALUE not in str(caught.value)

    @pytest.mark.parametrize(
        "response",
        [
            _Response(403, {"detail": {"error": "denied"}}),
            _Response(503, {"detail": "unavailable"}),
            ConnectionError("unreachable"),
            _Response(200, {}),
            _Response(200, {"value": ""}),
            _Response(200, {"value": 12345}),
            _Response(200, None, raise_on_json=True),
        ],
        ids=[
            "denied",
            "unavailable",
            "transport",
            "no-value",
            "blank",
            "non-string",
            "unparseable",
        ],
    )
    def test_every_failure_is_a_uniform_delivery_refusal(self, response):
        """One exception type, one FIXED message, no provider detail.

        The contract's registry declares ``refusal_exceptions=("DeliveryRefused",)``,
        so an AttributeError or a bare PermissionError would break the consumer's
        handling.

        The message is asserted to be *exactly* the constant, not merely "free of the
        test's secret". An earlier version only checked the latter and a mutant that
        interpolated the whole response body into the refusal survived it — the
        fixtures happened not to contain anything sensitive. Equality is the only form
        of this assertion that holds for bodies the test did not think to write.
        """
        adapter, _ = _client([response])
        with pytest.raises(DeliveryRefused) as caught:
            adapter.fetch_material(_lease())
        assert str(caught.value) == "credential delivery refused"
        # Nothing from the cause chain either: `from None` must have severed it.
        assert caught.value.__cause__ is None

    def test_a_refusal_body_carrying_provider_material_is_not_echoed(self):
        """The case the equality assertion above exists for, made explicit.

        A provider error body can quote the key that failed. If the refusal
        interpolated the body, that material would reach every log and transcript
        that records the exception.
        """
        body = {
            "detail": {
                "provider_error": f"invalid api key {SECRET_VALUE}",
                "token": SECRET_VALUE,
            }
        }
        adapter, _ = _client([_Response(400, body)])
        with pytest.raises(DeliveryRefused) as caught:
            adapter.fetch_material(_lease())
        rendered = f"{caught.value!r} {caught.value!s} {caught.value.args}"
        assert SECRET_VALUE not in rendered
        assert "provider_error" not in rendered

    def test_an_unusable_value_is_refused_at_both_layers(self):
        """Two independent refusals, named — because one of them is invisible.

        The client checks the value before constructing `SecretMaterial`, and
        `SecretMaterial.__init__` refuses a blank or non-string value as well. Removing
        the client's check leaves the suite green, since the contract's refusal is
        caught and remapped to the same `DeliveryRefused`.

        That is a genuine belt-and-braces arrangement rather than dead code, but the
        earlier tests read as though they were exercising the client's guard when the
        contract was doing the work. This pins both layers explicitly so a later
        relaxation of either one is visible: if the contract stopped refusing blanks,
        the second assertion fails and the client's guard is shown to be load-bearing.
        """
        from superplane_contracts.health import ContractViolation

        adapter, _ = _client([_Response(200, {"value": ""})])
        with pytest.raises(DeliveryRefused):
            adapter.fetch_material(_lease())

        with pytest.raises(ContractViolation):
            SecretMaterial("")

    def test_a_refusal_never_leaks_the_value_through_the_log(self, caplog):
        adapter, _ = _client([_Response(403, {"detail": {"error": "denied"}})])
        with caplog.at_level(logging.DEBUG), pytest.raises(DeliveryRefused):
            adapter.fetch_material(_lease())
        assert SECRET_VALUE not in caplog.text


class TestRecordDelivered:
    def test_audits_identifiers_and_never_the_value(self, caplog):
        adapter, _ = _client([_Response(200, {"value": SECRET_VALUE})])
        lease = _lease()
        with caplog.at_level(logging.INFO):
            adapter.record_delivered(lease)
        assert lease.lease_id in caplog.text
        assert lease.recipient.executor_id in caplog.text
        assert SECRET_VALUE not in caplog.text

    def test_it_makes_no_second_gateway_call(self):
        """A separate audit request could fail AFTER a successful delivery.

        The empty response queue is the assertion: the stub raises if any request is
        made, so this proves the domain-side record cannot invert the outcome.
        """
        adapter, recorder = _client([])
        adapter.record_delivered(_lease())
        assert recorder == []


class TestSyncMethodsRefuseTheEventLoop:
    """Blocking HTTP on the loop thread stalls the process and looks like latency."""

    @pytest.mark.parametrize(
        "method", ["revocation_state", "fetch_material", "record_delivered"]
    )
    def test_calling_from_a_running_loop_raises_runtime_error(self, method):
        adapter, _ = _client(
            [_Response(200, {"admits_work": True, "value": SECRET_VALUE})]
        )

        async def _call():
            # RuntimeError, deliberately NOT DeliveryRefused: this is a composition
            # bug in the caller, and dressing it as a refusal would hide it behind a
            # plausible denial.
            with pytest.raises(RuntimeError) as caught:
                getattr(adapter, method)(_lease())
            assert not isinstance(caught.value, DeliveryRefused)

        asyncio.run(_call())


class TestTenantScoping:
    def test_the_payload_carries_the_resolved_principals_org_not_a_caller_value(self):
        """The Gateway compares this against the verified run credential's tenant."""
        adapter, recorder = _client([_Response(200, {"admits_work": True})])
        adapter.revocation_state(_lease(org_id=ORG))
        assert recorder[0]["json"]["org_id"] == ORG
        assert recorder[0]["json"]["workspace_id"] == WORKSPACE
        assert recorder[0]["json"]["credential_id"] == CRED

    def test_the_delivery_payload_names_the_lease_recipient(self):
        adapter, recorder = _client([_Response(200, {"value": SECRET_VALUE})])
        adapter.fetch_material(_lease())
        assert recorder[0]["json"]["recipient"] == "exec-1"
        assert recorder[0]["json"]["provider"] == "openai"
        assert recorder[0]["json"]["provider_account_id"] == "acct-1"

    def test_no_payload_ever_contains_a_secret_or_an_arn(self):
        adapter, recorder = _client([_Response(200, {"value": SECRET_VALUE})])
        adapter.fetch_material(_lease())
        serialised = json.dumps(recorder[0]["json"])
        assert SECRET_VALUE not in serialised
        assert "arn:aws" not in serialised

    def test_binding_payload_attempt_id_is_distinct_from_operation_id(self):
        """attempt_id and job_id must not repeat operation_id in the delivery payload.

        An audit row with attempt_id == operation_id is unreadable and indicates a
        binding collision that makes two distinct executions look identical. The
        Gateway's OperationBinding requires distinct values for operation_id,
        attempt_id and job_id.
        """
        adapter, recorder = _client([_Response(200, {"value": SECRET_VALUE})])
        adapter.fetch_material(_lease())
        payload = recorder[0]["json"]
        op_id = payload["operation_id"]
        assert payload["attempt_id"] != op_id, (
            f"attempt_id ({payload['attempt_id']!r}) must not equal operation_id ({op_id!r})"
        )
        assert payload["job_id"] != op_id or payload["job_id"] == "exec-1", (
            "job_id should carry the executor identity, not repeat the operation_id"
        )

    def test_binding_payload_uses_durable_ids_independent_of_executor_id(self):
        """Opaque recipient identity must never determine operation/job/attempt IDs."""
        from superplane_contracts.delivery import ExecutorIdentity, RunBinding
        from superplane_contracts.provisioning import ResolvedPrincipal

        principal = ResolvedPrincipal(
            subject=PRINCIPAL, org_id=ORG, workspace_id=WORKSPACE
        )
        binding = RunBinding(
            operation_id="op-deploy-42",
            job_id="durable-job",
            attempt_id="execution-attempt",
            provider="openai",
            provider_account_id="acct-1",
            operation="launch",
            principal=principal,
            recipient=ExecutorIdentity(executor_id="developer-invocation#7"),
            permission=DELIVERY_PERMISSION,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
        from superplane_contracts.connections import CredentialReference

        lease = DeliveryLease(
            lease_id="lease-x",
            reference=CredentialReference(
                credential_id=CRED, service="openai", label="default"
            ),
            workspace_id=WORKSPACE,
            binding=binding,
            provenance={"trusted_delivery": "live"},
        )
        adapter, recorder = _client([_Response(200, {"value": SECRET_VALUE})])
        adapter.fetch_material(lease)
        payload = recorder[0]["json"]
        assert payload["operation_id"] == "op-deploy-42"
        assert payload["attempt_id"] == "execution-attempt"
        assert payload["job_id"] == "durable-job"


def test_singleton_client_has_no_delivery_identity_even_with_legacy_token_settings():
    class Settings:
        adp_gateway_internal_url = "http://gateway.internal"
        adp_gateway_internal_api_key = "key"
        adp_gateway_run_credential = "legacy-ignored"
        adp_gateway_workload_token = "legacy-ignored"

    client = build_vault_client(Settings())
    from superplane_contracts.delivery import TrustedDeliveryChannel

    assert not isinstance(client, TrustedDeliveryChannel)
    assert not hasattr(client, "fetch_material")
    assert not hasattr(client, "revocation_state")


class TestRedactionDoesNotDependOnSecretShape:
    """Why the open #5319/#5322 denylist gaps do not reach this story's guarantee.

    Reconciling those two issues against current code found real, reproducible gaps:
    `emission.scrub` leaves a credential untouched inside a `bytearray` or an
    exception object when the value matches no known provider pattern, and
    `delivery_executor._contains_exact_material` certifies such leaves as clean.

    Neither gap is in this adapter's path, and these tests are here to keep that
    true rather than to assert it once in a comment. This client's protection is
    **structural**, not pattern-based:

    * values are carried in `SecretMaterial`, which refuses every rendering path
      regardless of whether the value looks like anything;
    * refusals are fixed constants, so no response body is ever interpolated;
    * no log call in this module passes a body, a value or an exception object.

    A future edit that started relying on shape recognition — logging a response and
    trusting a filter to scrub it — would inherit those open gaps. These tests fail
    if that happens. The gaps themselves are reported to #5319/#5322 rather than
    fixed here: they are other stories' surfaces, and widening a shared denylist is
    not something to do inside an unrelated change.
    """

    UNSHAPED = "provider-cred-abc123-matches-no-known-pattern"

    def test_material_is_redacted_even_with_no_recognisable_shape(self):
        """The shape-independence property, stated as a test.

        `scrub` genuinely does not protect this value — asserted below so the test
        fails loudly if that ever changes and this rationale goes stale — yet the
        material still refuses to render, because the container does the work.
        """
        from superplane_contracts.emission import scrub

        assert self.UNSHAPED in str(
            scrub({"stderr": bytearray(self.UNSHAPED.encode())})
        ), (
            "scrub now protects unshaped values; re-check whether this adapter's "
            "structural redaction is still the only thing carrying the guarantee"
        )

        adapter, _ = _client([_Response(200, {"value": self.UNSHAPED})])
        material = adapter.fetch_material(_lease())
        for rendered in (
            repr(material),
            str(material),
            f"{material}",
            f"{material:>60}",
            "%s" % (material,),
        ):
            assert self.UNSHAPED not in rendered

    def test_a_refusal_carrying_an_unshaped_credential_body_is_still_silent(self):
        """A provider error body holding an unshaped key must not reach the message."""
        body = {"detail": {"provider_error": f"rejected key {self.UNSHAPED}"}}
        adapter, _ = _client([_Response(400, body)])
        with pytest.raises(DeliveryRefused) as caught:
            adapter.fetch_material(_lease())
        assert str(caught.value) == "credential delivery refused"
        assert self.UNSHAPED not in f"{caught.value!r} {caught.value.args}"

    def test_no_log_call_in_this_module_passes_a_body_value_or_exception(self):
        """Pins the discipline to the source, since a filter would not catch it.

        A value with no recognisable shape is invisible to the log filter, so
        "nothing renders a body" has to hold by construction. Source inspection is
        crude, but it is the only check that covers log lines no test happens to
        trigger.
        """
        import inspect
        import re

        from app.adapters import adp_vault_client

        source = inspect.getsource(adp_vault_client)
        offenders = [
            call.group(1).strip()[:100]
            for call in re.finditer(r"logger\.\w+\((.*?)\)\n", source, re.S)
            if re.search(r"\b(body|value|response|exc|material)\b", call.group(1))
        ]
        assert offenders == [], f"a log call may render secret material: {offenders}"


# ---------------------------------------------------------------------------
# AC-02: unrelated vault clients keep their behaviour
# ---------------------------------------------------------------------------


class TestUnrelatedVaultClientsAreUnchanged:
    """AC-02: this story must not alter clients it does not own."""

    def test_no_reader_is_installed_by_default(self):
        """The port ships with no adapter; installing one is a startup decision.

        If this story had wired itself in at import time, every deployment would
        silently acquire a vault dependency it never configured.
        """
        from app.services.credential_evidence import get_credential_evidence_reader

        assert get_credential_evidence_reader() is None

    def test_installing_a_reader_twice_is_still_refused(self):
        """The single-installation guard is a trust control, not a convenience."""
        import app.services.credential_evidence as module

        original = module._reader
        try:
            module._reader = None
            module.install_credential_evidence_reader(_client([])[0])
            with pytest.raises(RuntimeError):
                module.install_credential_evidence_reader(_client([])[0])
        finally:
            module._reader = original

    def test_the_mcp_vault_client_still_declares_its_mocked_binding(self):
        """The existing MCP client is untouched by this story.

        It documents its own limitation with `EXACT_BINDING_IS_MOCKED`. Asserting the
        flag here means a future change that quietly flipped it — implying a live
        binding this story did not build — fails a test.
        """
        import pathlib

        path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "tools"
            / "superplane-mcp"
            / "superplane_mcp"
            / "vault_client.py"
        )
        if not path.exists():
            pytest.skip("superplane-mcp vault client not present in this checkout")
        source = path.read_text(encoding="utf-8")
        assert "EXACT_BINDING_IS_MOCKED = True" in source

    def test_build_vault_client_returns_none_when_unconfigured(self):
        """A deployment with no vault settings gets no client — not a stub.

        A permissive stub would be a bypass; a stub returning None from every read
        would report a configuration gap as a per-credential denial.
        """

        class _Settings:
            adp_gateway_internal_url = ""
            adp_gateway_internal_api_key = ""

        assert build_vault_client(_Settings()) is None

    def test_build_vault_client_constructs_when_configured(self):
        class _Settings:
            adp_gateway_internal_url = "http://gateway.internal"
            adp_gateway_internal_api_key = "key"

        assert isinstance(build_vault_client(_Settings()), AdpVaultClient)

    def test_a_partially_configured_deployment_gets_no_client(self):
        """A URL with no key, or a key with no URL, is a misconfiguration.

        Not a client that 403s on every call: `AdpVaultClient.__init__` would raise on
        the empty key and take the whole process down at startup, and a URL-less client
        would post to a relative path against whatever base httpx defaulted to.
        """

        class _UrlOnly:
            adp_gateway_internal_url = "http://gateway.internal"
            adp_gateway_internal_api_key = "   "

        class _KeyOnly:
            adp_gateway_internal_url = ""
            adp_gateway_internal_api_key = "key"

        assert build_vault_client(_UrlOnly()) is None
        assert build_vault_client(_KeyOnly()) is None


class TestStartupComposition:
    """`compose_vault_client` — the wiring, and the two ordering rules it depends on."""

    def test_it_installs_the_client_when_configured(self, monkeypatch):
        import app.services.credential_evidence as evidence
        from app import main

        monkeypatch.setattr(evidence, "_reader", None)
        monkeypatch.setattr(
            main.settings,
            "adp_gateway_internal_url",
            "http://gateway.internal",
            raising=False,
        )
        monkeypatch.setattr(
            main.settings, "adp_gateway_internal_api_key", "key", raising=False
        )

        main.compose_vault_client()
        assert isinstance(evidence.get_credential_evidence_reader(), AdpVaultClient)

    def test_an_unconfigured_deployment_installs_nothing(self, monkeypatch, caplog):
        """No reader → the routes answer 503, which is the truthful answer.

        The log assertion is not decoration. Dropping the `client is None` guard leaves
        the installed reader as `None` either way — so the state check alone cannot
        tell the two versions apart — but it emits "Installed the ADP vault
        credential-evidence reader" on a deployment with no vault configured. An
        operator reading that line would stop looking for the missing setting that is
        actually causing every request to 503.
        """
        import app.services.credential_evidence as evidence
        from app import main

        monkeypatch.setattr(evidence, "_reader", None)
        monkeypatch.setattr(
            main.settings, "adp_gateway_internal_url", "", raising=False
        )
        monkeypatch.setattr(
            main.settings, "adp_gateway_internal_api_key", "", raising=False
        )

        with caplog.at_level(logging.INFO):
            main.compose_vault_client()
        assert evidence.get_credential_evidence_reader() is None
        assert "Installed the ADP vault" not in caplog.text

    def test_it_does_not_displace_a_reader_someone_else_installed(self, monkeypatch):
        """A substituted reader wins, and composition must not raise trying to replace it.

        `install_credential_evidence_reader` refuses a second install, so a
        `compose_vault_client` that called it unconditionally would crash the lifespan
        of any host that had already composed one — and worse, a version that forced
        the install would silently displace a deliberately substituted trust source.
        """
        import app.services.credential_evidence as evidence
        from app import main

        sentinel = object()
        monkeypatch.setattr(evidence, "_reader", sentinel)
        monkeypatch.setattr(
            main.settings,
            "adp_gateway_internal_url",
            "http://gateway.internal",
            raising=False,
        )
        monkeypatch.setattr(
            main.settings, "adp_gateway_internal_api_key", "key", raising=False
        )

        main.compose_vault_client()  # must not raise
        assert evidence.get_credential_evidence_reader() is sentinel

    def test_composition_is_idempotent(self, monkeypatch):
        """Two lifespan startups in one process (reload, embedded host) must not crash."""
        import app.services.credential_evidence as evidence
        from app import main

        monkeypatch.setattr(evidence, "_reader", None)
        monkeypatch.setattr(
            main.settings,
            "adp_gateway_internal_url",
            "http://gateway.internal",
            raising=False,
        )
        monkeypatch.setattr(
            main.settings, "adp_gateway_internal_api_key", "key", raising=False
        )

        main.compose_vault_client()
        first = evidence.get_credential_evidence_reader()
        main.compose_vault_client()
        assert evidence.get_credential_evidence_reader() is first

    def test_nothing_is_installed_merely_by_importing_the_app(self):
        """The import-time rule.

        A module-level install would give every test process a vault dependency and
        make `install_credential_evidence_reader`'s single-install guard unusable for
        substitution. `app.main` is already imported by conftest at this point, so
        this observes the real state rather than a simulated one.
        """
        import sys

        from app.services.credential_evidence import get_credential_evidence_reader

        assert "app.main" in sys.modules
        assert get_credential_evidence_reader() is None

    def test_composition_runs_before_the_installation_gate(self):
        """The ordering rule, asserted against the lifespan's own source.

        The gate refuses to start an image whose trust adapters are not composed, by
        probing what is installed. Composing after it would make the gate permanently
        unsatisfiable — a real adapter could never pass it. Source order is a crude
        assertion, but the alternative is running the gate, which requires the full
        set of production adapters this story does not provide.
        """
        import inspect

        from app import main

        source = inspect.getsource(main.lifespan)
        composed_at = source.find("compose_vault_client()")
        gated_at = source.find("SUPERPLANE_INSTALLATION_REQUIRED")
        assert composed_at != -1, "the lifespan no longer composes the vault client"
        assert gated_at != -1, "the installation gate moved; re-check the ordering"
        assert composed_at < gated_at, "composition must precede the installation gate"


def test_delivery_refuses_missing_durable_identity_before_transport():
    from dataclasses import replace

    lease = _lease()
    lease = replace(lease, binding=replace(lease.binding, job_id=None, attempt_id=None))
    adapter, recorder = _client([])
    with pytest.raises(DeliveryRefused):
        adapter.fetch_material(lease)
    assert recorder == []
