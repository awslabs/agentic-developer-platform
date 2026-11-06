"""Assert source coverage without mistaking a stale or revoked binding for readiness."""

import json


def assert_source_status(source, binding, *, coverage, known_native_ids, observed_at=None):
    assert source["coverage"] == coverage
    resources = source.get("resources", [])
    assert isinstance(resources, list)
    assert {item["native_id"] for item in resources} == set(known_native_ids)
    if coverage == "unavailable":
        assert source.get("health") != "healthy"
        assert "resources" not in source and "verified_at" not in source
        assert set(source).isdisjoint({"verified_environment", "account_id", "role_ref", "namespace", "native_id"})
        assert binding["status"] == "restricted"
        assert set(binding).isdisjoint({"selection", "observed_at", "verified_environment", "verified_ref"})
        return False

    assert coverage in ("partial", "complete") and resources
    assert binding["status"] in ("healthy", "degraded")
    assert observed_at is not None and binding["observed_at"] == observed_at
    assert all(resource["observed_at"] == source["verified_at"] for resource in resources)
    return binding["status"] == "healthy"


def assert_status_surfaces(source, binding, profile, revision, session, *, phase, observed_at, native_id, private_values):
    if phase == "restricted":
        assert not assert_source_status(source, binding, coverage="unavailable", known_native_ids=set())
        for view in (source, binding):
            for private_value in private_values:
                assert private_value not in json.dumps(view)
    else:
        ready = assert_source_status(source, binding, coverage="partial", known_native_ids={native_id}, observed_at=observed_at)
        assert ready is (phase == "verified")
    for view in (profile, revision, session):
        if view is None:
            assert phase == "restricted"
            continue
        checks = {check["id"]: check["status"] for check in view["progress"]["checks"]}
        assert "environment" in checks
        if phase == "restricted":
            assert checks["environment"] != "complete"
            for private_value in private_values:
                assert private_value not in json.dumps(view)
        else:
            assert checks["environment"] == ("complete" if phase == "verified" else "stale")
