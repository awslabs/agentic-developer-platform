"""Unrelated registration references refuse before provider or store access."""

import json
from types import SimpleNamespace

import pytest

from workspace_provisioning.bootstrap_runtime import bootstrap
from workspace_provisioning.runtime_config import LifecycleRefused


@pytest.mark.parametrize("admitted", [None, "", " ", "another-vault-reference"])
def test_dedicated_bootstrap_refuses_unverified_reference_before_effects(admitted):
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=SimpleNamespace(org_id="org", workspace_id="ws")),
        request=SimpleNamespace(
            parameters={
                "lifecycle_inputs": json.dumps({"cluster_placement": "dedicated"}),
                "credential_id": admitted,
            }
        ),
    )

    def forbidden():
        raise AssertionError(
            "mismatched reference must refuse before authority/effects"
        )

    with pytest.raises(LifecycleRefused, match="admitted vault reference"):
        bootstrap(
            operation,
            None,
            {"bootstrap_credential_reference_id": "verified-vault-reference"},
            None,
            None,
            None,
            None,
            None,
            forbidden,
            None,
        )
