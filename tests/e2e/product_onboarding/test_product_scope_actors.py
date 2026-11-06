"""Controlled ordinary-editor setup and membership recovery, not deployed evidence."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.e2e.new_ui.product_fixture_provider import BrowserFixtureProvider
from tests.e2e.new_ui.product_fixture_bootstrap import preflight_b03
from tests.e2e.new_ui.product_scope_actors import (
    restore_scope_owner_role,
    share_scope_team,
)
from tests.e2e.new_ui.product_scope_fixture import prepare_scope_fixture
from tests.e2e.product_onboarding.test_product_scope_producer import (
    ProductAPI,
    Response,
    State,
)
from tests.unit.test_product_browser_provider import (
    API,
    BINDING,
    ORIGIN,
    Response as BrowserResponse,
    S3,
    SOURCE,
    state,
)


class EditorProductAPI(ProductAPI):
    def __init__(self):
        super().__init__()
        self.team_id = None
        self.owner_role = "member"
        self.owner_updated_at = "before"
        self.lose_team_receipt = False
        self.fail_restore = False
        self.lose_role_promotion = False

    def get(self, url, **kwargs):
        if (
            kwargs["headers"]["Authorization"] == "Bearer editor"
            and self.team_id != "editor-team"
        ):
            return Response({}, 404)
        response = super().get(url, **kwargs)
        if response.status == 200:
            response.value["product"]["team_id"] = self.team_id
        return response

    def put(self, url, headers, data, **kwargs):
        assert url.endswith(
            "/api/admin/organizations/example-tenant/users/example-owner"
        )
        assert headers["Authorization"] == "Bearer control"
        if data["role"] == "member" and self.fail_restore:
            raise ConnectionError("owner restoration response failed")
        self.owner_role = data["role"]
        self.owner_updated_at = "after"
        if self.owner_role == "org_admin" and self.lose_role_promotion:
            self.lose_role_promotion = False
            raise ConnectionError("owner promotion response was lost")
        return Response({"role": self.owner_role})

    def patch(self, url, headers, data, **kwargs):
        if "team_id" not in data:
            return super().patch(url, headers=headers, data=data, **kwargs)
        key = headers["Idempotency-Key"]
        if key in self.operations:
            return Response(self.operations[key])
        assert int(headers["If-Profile-Revision"]) == self.revision
        assert data == {"team_id": "editor-team"} and self.owner_role == "org_admin"
        self.revision += 1
        self.mutations += 1
        self.team_id = data["team_id"]
        result = {"product": {"team_id": self.team_id}}
        self.operations[key] = result
        if self.lose_team_receipt:
            self.lose_team_receipt = False
            raise ConnectionError("team assignment committed without a receipt")
        return Response(result)


@pytest.fixture
def prepared(tmp_path):
    binding = {
        **State.binding,
        "editor_id": "editor",
        "editor_subject": "editor-sub",
        "editor_team_id": "editor-team",
    }
    selected = State()
    selected.binding = binding
    selected.document["binding"] = binding
    api = EditorProductAPI()
    owner = {
        "actor_id": "example-owner",
        "tenant_id": "example-tenant",
        "access_token": "owner",
    }
    product = {
        "product_id": "owned-product",
        "project_id": "first",
        "product_name": "Owned Product",
    }
    inputs = [
        {
            "selection": {
                "kind": "repository",
                "provider": "github",
                "connection_ref": {"kind": "github_installation", "id": str(index + 1)},
                "namespace": "example-org",
                "native_id": "example-repository-" + str(index + 1),
            },
            "record_revision": "b" * 40,
            "release_target_date": f"202{index + 7}-12-01",
        }
        for index in range(2)
    ]
    fixture = prepare_scope_fixture(
        selected,
        api,
        "https://example.invalid",
        owner,
        product,
        inputs,
        tmp_path / "private-scope.json",
    )
    editor = {
        "actor_id": "editor",
        "tenant_id": "example-tenant",
        "access_token": "editor",
    }
    baseline = {
        "role": "member",
        "team_id": "",
        "teams": [],
        "membership_status": "active",
    }
    selected.document["membership"] = {"resource": baseline}
    membership = SimpleNamespace(
        org_path="https://example.invalid/api/admin/organizations/example-tenant",
        read=lambda: {
            "resource": {
                **baseline,
                "role": api.owner_role,
                "updated_at": api.owner_updated_at,
            }
        },
    )
    provider = SimpleNamespace(
        state=selected,
        membership=membership,
        usage_control={"access_token": "control"},
        request=api,
        binding=binding,
        base_url="https://example.invalid",
        owner=owner,
        editor=editor,
        editor_membership=object(),
        headers=lambda session: {"Authorization": "Bearer " + session["access_token"]},
    )
    return provider, fixture


def test_ordinary_editor_sees_shared_identity_without_scope_transfer(prepared):
    provider, fixture = prepared
    selected = share_scope_team(provider, fixture)
    assert selected == provider.editor
    assert provider.request.team_id == "editor-team"
    assert provider.request.owner_role == "member"
    assert provider.state.document["scope_owner_role_restore_required"] is False
    assert provider.state.document["scope"]["team_share"]["previous_team_id"] is None
    assert provider.request.mutations == 6
    assert share_scope_team(provider, fixture) == selected
    assert provider.request.mutations == 6


def test_lost_team_response_replays_original_key(prepared):
    provider, fixture = prepared
    provider.request.lose_team_receipt = True
    with pytest.raises(ConnectionError, match="without a receipt"):
        share_scope_team(provider, fixture)
    intent = deepcopy(provider.state.document["scope"]["team_share"])
    assert provider.request.team_id == "editor-team"
    assert share_scope_team(provider, fixture) == provider.editor
    assert provider.state.document["scope"]["team_share"] == intent
    assert provider.request.mutations == 6


def test_interrupted_owner_promotion_restores_ordinary_role(prepared):
    provider, fixture = prepared
    provider.request.lose_role_promotion = True
    with pytest.raises(ConnectionError, match="promotion response"):
        share_scope_team(provider, fixture)
    assert provider.request.owner_role == "member"
    assert provider.state.document["scope"]["team_share"]["key"]
    assert share_scope_team(provider, fixture) == provider.editor
    assert provider.request.owner_role == "member"


def test_failed_owner_role_restoration_keeps_durable_claim(prepared):
    provider, fixture = prepared
    provider.request.fail_restore = True
    with pytest.raises(ConnectionError, match="restoration response"):
        share_scope_team(provider, fixture)
    assert provider.request.owner_role == "org_admin"
    assert provider.state.document["scope_owner_role_restore_required"] is True
    provider.request.fail_restore = False
    restore_scope_owner_role(provider)
    assert provider.request.owner_role == "member"
    assert provider.state.document["scope_owner_role_restore_required"] is False
    assert share_scope_team(provider, fixture) == provider.editor


def test_editor_assignment_intent_is_durable_before_mutation(prepared):
    provider, fixture = prepared
    provider.state.fail_at = provider.state.saved + 1
    with pytest.raises(OSError, match="durable state"):
        share_scope_team(provider, fixture)
    assert provider.request.team_id is None and provider.request.mutations == 5
    assert "team_share" not in provider.state.document["scope"]


def test_mismatched_or_unverified_editor_cannot_share_product(prepared):
    provider, fixture = prepared
    provider.editor = {**provider.editor, "actor_id": "other"}
    with pytest.raises(ValueError, match="durable owned fixture"):
        share_scope_team(provider, fixture)
    provider.editor["actor_id"] = "editor"
    provider.request.team_id = "external-team"
    with pytest.raises(ValueError, match="not journaled"):
        share_scope_team(provider, fixture)
    assert provider.request.mutations == 5


class EditorMembershipAPI(API):
    def __init__(self, *, connected=False):
        super().__init__()
        self.connected = connected
        self.editor_status = "active"
        self.editor_role = "member"
        self.editor_team = "editor-team"
        self.editor_subject = "editor-sub"

    def member(self):
        value = super().member()
        value["resource"]["role"] = getattr(self, "owner_role", "member")
        if self.connected:
            value["resource"]["team_id"] = getattr(self, "owner_team", "editor-team")
            value["resource"]["teams"] = [{"team_id": "editor-team", "role": "member", "is_primary": True}]
        return value

    def put(self, url, headers, data, **kwargs):
        assert url.endswith("/api/admin/organizations/owned-workspace/users/owner")
        assert headers["Authorization"] == "Bearer control"
        self.owner_role = data["role"]
        return BrowserResponse(200, {"role": self.owner_role})

    def get(self, url, **kwargs):
        if url.endswith("/owned-workspace/users"):
            response = super().get(url, **kwargs)
            response.value["users"].append(
                {
                    "id": "editor",
                    "org_id": "owned-workspace",
                    "cognito_sub": self.editor_subject,
                    "cognito_username": "eval-regression-editor" if self.connected else "editor",
                    "role": self.editor_role,
                }
            )
            if self.connected:
                response.value["users"][0]["cognito_username"] = getattr(self, "owner_login", "eval-regression-owner")
                response.value["users"][1]["cognito_username"] = getattr(self, "editor_login", "eval-regression-editor")
                response.value["users"].append({"id": "existing-tenant-user", "org_id": "owned-workspace"})
                response.value["total"] = 4 if getattr(self, "missing_inventory", False) else 3
            return response
        if self.connected and url.endswith("/api/api/admin/identity/organizations/owned-workspace"):
            return BrowserResponse(200, {"id": "owned-workspace", "name": "connected-fixture-tenant"})
        if url.endswith("/hierarchy/member/editor"):
            return BrowserResponse(
                200,
                {
                    "id": "editor",
                    "org_id": "owned-workspace",
                    "kind": "member",
                    "revision": "c" * 64,
                    "resource": {
                        "id": "editor",
                        "org_id": "owned-workspace",
                        "role": "member",
                        "team_id": self.editor_team,
                        "teams": [
                            {
                                "team_id": self.editor_team,
                                "role": "member",
                                "is_primary": True,
                            }
                        ],
                        "membership_status": self.editor_status,
                    },
                },
            )
        if (
            url.endswith("/auth/me")
            and kwargs["headers"]["Authorization"] == "Bearer editor"
        ):
            return BrowserResponse(
                200,
                {
                    "account_type": "human",
                    "is_admin": False,
                    "user_id": self.editor_subject,
                    "org_id": "owned-workspace",
                },
            )
        return super().get(url, **kwargs)

    def delete(self, url, **kwargs):
        if url.endswith("/hierarchy/member/editor?expected_revision=" + "c" * 64):
            self.editor_status = "revoked"
            return BrowserResponse(200, {})
        return super().delete(url, **kwargs)

    def post(self, url, **kwargs):
        if url.endswith("/members") and kwargs["data"].get("user_id") == "editor":
            self.editor_status = "active"
            return BrowserResponse(201, {"id": "editor", "org_id": "owned-workspace"})
        return super().post(url, **kwargs)


def protected_editor(api, bucket, *, recover=False, connected=False):
    binding = {
        **BINDING,
        **({"scope_fixture_mode": "connected_scope"} if connected else {}),
        "editor_id": "editor",
        "editor_subject": "editor-sub",
        "editor_team_id": "editor-team",
    }
    credentials = {
        name: {
            "username": "eval-regression-" + name if connected and name != "nonmember" else name,
            "password": "synthetic",
            "cognito_user_pool_id": "pool",
            "cognito_client_id": "client",
        }
        for name in ("owner", "nonmember", "editor")
    }
    return BrowserFixtureProvider(
        api,
        ORIGIN,
        {"access_token": "control"},
        state=state(bucket, binding),
        retire=api.retire,
        frontend={"verified": True},
        evidence_context={"plan_revision": SOURCE},
        credentials=credentials,
        recover=recover,
        authenticate=lambda value: {"access_token": value.username.removeprefix("eval-regression-")},
    )


def test_protected_editor_uses_real_ordinary_identity_and_recovers_membership():
    api, bucket = EditorMembershipAPI(), S3()
    selected = protected_editor(api, bucket)
    assert (
        selected.editor["actor_id"] == "editor"
        and selected.editor["tenant_id"] == "owned-workspace"
    )
    assert (
        selected.state.document["scope_editor_membership"]["resource"]["teams"][0][
            "team_id"
        ]
        == "editor-team"
    )
    selected.editor_membership.revoke(selected.state)
    assert selected.state.document["scope_editor_restore_required"] is True
    assert api.editor_status == "revoked"
    resumed = protected_editor(api, bucket, recover=True)
    assert (
        api.editor_status == "active"
        and resumed.state.document["scope_editor_restore_required"] is False
    )
    assert resumed.editor["actor_id"] == "editor"


def test_missing_fresh_scope_inputs_do_not_erase_interrupted_editor_restoration():
    api, bucket = EditorMembershipAPI(), S3()
    selected = protected_editor(api, bucket)
    selected.state.save({**selected.state.document, "scope": {
        "cleanup_pending": True, "intents": {"original": "retained"},
    }})
    selected.editor_membership.revoke(selected.state)
    with pytest.raises(ValueError, match="scope_inputs"):
        preflight_b03({"scope_editor": {"actor_id": "editor"}}, base_url=ORIGIN, environment={})
    assert api.editor_status == "revoked"
    resumed = protected_editor(api, bucket, recover=True)
    assert api.editor_status == "active"
    assert resumed.state.document["scope"]["intents"] == {"original": "retained"}
    assert resumed.state.document["scope_editor_restore_required"] is False
    with pytest.raises(ValueError, match="scope fixture cleanup"):
        resumed.state.release()


@pytest.mark.parametrize(
    "alter,reason",
    [
        ("editor_role", "canonical actor"),
        ("editor_team", "ordinary team baseline"),
        ("editor_subject", "canonical actor"),
    ],
)
def test_protected_editor_refuses_unverified_identity_or_team(alter, reason):
    api, bucket = EditorMembershipAPI(), S3()
    setattr(api, alter, "unverified")
    with pytest.raises(ValueError, match=reason):
        protected_editor(api, bucket)
    assert bucket.objects == {}


def test_recovery_restores_interrupted_owner_role_before_membership_checks():
    api, bucket = EditorMembershipAPI(), S3()
    selected = protected_editor(api, bucket)
    selected.state.save(
        {**selected.state.document, "scope_owner_role_restore_required": True}
    )
    api.owner_role = "org_admin"
    resumed = protected_editor(api, bucket, recover=True)
    assert (
        api.owner_role == "member"
        and resumed.state.document["scope_owner_role_restore_required"] is False
    )


def test_unrecognized_owner_role_does_not_clear_restore_intent():
    api, bucket = EditorMembershipAPI(), S3()
    selected = protected_editor(api, bucket)
    selected.state.save(
        {**selected.state.document, "scope_owner_role_restore_required": True}
    )
    api.owner_role = "platform_admin"
    with pytest.raises(ValueError, match="externally changed Product owner"):
        protected_editor(api, bucket, recover=True)
    assert selected.state.document["scope_owner_role_restore_required"] is True


def test_failed_editor_restoration_preserves_durable_claim():
    api, bucket = EditorMembershipAPI(), S3()
    selected = protected_editor(api, bucket)
    selected.editor_membership.revoke(selected.state)
    api.editor_team = "externally-changed-team"
    with pytest.raises(ValueError, match="externally changed"):
        selected.editor_membership.restore(selected.state)
    assert selected.state.document["scope_editor_restore_required"] is True
    with pytest.raises(ValueError, match="cleanup must finish"):
        selected.state.release()


def test_scope_cleanup_retains_claim_after_editor_restoration():
    api, bucket = EditorMembershipAPI(), S3()
    selected = protected_editor(api, bucket)
    selected.editor_membership.revoke(selected.state)
    selected.editor_membership.restore(selected.state)
    selected.state.save({**selected.state.document, "scope": {"cleanup_pending": True}})
    with pytest.raises(ValueError, match="scope fixture cleanup"):
        selected.state.release()
    with pytest.raises(ValueError, match="scope associations"):
        selected.cleanup("create", api)
    assert selected.state.document["scope"]["cleanup_pending"] is True


def test_connected_scope_verifies_dedicated_members_before_claim_and_recovers():
    api, bucket = EditorMembershipAPI(connected=True), S3()
    selected = protected_editor(api, bucket, connected=True)
    assert selected.editor["actor_id"] == "editor"
    assert selected.state.document["binding"]["scope_fixture_mode"] == "connected_scope"
    selected.state.save({**selected.state.document, "scope": {"cleanup_pending": True, "intents": {"original": "retained"}}})
    selected.editor_membership.revoke(selected.state)
    assert selected.state.document["scope_editor_restore_required"] is True
    resumed = protected_editor(api, bucket, recover=True, connected=True)
    assert api.editor_status == "active"
    assert resumed.state.document["scope_editor_restore_required"] is False
    assert resumed.state.document["scope"]["intents"] == {"original": "retained"}
    with pytest.raises(ValueError, match="scope fixture cleanup"):
        resumed.state.release()


@pytest.mark.parametrize("corruption", ["owner_login", "editor_login", "editor_role", "owner_team", "incomplete_inventory"])
def test_connected_scope_refuses_unverified_members_without_a_claim(corruption):
    api, bucket = EditorMembershipAPI(connected=True), S3()
    if corruption == "owner_login":
        api.owner_login = "shared-owner"
    elif corruption == "editor_login":
        api.editor_login = "shared-editor"
    elif corruption == "editor_role":
        api.editor_role = "org_admin"
    elif corruption == "owner_team":
        api.owner_team = "foreign-team"
    else:
        api.missing_inventory = True
    with pytest.raises(ValueError, match="canonical actor|inventory|ordinary team baseline"):
        protected_editor(api, bucket, connected=True)
    assert bucket.objects == {}
