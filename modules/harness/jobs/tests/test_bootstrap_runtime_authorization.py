from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs.bootstrap_runtime_authorization import (
    SETTLED_REAUTHORIZATION_KIND,
    runtime_amendment,
    runtime_authorization,
    settled_runtime_amendment,
)
from harness_jobs.identity import OperationRefused


def amendment(original=3600, requested=10800):
    return {
        "version": 1,
        "kind": "bootstrap-runtime-extension-v1",
        "original_max_runtime_seconds": original,
        "max_runtime_seconds": requested,
    }


@pytest.mark.parametrize("original", [1, 900, 3600])
def test_explicit_bootstrap_runtime_preserves_original_value(original):
    terms = amendment(original)
    assert runtime_amendment(terms, original) == 10800
    assert terms == amendment(original)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("kind", "another-scope"),
        ("original_max_runtime_seconds", 900),
        ("original_max_runtime_seconds", True),
        ("max_runtime_seconds", True),
        ("max_runtime_seconds", 10800.0),
        ("max_runtime_seconds", "10800"),
        ("max_runtime_seconds", 10801),
        ("max_runtime_seconds", 3600),
        ("max_runtime_seconds", 900),
        ("extra", "unreviewed"),
    ],
)
def test_runtime_amendment_refuses_changed_or_unbounded_terms(field, value):
    terms = amendment()
    terms[field] = value
    with pytest.raises(OperationRefused):
        runtime_amendment(terms, 3600)


@pytest.mark.parametrize("missing", list(amendment()))
def test_runtime_amendment_requires_every_reviewed_field(missing):
    terms = amendment()
    del terms[missing]
    with pytest.raises(OperationRefused):
        runtime_amendment(terms, 3600)


@pytest.mark.parametrize(
    "scope,expiry",
    [
        ("unstarted-planning", "future"),
        ("unstarted-bootstrap", "past"),
        ("unstarted-bootstrap", "naive"),
        ("unstarted-bootstrap", None),
    ],
)
def test_runtime_receipt_requires_current_bootstrap_authority(scope, expiry):
    now = datetime.now(UTC)
    values = {
        "future": (now + timedelta(hours=3)).isoformat(),
        "past": (now - timedelta(seconds=1)).isoformat(),
        "naive": "2099-01-01T00:00:00",
        None: None,
    }
    with pytest.raises(OperationRefused):
        runtime_authorization(
            {
                "runtime_amendment": amendment(),
                "runtime_authority_expires_at": values[expiry],
            },
            3600,
            scope,
        )


def test_missing_amendment_retains_original_runtime():
    assert runtime_authorization({}, 3600, "unstarted-bootstrap") == (3600, None)


def test_equal_three_hour_cap_requires_distinct_settled_terms():
    terms = {**amendment(10800), "kind": SETTLED_REAUTHORIZATION_KIND}
    assert settled_runtime_amendment(terms, 10800) == 10800
    with pytest.raises(OperationRefused):
        runtime_amendment(terms, 10800)
    with pytest.raises(OperationRefused):
        settled_runtime_amendment(amendment(10800), 10800)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("extra", "unreviewed"),
        ("original_max_runtime_seconds", 3600),
        ("original_max_runtime_seconds", True),
        ("max_runtime_seconds", 10801),
        ("max_runtime_seconds", 10799),
        ("max_runtime_seconds", 3600),
        ("max_runtime_seconds", 10800.0),
        ("max_runtime_seconds", True),
        ("max_runtime_seconds", None),
    ],
)
def test_settled_same_cap_terms_cannot_expand_or_change_original(field, value):
    terms = {**amendment(10800), "kind": SETTLED_REAUTHORIZATION_KIND, field: value}
    with pytest.raises(OperationRefused):
        settled_runtime_amendment(terms, 10800)


@pytest.mark.parametrize("closure", [None, {}, {"kind": "settled-bootstrap-v1"}])
def test_same_cap_runtime_receipt_requires_valid_settled_closure(closure):
    proof = {
        "runtime_amendment": {**amendment(10800), "kind": SETTLED_REAUTHORIZATION_KIND},
        "runtime_authority_expires_at": (
            datetime.now(UTC) + timedelta(hours=3)
        ).isoformat(),
        "settled_bootstrap": closure,
    }
    with pytest.raises(OperationRefused):
        runtime_authorization(proof, 10800, "unstarted-bootstrap")


@pytest.mark.parametrize("original", [True, 3600, 10800.0, 10801])
def test_only_original_integer_three_hour_cap_can_be_reauthorized(original):
    terms = {**amendment(10800), "kind": SETTLED_REAUTHORIZATION_KIND}
    with pytest.raises(OperationRefused):
        settled_runtime_amendment(terms, original)
