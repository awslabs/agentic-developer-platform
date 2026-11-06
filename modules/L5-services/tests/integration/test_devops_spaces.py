"""Offline native-space fixtures; no provider credentials or service are used."""

import pytest

from l5_services.adapters.devops.spaces import SpaceAdapter, SpaceIdentityError, SpaceSelection


class Spaces:
    def __init__(self):
        self.spaces = {}
        self.calls = []

    def list_agent_spaces(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {"agentSpaces": [{"agentSpaceId": identity, "name": data["name"]}
                                for identity, data in self.spaces.items()]}

    def create_agent_space(self, **kwargs):
        self.calls.append(("create", kwargs))
        identity = "space-example-1"
        self.spaces[identity] = kwargs
        return {"agentSpace": {"agentSpaceId": identity}}

    def get_agent_space(self, *, agentSpaceId):
        self.calls.append(("get", agentSpaceId))
        data = self.spaces[agentSpaceId]
        return {"agentSpace": {"agentSpaceId": agentSpaceId, "name": data["name"]},
                "tags": data.get("tags", {})}


@pytest.fixture
def selection():
    return SpaceSelection("tenant-example", "product-example", "project-example", "binding-example",
                          "123456789012", "example-region-1")


def test_provider_create_adopt_replay(selection):
    provider = Spaces()
    adapter = SpaceAdapter(provider, account_id=selection.account_id, region=selection.region)
    created = adapter.create(selection, "operation-example")
    assert (created.agent_space_id, created.account_id, created.region, created.ownership) == (
        "space-example-1", selection.account_id, selection.region, "adp_created")
    assert adapter.create(selection, "operation-example") == created
    assert len([call for call in provider.calls if call[0] == "create"]) == 1
    assert adapter.adopt(selection, created.agent_space_id).ownership == "customer_linked"
    assert provider.calls[-1] == ("get", created.agent_space_id)


def test_provider_rejects_mismatched_target_and_receipt(selection):
    provider = Spaces()
    adapter = SpaceAdapter(provider, account_id="000000000000", region=selection.region)
    with pytest.raises(SpaceIdentityError, match="hosting connection"):
        adapter.create(selection, "operation-example")
    assert provider.calls == []
    adapter = SpaceAdapter(provider, account_id=selection.account_id, region=selection.region)
    provider.spaces["space-foreign"] = {"name": f"adp-devops-{selection.fingerprint()[:32]}", "tags": {}}
    with pytest.raises(SpaceIdentityError, match="creation receipt"):
        adapter.create(selection, "operation-example")
    assert not any(call[0] == "create" for call in provider.calls)


def test_adoption_verifies_native_id_without_creation_ownership(selection):
    provider = Spaces()
    provider.spaces["space-customer"] = {"name": "customer managed"}
    adapter = SpaceAdapter(provider, account_id=selection.account_id, region=selection.region)
    assert adapter.adopt(selection, "space-customer").ownership == "customer_linked"
    provider.get_agent_space = lambda **kwargs: {"agentSpace": {"agentSpaceId": "other"}}
    with pytest.raises(SpaceIdentityError, match="different native space ID"):
        adapter.adopt(selection, "space-customer")
