"""Shared release reuse needs verified lineage, never a weaker source claim."""

from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding.product_shared_release import verify_shared_source_lineage

RELEASE = "e" * 40
ENGINE = "9" * 40


def runner(*, missing=(), ancestor=True):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["git", "cat-file"]:
            return SimpleNamespace(returncode=1 if command[2].startswith(tuple(missing) or ("\0",)) else 0)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return SimpleNamespace(returncode=0 if ancestor else 1)
        raise AssertionError("Unexpected command")

    return run, calls


def test_identical_revisions_need_no_history_lookup():
    run, calls = runner()
    assert verify_shared_source_lineage({"source_revision": RELEASE},
                                        {"ADP_SOURCE_REVISION": RELEASE}, run=run) is True
    assert calls == []


def test_engine_source_descending_from_the_release_source_is_reused():
    run, calls = runner()
    assert verify_shared_source_lineage(
        {"source_revision": RELEASE},
        {"ADP_SOURCE_REVISION": ENGINE, "ADP_CHECKOUT_REVISION": ENGINE}, run=run) is True
    assert ["git", "merge-base", "--is-ancestor", RELEASE, ENGINE] in calls


def test_absent_engine_transport_does_not_claim_lineage():
    run, calls = runner()
    assert verify_shared_source_lineage({"source_revision": RELEASE}, {}, run=run) is False
    assert calls == []


@pytest.mark.parametrize("problem", ["ancestor", "unknown", "short", "checkout"])
def test_refuse_a_release_outside_the_engine_source_lineage(problem):
    manifest = {"source_revision": "abc" if problem == "short" else RELEASE}
    environment = {"ADP_SOURCE_REVISION": ENGINE,
                   "ADP_CHECKOUT_REVISION": RELEASE if problem == "checkout" else ENGINE}
    run, _ = runner(missing=(RELEASE,) if problem == "unknown" else (),
                    ancestor=problem != "ancestor")
    with pytest.raises(ValueError):
        verify_shared_source_lineage(manifest, environment, run=run)
