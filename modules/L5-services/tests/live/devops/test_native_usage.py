"""AWS task billing omissions must not become invented zero-cost evidence."""

import json

import pytest

from native_usage import TASK_API, UNAVAILABLE_REASON, verify_native_usage
from selector import qualify_journeys
from test_selector import FakeReader, NOW, offline_drive, offline_policy
from test_selector import selector_case as selector_case


def unavailable(requests=1, elapsed_seconds=5):
    return {"cost_coverage": "unavailable", "cost_source": TASK_API,
            "cost_reason": UNAVAILABLE_REASON, "cost_scope": "native_task",
            "requests": requests, "elapsed_seconds": elapsed_seconds}


LIMIT = {"max_spend_usd": "0.25", "max_requests": 4, "deadline_seconds": 60}


def test_native_task_absence_is_preserved_by_real_selector(request):
    manifest, path, directory, release_reader = request.getfixturevalue("selector_case")

    class NativeBillingUnavailable(FakeReader):
        def read_capabilities(self, target):
            results = super().read_capabilities(target)
            for result in results:
                result["usage"] = unavailable()
            return results

        def read_journeys(self, target):
            results = super().read_journeys(target)
            for result in results:
                result["usage"] = unavailable()
            return results

    run_id = qualify_journeys(
        manifest["release"]["source_sha"], path, directory,
        policy_reader=offline_policy, journey_runner=offline_drive,
        reader_factory=lambda config, **bounds: NativeBillingUnavailable(config, release_reader, **bounds), now=NOW,
    )
    receipt = json.loads((directory / f"devops-{run_id}-journeys.json").read_text())
    assert all(result["usage"]["cost_coverage"] == "unavailable" for result in receipt["capabilities"])
    usage = receipt["native_usage"]["investigation"]
    assert usage == {"measured_spend_usd": None, "estimated_spend_usd": None,
                     "total_spend_usd": None, "unavailable_task_count": 4, "requests": 4}


@pytest.mark.parametrize("change", [
    {"spend_usd": "0"}, {"estimated_spend_usd": "0"}, {"cost_reason": ""},
    {"cost_source": "aws-devops-agent:GetAccountUsage"}, {"cost_scope": "monthly_account"},
    {"requests": 5}, {"requests": True}, {"elapsed_seconds": 61}, {"elapsed_seconds": -1},
])
def test_unknown_cost_cannot_hide_fabrication_or_other_exceeded_bounds(change):
    with pytest.raises(ValueError):
        verify_native_usage({**unavailable(), **change}, LIMIT)


def test_estimate_stays_distinct_from_measurement_and_requires_provenance():
    usage = {"cost_coverage": "estimated", "estimated_spend_usd": "0.10",
             "estimate_ref": "sha256:documented-price-and-duration", "requests": 1, "elapsed_seconds": 5}
    coverage, amount = verify_native_usage(usage, LIMIT)
    assert coverage == "estimated" and str(amount) == "0.10"
    for change in ({"estimate_ref": ""}, {"spend_usd": "0.10"}, {"estimated_spend_usd": "0.30"}):
        with pytest.raises(ValueError):
            verify_native_usage({**usage, **change}, LIMIT)


@pytest.mark.parametrize("amount", [None, "NaN", "-1", "Infinity", "0.30"])
def test_known_cost_still_requires_finite_usage_within_the_limit(amount):
    with pytest.raises(ValueError):
        verify_native_usage({"spend_usd": amount, "requests": 1, "elapsed_seconds": 5}, LIMIT)
