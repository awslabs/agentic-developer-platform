"""Local status assertions use a pinned fixture clock, not a deployed connection."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.products.source_routes import binding_view, document_event_support
from tests.products.source_status import assert_source_status


def fixture_binding(observed_at):
    return SimpleNamespace(
        id="owned-binding",
        project_id="owned-project",
        generation=1,
        revision=1,
        status="healthy",
        desired={"native_id": "us-east-1"},
        observed={
            "observed_at": observed_at.isoformat(),
            "stale_after": (observed_at + timedelta(minutes=15)).isoformat(),
            "source_revision": "fixture-version",
            "verified_environment": {"role_ref": "role/example"},
        },
    )


def fixture_source(observed_at):
    stamp = observed_at.isoformat()
    return {
        "coverage": "partial",
        "health": "healthy",
        "verified_at": stamp,
        "resources": [{"kind": "environment", "native_id": "us-east-1", "observed_at": stamp}],
    }


def test_document_feed_remains_unavailable_without_a_verified_revision_producer():
    linked = [{"kind": "document", "record_ref": {"record_id": "document-a"}}, {"kind": "tracker", "record_ref": {"record_id": "tracker-a"}}]
    assert document_event_support(linked) == {"event_support": {"document_revision": "unavailable"}}
    assert document_event_support([linked[1]]) == {}
    assert document_event_support([{"record_type": "document", "record_id": "document-a"}]) == {"event_support": {"document_revision": "unavailable"}}
    assert linked[0] == {"kind": "document", "record_ref": {"record_id": "document-a"}}


def test_partial_connection_preserves_observed_time_without_claiming_other_regions():
    observed_at = datetime(2026, 10, 20, tzinfo=UTC)
    binding = fixture_binding(observed_at)
    source = fixture_source(observed_at)
    before = binding_view(binding, True, observed_at + timedelta(minutes=14, seconds=59))
    assert assert_source_status(source, before, coverage="partial", known_native_ids={"us-east-1"}, observed_at=observed_at.isoformat())
    assert binding_view(binding, True, observed_at + timedelta(minutes=15))["observed_at"] == observed_at.isoformat()
    at_expiry = binding_view(binding, True, observed_at + timedelta(minutes=15))
    assert not assert_source_status(source, at_expiry, coverage="partial", known_native_ids={"us-east-1"}, observed_at=observed_at.isoformat())
    with pytest.raises(AssertionError):
        assert_source_status(
            {**source, "coverage": "complete"}, before, coverage="partial", known_native_ids={"us-east-1"}, observed_at=observed_at.isoformat()
        )
    with pytest.raises(AssertionError):
        assert_source_status(source, before, coverage="partial", known_native_ids={"us-east-1", "us-west-2"}, observed_at=observed_at.isoformat())


def test_unavailable_connection_and_revocation_do_not_expose_verified_metadata():
    observed_at = datetime(2026, 10, 20, tzinfo=UTC)
    binding = fixture_binding(observed_at)
    source = {"coverage": "unavailable", "health": "unverified"}
    inaccessible = binding_view(binding, False, observed_at)
    assert not assert_source_status(source, inaccessible, coverage="unavailable", known_native_ids=set())
    binding.status = "revoked"
    revoked = binding_view(binding, True, observed_at)
    assert not assert_source_status(source, revoked, coverage="unavailable", known_native_ids=set())
    for disclosed in (
        {**source, "resources": fixture_source(observed_at)["resources"]},
        {**source, "verified_at": observed_at.isoformat()},
        {**source, "verified_environment": {"role_ref": "role/example"}},
        {**revoked, "verified_environment": {"role_ref": "role/example"}},
    ):
        with pytest.raises(AssertionError):
            if "status" in disclosed:
                assert_source_status(source, disclosed, coverage="unavailable", known_native_ids=set())
            else:
                assert_source_status(disclosed, revoked, coverage="unavailable", known_native_ids=set())
