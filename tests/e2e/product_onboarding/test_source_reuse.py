"""Local source identity contract; independent deployed readback is separate."""

from copy import deepcopy

import pytest

from modules.gateway.tests.products.source_reuse import assert_source_reuse


@pytest.fixture
def source_observations():
    provider_repository = {"id": 35, "owner": {"id": 27}, "updated_at": "repo-rev"}
    provider_environment = {"account_id": "000000000000", "region": "us-east-1", "role_ref": "role/example"}
    repository = {"kind": "repository", "namespace": "27", "native_id": "35", "source_revision": "repo-rev"}
    environment = {
        "kind": "environment", "namespace": "000000000000", "native_id": "us-east-1",
        "source_revision": "secret-version", "observed_at": "2026-10-20T00:00:00Z",
        "verified_environment": {
            "provider": "aws", "account_id": "000000000000", "regions": ["us-east-1"],
            "role_ref": "role/example", "source_revision": "secret-version",
            "verified_at": "2026-10-20T00:00:00Z",
        },
    }
    return repository, environment, provider_repository, provider_environment


def test_source_reuse_compares_independent_native_ids_and_versions(source_observations):
    before = deepcopy(source_observations)
    assert_source_reuse(*source_observations)
    assert source_observations == before


@pytest.mark.parametrize("index,path,wrong", [
    (0, ("native_id",), "0035"),
    (0, ("namespace",), "29"),
    (0, ("source_revision",), "stale"),
    (1, ("native_id",), "us-west-2"),
    (1, ("verified_environment", "account_id"), "000000000001"),
    (1, ("verified_environment", "role_ref"), "role/other"),
    (1, ("verified_environment", "verified_at"), "stale"),
])
def test_source_reuse_rejects_mismatched_provider_identity(source_observations, index, path, wrong):
    observations = deepcopy(source_observations)
    target = observations[index]
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = wrong
    with pytest.raises(AssertionError):
        assert_source_reuse(*observations)
