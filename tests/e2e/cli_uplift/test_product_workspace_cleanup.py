"""A run-owned Product workspace archives after the Product, never a shared org."""

from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift import cleanup, live, ports


RUN_ID = "adp-e2e-20261020-113000-abcdef"
WORKSPACE = RUN_ID + "-product-" + "a" * 24


class WorkspaceAPI:
    def __init__(self):
        self.workspace = {
            "id": WORKSPACE,
            "name": "eval-regression-" + RUN_ID,
            "settings": {},
        }
        self.identity = {
            "verified": True,
            "role": "platform_admin",
            "user_id": "owned-actor",
            "org_id": WORKSPACE,
        }
        self.calls = []
        self.archive_result = 204
        self.archive_changes_state = True

    def get(self, url, *, token, expect):
        assert token == "private-token" and expect is None
        self.calls.append(("GET", url))
        if url.endswith("/auth/cli/admin-session"):
            return 200, self.identity
        if self.workspace is None:
            return 404, None
        return 200, self.workspace

    def request(self, url, *, method, token, expect):
        assert method == "DELETE" and token == "private-token" and expect is None
        self.calls.append((method, url))
        if self.archive_result == 204 and self.archive_changes_state:
            self.workspace["settings"] = {"status": "archived"}
        return self.archive_result, None


@pytest.fixture
def archived(monkeypatch):
    api = WorkspaceAPI()
    cfg = {
        "gateway_url": "https://gateway.example.invalid",
        "region": "us-east-1",
        "platform_account": "000000000000",
    }
    monkeypatch.setattr(live, "_vault_token", lambda *_args: "private-token")
    build = live._deleters(SimpleNamespace(), cfg, http=api, ssm=object())
    session = {
        "prefix": RUN_ID,
        "user_id": "owned-actor",
        "org_id": WORKSPACE,
        "session_ref": "/private/session.json",
    }
    deleters = build(
        None, ctx={"document": {"session": session, "instance_id": "owned-instance"}}
    )
    return api, deleters


@pytest.mark.parametrize(
    "change",
    [
        "valid",
        "absent",
        "foreign_email",
        "foreign_org",
        "duplicate",
        "unconfirmed",
        "wrong_account",
        "wrong_region",
    ],
)
@pytest.mark.parametrize("role", ["user", "admin"])
def test_run_owned_user_intent_is_retired_by_verified_email(monkeypatch, change, role):
    email = RUN_ID + "-product-" + role + "@adp-eval.example"

    class UserAPI:
        def __init__(self):
            self.users = [{"id": "owned-user", "org_id": WORKSPACE, "email": email}]
            self.deleted = []

        def get(self, _url, *, token, expect):
            assert token == "private-token" and expect is None
            return 200, {"users": list(self.users)}

        def request(self, url, *, method, token, expect):
            assert method == "DELETE" and token == "private-token" and expect is None
            self.deleted.append(url)
            if change != "unconfirmed":
                self.users = []
            return 204, None

    api = UserAPI()
    if change == "absent":
        api.users = []
    elif change == "duplicate":
        api.users.append({"id": "different", "org_id": WORKSPACE, "email": email})
    elif change == "foreign_org":
        api.users[0]["org_id"] = "shared-platform"
    cfg = {
        "gateway_url": "https://gateway.example.invalid",
        "region": "us-east-1",
        "platform_account": "000000000000",
    }
    monkeypatch.setattr(live, "_vault_token", lambda *_args: "private-token")
    build = live._deleters(SimpleNamespace(), cfg, http=api, ssm=object())
    deleters = build(
        None,
        ctx={
            "document": {"session": {"prefix": RUN_ID}, "instance_id": "owned-instance"}
        },
    )
    intent = WORKSPACE + "/" + email
    if change == "foreign_email":
        intent = WORKSPACE + "/other@example.invalid"
    if change in {
        "foreign_email",
        "foreign_org",
        "duplicate",
        "unconfirmed",
        "wrong_account",
        "wrong_region",
    }:
        with pytest.raises(ports.PortError, match="Product user"):
            deleters["adp_user_email"](
                intent,
                account="000000000001"
                if change == "wrong_account"
                else cfg["platform_account"],
                region="us-west-2" if change == "wrong_region" else cfg["region"],
            )
    else:
        deleters["adp_user_email"](intent)
    assert len(api.deleted) == (1 if change in {"valid", "unconfirmed"} else 0)
    if change == "valid":
        assert api.deleted[0].endswith("/users/owned-user")


def test_archive_checks_ownership_and_readback_before_reporting_success(archived):
    api, deleters = archived
    deleters["adp_org"](WORKSPACE)
    assert [method for method, _url in api.calls] == ["GET", "GET", "DELETE", "GET"]
    assert api.workspace["settings"]["status"] == "archived"
    deleters["adp_org"](WORKSPACE)
    assert [method for method, _url in api.calls].count("DELETE") == 1


def test_archived_retry_needs_owned_readback_not_deleted_actor(archived):
    api, deleters = archived
    api.workspace["settings"] = {"status": "archived"}
    api.identity = None
    deleters["adp_org"](WORKSPACE)
    assert len(api.calls) == 1
    assert api.calls[0][1].endswith("/" + WORKSPACE)


def test_missing_intended_org_is_safe_after_interrupted_create(archived):
    api, deleters = archived
    api.workspace = None
    deleters["adp_org"](WORKSPACE)
    assert not any(method == "DELETE" for method, _url in api.calls)


@pytest.mark.parametrize(
    "change",
    [
        "foreign_id",
        "foreign_name",
        "wrong_role",
        "wrong_actor",
        "wrong_account",
        "wrong_region",
        "wrong_prefix",
        "failed_receipt",
        "false_receipt",
    ],
)
def test_archive_refuses_foreign_or_unverified_resource(archived, change):
    api, deleters = archived
    identifier, account, region = WORKSPACE, None, None
    if change == "foreign_id":
        identifier = "shared-platform"
    elif change == "foreign_name":
        api.workspace["name"] = "shared-platform"
    elif change == "wrong_role":
        api.identity["role"] = "org_admin"
    elif change == "wrong_actor":
        api.identity["user_id"] = "other-actor"
    elif change == "wrong_account":
        account = "000000000001"
    elif change == "wrong_region":
        region = "other-region"
    elif change == "wrong_prefix":
        identifier = RUN_ID + "-product-short"
    elif change == "failed_receipt":
        api.archive_result = 403
    else:
        api.archive_changes_state = False
    with pytest.raises(ports.PortError, match="workspace|administrator"):
        deleters["adp_org"](identifier, account=account, region=region)
    if change not in {"failed_receipt", "false_receipt"}:
        assert not any(method == "DELETE" for method, _url in api.calls)


def test_manifest_sweeps_workspace_before_deleting_its_actor(tmp_path, archived):
    api, deleters = archived
    manifest = cleanup.Manifest(tmp_path / "owned.json", RUN_ID)
    manifest.record("adp_user", WORKSPACE + "/owned-actor")
    manifest.record("adp_org", WORKSPACE)
    seen = []
    deleters["adp_user"] = lambda _identifier, **_kwargs: seen.append(
        api.workspace["settings"].get("status")
    )
    ok, results = cleanup.sweep(manifest, deleters)
    assert ok and [entry["kind"] for entry in results] == ["adp_org", "adp_user"]
    assert seen == ["archived"]
    assert not manifest.outstanding()


def test_false_archive_receipt_leaves_workspace_outstanding(tmp_path, archived):
    api, deleters = archived
    api.archive_changes_state = False
    manifest = cleanup.Manifest(tmp_path / "owned.json", RUN_ID)
    manifest.record("adp_org", WORKSPACE)
    ok, results = cleanup.sweep(manifest, deleters)
    assert not ok
    assert results[0]["kind"] == "adp_org"
    assert results[0]["status"] == cleanup.FAILED
    assert manifest.outstanding()[0]["id"] == WORKSPACE


def test_pending_product_retirement_preserves_only_its_owned_dependencies(tmp_path):
    account, region = "000000000000", "us-east-1"
    actor = WORKSPACE + "/owned-actor"
    secret = f"adp/cli-uplift-eval/{RUN_ID}-product-fixtures"
    cognito = f"pool/{RUN_ID}-product-admin@adp-eval.example"
    manifest = cleanup.Manifest(tmp_path / "owned.json", RUN_ID)
    owned = [
        ("ec2_instance", "i-owned"),
        ("secret", secret),
        ("cognito_user", cognito),
        ("product_fixture", "product-123456-2"),
        ("adp_org", WORKSPACE),
        ("adp_user", actor),
        ("adp_org", RUN_ID + "-product-" + "b" * 24),
    ]
    for kind, identifier in owned:
        manifest.record(
            kind,
            identifier,
            account=account,
            region=region,
            detail={
                "tenant_id": WORKSPACE,
                "actor_id": "owned-actor",
                "actor_secret_id": secret,
                "actor_cognito_id": cognito,
            }
            if kind == "product_fixture"
            else None,
        )
    manifest.record("adp_connection", "shared-asset", reused=True)
    deleted = []

    def retire(_identifier, **_location):
        raise ports.PortError("protected retirement unavailable")

    deleters = {
        kind: lambda identifier, **_location: deleted.append(identifier)
        for kind, _identifier in owned
    }
    deleters["product_fixture"] = retire
    ok, results = cleanup.sweep(manifest, deleters)
    assert ok is False
    assert deleted == [RUN_ID + "-product-" + "b" * 24]
    assert {row["kind"] for row in results if row["status"] == cleanup.FAILED} == {
        "ec2_instance",
        "secret",
        "cognito_user",
        "product_fixture",
        "adp_org",
        "adp_user",
    }
    assert any(
        entry["kind"] == "adp_connection" and entry["status"] == cleanup.REUSED
        for entry in manifest.read()["resources"]
    )

    manifest.mark("product_fixture", "product-123456-2", cleanup.DELETED)
    ok, results = cleanup.sweep(manifest, deleters)
    assert ok is True
    assert [entry["id"] for entry in manifest.outstanding()] == []
    assert {row["id"] for row in results} == {
        "i-owned",
        secret,
        cognito,
        WORKSPACE,
        actor,
    }
    assert "shared-asset" not in deleted
