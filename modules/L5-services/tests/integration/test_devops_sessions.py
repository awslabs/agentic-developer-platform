from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from l5_services.adapters.devops.spaces import SpaceSelection
from l5_services.devops.access import authorize_browser_launch

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def launch_fixture():
    selection = SpaceSelection("tenant", "product", "project", "binding", "123456789012", "us-east-1")
    actor = SimpleNamespace(user_id="person", org_id="tenant", account_type="human",
                            auth_source="jwt", expires_at=NOW + timedelta(minutes=10))
    member = SimpleNamespace(id="person", org_id="tenant", user_kind="human", is_shadow=False)
    configuration = SimpleNamespace(tenant_id="tenant", product_id="product", project_id="project",
                                    service_id="devops", relationship_revision="3", current_revision=2,
                                    handoff_state="linked")
    version = SimpleNamespace(revision=2, state="ready", settings={"devops": {
        "browser_launch_policy": "scoped_session", "authentication_mode": "scoped_iam_federation",
        "permitted_native_actions": ["launchNativeBrowser"],
    }})
    receipt = SimpleNamespace(tenant_id="tenant", configuration_revision=2, state="ready",
                              native_space_id="space", scope_fingerprint=selection.fingerprint(),
                              selection=vars(selection).copy())
    return {"actor": actor, "member": member, "configuration": configuration, "version": version,
            "receipt": receipt, "product_id": "product", "project_id": "project", "binding_id": "binding",
            "configuration_revision": 2, "relationship_revision": 3, "action_allowed": True, "now": NOW}


def test_authorized_launch_uses_verified_binding():
    values = launch_fixture()
    selection = authorize_browser_launch(**values)
    assert selection.binding_id == "binding"
    assert selection.account_id == "123456789012"


def test_two_project_member_policies_keep_distinct_binding_selections():
    first = launch_fixture()
    second = launch_fixture()
    second_selection = SpaceSelection("tenant", "another-product", "another-project", "another-binding",
                                      "210987654321", "us-west-2")
    second["actor"].user_id = "another-person"
    second["member"].id = "another-person"
    second.update(product_id=second_selection.product_id, project_id=second_selection.project_id,
                  binding_id=second_selection.binding_id)
    second["configuration"].product_id = second_selection.product_id
    second["configuration"].project_id = second_selection.project_id
    second["receipt"].selection = vars(second_selection).copy()
    second["receipt"].scope_fingerprint = second_selection.fingerprint()
    actions_by_member_and_project = {
        ("product", "project", "person"): {"launchNativeBrowser"},
        ("another-product", "another-project", "another-person"): {"launchNativeBrowser"},
    }

    for values, expected in ((first, SpaceSelection("tenant", "product", "project", "binding",
                                                    "123456789012", "us-east-1")), (second, second_selection)):
        scope = (values["product_id"], values["project_id"], values["actor"].user_id)
        values["action_allowed"] = "launchNativeBrowser" in actions_by_member_and_project.get(scope, set())
        assert authorize_browser_launch(**values).fingerprint() == expected.fingerprint()

    actions_by_member_and_project.pop(("another-product", "another-project", "another-person"))
    second["action_allowed"] = "launchNativeBrowser" in actions_by_member_and_project.get(
        (second["product_id"], second["project_id"], second["actor"].user_id), set(),
    )
    with pytest.raises(HTTPException) as denied:
        authorize_browser_launch(**second)
    assert denied.value.status_code == 404


@pytest.mark.parametrize("change", [
    lambda values: values.update(actor=None),
    lambda values: setattr(values["actor"], "auth_source", "iam"),
    lambda values: setattr(values["actor"], "account_type", "service"),
    lambda values: setattr(values["actor"], "expires_at", NOW),
    lambda values: setattr(values["member"], "id", "another"),
    lambda values: values.update(action_allowed=False),
    lambda values: values.update(binding_id="foreign"),
    lambda values: values.update(product_id="foreign"),
    lambda values: values.update(project_id="foreign"),
    lambda values: values["receipt"].selection.update(binding_id="foreign"),
    lambda values: values["receipt"].selection.update(project_id="foreign"),
    lambda values: setattr(values["receipt"], "state", "suspended"),
    lambda values: setattr(values["receipt"], "scope_fingerprint", "tampered"),
    lambda values: setattr(values["configuration"], "current_revision", 3),
    lambda values: setattr(values["configuration"], "relationship_revision", "2"),
    lambda values: setattr(values["version"], "state", "disabled"),
    lambda values: values.update(version=None),
    lambda values: values.update(receipt=None),
    lambda values: setattr(values["receipt"], "native_space_id", None),
    lambda values: values["receipt"].selection.update(account_id="987654321098"),
    lambda values: values["version"].settings["devops"].update(browser_launch_policy="disabled"),
])
def test_denied_launch_cannot_resolve_provider_scope(change):
    values = launch_fixture()
    change(values)
    with pytest.raises(HTTPException) as denied:
        authorize_browser_launch(**values)
    assert denied.value.status_code in {401, 404}
