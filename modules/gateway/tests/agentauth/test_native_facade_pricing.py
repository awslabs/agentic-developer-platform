"""Native facade quote-to-reservation checks without a provider or test database."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.agentauth.native_test_facade_admission import _priced_hold
from src.agentauth.test_run_admission_store import AdmissionRefusedError


def protected_offer(**change):
    fields = {
        "quoted_usd": "8",
        "reserved_usd": "8",
        "run_cap_usd": "20",
        "quote_total": Decimal("8"),
    }
    fields.update(change)
    return SimpleNamespace(
        receipt=SimpleNamespace(quoted_usd=fields["quoted_usd"], reserved_usd=fields["reserved_usd"]),
        _authority={"cap_usd": fields["run_cap_usd"]},
        _quote=SimpleNamespace(total_usd=fields["quote_total"]),
    )


def test_quote_and_confirmed_reservation_retain_the_shared_run_cap():
    assert _priced_hold(protected_offer()) == ("8", "8", "20")


@pytest.mark.parametrize(
    "change",
    [
        {"quoted_usd": None},
        {"reserved_usd": None},
        {"reserved_usd": "5"},
        {"quoted_usd": "21", "reserved_usd": "21", "quote_total": Decimal("21")},
        {"quoted_usd": "NaN", "reserved_usd": "NaN"},
        {"run_cap_usd": "200"},
        {"quote_total": Decimal("7")},
    ],
)
def test_missing_unmatched_or_over_cap_price_never_becomes_a_facade_allow(change):
    with pytest.raises(AdmissionRefusedError, match="native_test_facade_price_unavailable"):
        _priced_hold(protected_offer(**change))
