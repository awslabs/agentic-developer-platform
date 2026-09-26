"""Pure cleanup graph compilation over real original network journal writes."""

# ruff: noqa: F811
import json

import pytest

from harness_jobs.identity import OperationRefused
from superplane_executor.cleanup_recipes import network_recipes
from superplane_executor.network_inventory import rows
from test_network import network as network, pytestmark as pytestmark
from network_support import REMOTE


async def test_recipe_compilation_preserves_every_original_key_without_sdk_calls(
    network,
):
    runtime, aws = network
    await runtime.establish(REMOTE)
    original = await rows(runtime.provider, runtime.operation)
    calls = list(aws.calls)
    recipes = network_recipes(runtime.plan, original)
    assert [r["key"] for r in recipes] == list(
        dict.fromkeys(k for k, _, _ in reversed(runtime.recipes))
    )
    assert {r["key"] for r in recipes} == {r["resource_key"] for r in original}
    assert aws.calls == calls


@pytest.mark.parametrize("changed", ["missing", "descriptor", "reference", "extra"])
async def test_partial_or_changed_native_recipe_is_not_a_cleanup_graph(
    network, changed
):
    runtime, aws = network
    await runtime.establish(REMOTE)
    original = [dict(r) for r in await rows(runtime.provider, runtime.operation)]
    if changed == "missing":
        original.pop()
    elif changed == "descriptor":
        original[0]["descriptor"] = json.dumps({"foreign": True})
    elif changed == "reference":
        original[0]["provider_reference"] = None
    else:
        original.append({**original[0], "resource_key": "foreign"})
    calls = list(aws.calls)
    with pytest.raises(OperationRefused):
        network_recipes(runtime.plan, original)
    assert aws.calls == calls
