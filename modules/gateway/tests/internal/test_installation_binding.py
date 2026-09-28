"""Unit tests for resolve_installation_binding (issue #4272).

This is the fail-closed sibling of resolve_credential_binding. The distinction
matters enough to test directly rather than only through the route:

  * resolve_credential_binding resolves a USER and is fail-SOFT by design — a
    DDB failure must not break credential reads.
  * resolve_installation_binding resolves an INSTALLATION + TENANT and must be
    fail-CLOSED — failing soft would hand out an unverifiable org-scoped GitHub
    token, which is the exact confused-deputy bug #4272 exists to close.

Every case below is a reject. The one accept case proves the tenant comes from
the registry row rather than from anything the caller supplied.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.internal.credential_binding import resolve_installation_binding

_INVOCATION_ID = "evt-xyz-789"
_BOUND = 424242
_TENANT = "org-acme"


def _settings(*, enforce_credential_binding: bool = True) -> MagicMock:
    s = MagicMock()
    s.aws_region = "us-east-1"
    s.webhook_events_table = "adp-test-webhook-events"
    s.enforce_credential_binding = enforce_credential_binding
    return s


def _table(items: list[dict] | None = None, *, error: Exception | None = None) -> MagicMock:
    table = MagicMock()
    if error is not None:
        table.query.side_effect = error
    else:
        table.query.return_value = {"Items": items or []}
    return table


def _resolve(
    *,
    items: list[dict] | None = None,
    error: Exception | None = None,
    invocation_id: str | None = _INVOCATION_ID,
    requested: int = _BOUND,
    settings: MagicMock | None = None,
):
    with patch(
        "src.internal.credential_binding._get_dynamodb_table",
        return_value=_table(items, error=error),
    ):
        return resolve_installation_binding(
            invocation_id=invocation_id,
            requested_installation_id=requested,
            settings=settings or _settings(),
        )


def _row(**over) -> dict:
    row = {
        "event_id": _INVOCATION_ID,
        "arrived_at": "2026-08-27T15:00:00Z",
        "installation_id": _BOUND,
        "tenant_id": _TENANT,
    }
    row.update(over)
    return row


class TestAccept:
    def test_matching_row_returns_tenant_from_registry(self):
        """The tenant is derived server-side; the caller never asserts it."""
        binding = _resolve(items=[_row()])

        assert binding.tenant_id == _TENANT
        assert binding.installation_id == _BOUND

    def test_string_installation_id_in_row_is_accepted(self):
        """DDB round-trips numbers as Decimal and some writers store strings."""
        binding = _resolve(items=[_row(installation_id=str(_BOUND))])

        assert binding.installation_id == _BOUND

    def test_query_is_newest_first_limit_one(self):
        """Composite key (event_id HASH + arrived_at RANGE): Query, not GetItem.

        A GetItem would need both keys and arrived_at is not known to the caller —
        the bug #3376 fixed on the user-binding path.
        """
        table = _table([_row()])
        with patch("src.internal.credential_binding._get_dynamodb_table", return_value=table):
            resolve_installation_binding(
                invocation_id=_INVOCATION_ID,
                requested_installation_id=_BOUND,
                settings=_settings(),
            )

        kwargs = table.query.call_args.kwargs
        assert kwargs["ScanIndexForward"] is False, "must read the newest row for this event"
        assert kwargs["Limit"] == 1


class TestFailClosed:
    def test_missing_invocation_id_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            _resolve(invocation_id=None, items=[_row()])

        assert exc.value.status_code == 403

    def test_empty_invocation_id_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            _resolve(invocation_id="", items=[_row()])

        assert exc.value.status_code == 403

    def test_no_row_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            _resolve(items=[])

        assert exc.value.status_code == 403

    def test_row_without_installation_id_raises_403(self):
        """Reachable state: write_event stores installation_id conditionally."""
        row = _row()
        del row["installation_id"]

        with pytest.raises(HTTPException) as exc:
            _resolve(items=[row])

        assert exc.value.status_code == 403

    def test_row_without_tenant_id_raises_403(self):
        row = _row()
        del row["tenant_id"]

        with pytest.raises(HTTPException) as exc:
            _resolve(items=[row])

        assert exc.value.status_code == 403

    def test_unparseable_installation_id_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            _resolve(items=[_row(installation_id="not-a-number")])

        assert exc.value.status_code == 403

    def test_mismatch_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            _resolve(items=[_row(installation_id=_BOUND)], requested=_BOUND + 1)

        assert exc.value.status_code == 403

    def test_ddb_client_error_raises_403_not_a_pass(self):
        """Fail-closed on lookup failure — the inverse of the user-binding path.

        resolve_credential_binding returns None here on purpose (a DDB blip must
        not break credential reads). Doing that here would mint an org-scoped
        GitHub token for an unverified installation during any DDB degradation.
        """
        error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
            "Query",
        )

        with pytest.raises(HTTPException) as exc:
            _resolve(error=error)

        assert exc.value.status_code == 403


class TestIndependentOfEnforceFlag:
    """The guard must not be gated on ENFORCE_CREDENTIAL_BINDING.

    That flag is false on at least one live environment. If this function read it,
    every rejection below would become a silent pass in production while the unit
    suite (which defaults it true) stayed green — a shadowed control that looks
    enforced.
    """

    @pytest.mark.parametrize("enforce", [True, False])
    def test_mismatch_rejected_under_both_flag_values(self, enforce):
        with pytest.raises(HTTPException) as exc:
            _resolve(
                items=[_row()],
                requested=_BOUND + 1,
                settings=_settings(enforce_credential_binding=enforce),
            )

        assert exc.value.status_code == 403

    @pytest.mark.parametrize("enforce", [True, False])
    def test_missing_invocation_id_rejected_under_both_flag_values(self, enforce):
        with pytest.raises(HTTPException) as exc:
            _resolve(
                invocation_id=None,
                settings=_settings(enforce_credential_binding=enforce),
            )

        assert exc.value.status_code == 403

    def test_settings_flag_is_never_read(self):
        """Belt-and-braces: touching the attribute at all is a design smell here."""
        settings = _settings()
        # A property that explodes if read — proves independence structurally, not
        # by grepping source text.
        type(settings).enforce_credential_binding = property(
            lambda _self: (_ for _ in ()).throw(AssertionError("resolve_installation_binding must not read enforce_credential_binding"))
        )
        try:
            binding = _resolve(items=[_row()], settings=settings)
            assert binding.installation_id == _BOUND
        finally:
            del type(settings).enforce_credential_binding
