"""Offline exact-result checks for the FO07 interruption procedure."""

from copy import deepcopy
import os
from pathlib import Path

import pytest

from l5_services.finops_reconciliation import normalize_cost_result, unavailable_native_cost
from test_fo04 import cost_case


def test_installed_recovery_components_match_release():
    installed_root = os.environ.get("FO07_INSTALLED_ROOT")
    if installed_root is None:
        pytest.skip("only checked in the installed-wheel recovery selector")

    from l5_services import billing_collection, findings, run_access
    from l5_services.adapters.finops import billing
    from l5_services.api import router
    from l5_services.publication import recovery
    from l5_services.workers import execution, relay

    installed = Path(installed_root).resolve()
    for component in (billing_collection, findings, run_access, billing, router,
                      recovery, execution, relay):
        assert Path(component.__file__).resolve().is_relative_to(installed)


@pytest.mark.parametrize(
    ("case", "amount", "coverage", "comparison"),
    [
        ("missing", None, "unavailable", "unavailable"),
        ("partial", None, "partial", "incomparable"),
        ("unknown", "10.00000001", "complete", "incomparable"),
        ("zero", "0.000", "complete", "exact"),
    ],
)
def test_interrupted_billing_never_invents_zero(case, amount, coverage, comparison):
    run, native, billing = cost_case(case)
    result = normalize_cost_result(run, native, billing)

    assert result["run_id"] == run.run_id
    assert result["scope_digest"] == run.admission["billing_scope"]["scope_digest"]
    assert result["native"]["answer_artifact"] == run.provider_readback_ref["answer_artifact"]
    assert result["oracle"]["measure"]["amount"] == amount
    assert result["oracle"]["measure"]["coverage"] == coverage
    assert result["comparison"]["state"] == comparison
    assert result["oracle"]["pages"] == billing["pages"]


def test_unknown_native_stays_unknown_even_with_complete_zero_billing():
    run, native, billing = cost_case("zero")
    run.provider_readback_ref["status"] = "outcome_unknown"
    native.update(status="outcome_unknown", amount=None, coverage="unavailable",
                  estimated=None, series=[])

    result = normalize_cost_result(run, native, billing)
    assert result["native"]["status"] == "outcome_unknown"
    assert result["native"]["measure"]["amount"] is None
    assert result["oracle"]["measure"]["amount"] == "0.000"
    assert result["comparison"]["state"] == "unavailable"
    assert result["comparison"]["delta"] is None
    assert result["native"]["answer_artifact"] == run.provider_readback_ref["answer_artifact"]


def test_credit_survives_recovery_without_rounding_or_answer_replacement():
    run, native, billing = cost_case("stale")
    result = normalize_cost_result(run, native, billing)
    raw_credit = run.pages[0]["response"]["ResultsByTime"][0]["Groups"][1]
    assert raw_credit["Metrics"]["UnblendedCost"]["Amount"] == "-0.125"
    assert native["series"][1]["amount"] == billing["series"][1]["amount"] == "-0.125"
    assert result["native"]["measure"]["amount"] == "10.00000001"
    assert result["oracle"]["measure"]["amount"] == "10.00000001"
    assert result["comparison"]["state"] == "incomparable"
    assert result["oracle"]["measure"]["freshness_reason"] == "stale"

    replaced_answer = deepcopy(native)
    replaced_answer["answer_artifact"]["version"] = "2"
    with pytest.raises(ValueError, match="original answer"):
        normalize_cost_result(run, replaced_answer, billing)


def test_prose_answer_keeps_independent_ce_without_claiming_native_cost():
    run, _, billing = cost_case("zero")
    native = unavailable_native_cost(run, billing["query_hash"])
    result = normalize_cost_result(run, native, billing)
    assert result["native"]["answer_artifact"] == run.provider_readback_ref["answer_artifact"]
    assert result["native"]["measure"]["coverage"] == "unavailable"
    assert result["native"]["measure"]["amount"] is None
    assert result["oracle"]["measure"]["amount"] == "0.000"
    assert result["oracle"]["pages"] == billing["pages"]
    assert result["comparison"]["state"] == "unavailable"
    run.provider_readback_ref = None
    with pytest.raises(ValueError, match="original native answer"):
        unavailable_native_cost(run, billing["query_hash"])
