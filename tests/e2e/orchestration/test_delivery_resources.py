"""Offline write-ahead worker resource inventory and ownership checks."""

from dataclasses import replace
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.inventory import Inventory
from tests.e2e.orchestration.scenarios.delivery_resources import (
    DeliveryResourceProvider,
    plan_resources,
)
from tests.e2e.orchestration.scenarios.http import Unsupported


def test_worker_resource_intents_precede_any_dispatch(valid_config):
    inventory = Inventory.create(
        valid_config.artifact_directory,
        "q-resources0123456789",
        valid_config.environment,
    )
    client = Mock()
    session = NS(config=valid_config, inventory=inventory, client=client)
    with pytest.raises(Unsupported, match="23 inventory slots"):
        plan_resources(session)
    assert inventory.fixtures == []
    session.config = replace(
        valid_config, bounds={**valid_config.bounds, "max_resources": 23}
    )
    plan_resources(session)
    assert len(inventory.fixtures) == 4
    assert {r.kind for r in inventory.fixtures} == {
        "qualification-pr",
        "qualification-branch",
    }
    assert all(r.state == "planned" for r in inventory.fixtures)
    client.request.assert_not_called()


@pytest.mark.parametrize("attack", ["head", "branch", "fork", "base"])
def test_changed_or_foreign_worker_resource_is_not_owned(valid_config, attack):
    qid = "q-resources0123456789"
    branch = f"qualification/{qid}/story-1"
    pr = {
        "number": 1,
        "head": {
            "sha": "a" * 40,
            "ref": branch,
            "repo": {"full_name": valid_config.repository},
        },
        "base": {"repo": {"full_name": valid_config.repository}},
    }
    client = Mock(config=valid_config)
    client.get.return_value = pr
    provider = DeliveryResourceProvider(client, "qualification-pr")
    descriptor = provider.descriptor(pr, qid)
    assert provider.read_tags(descriptor) == valid_config.ownership_tags(qid)
    if attack == "head":
        pr["head"]["sha"] = "b" * 40
    elif attack == "branch":
        pr["head"]["ref"] = "unrelated"
    elif attack == "fork":
        pr["head"]["repo"]["full_name"] = "foreign/repo"
    else:
        pr["base"]["repo"]["full_name"] = "foreign/repo"
    assert provider.read_tags(descriptor) is None
    with pytest.raises(Unsupported, match="reconciliation"):
        provider.delete(descriptor)
    client.request.assert_not_called()
    assert json.loads(descriptor)["head_sha"] == "a" * 40
