"""Browser and installed CLI resume the same gateway-backed session."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from src.auth.dependencies import get_current_user
from src.products.models import ProductOnboardingSession
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member
from tests.products.product_resume import assert_resume
from tests.products.test_i01 import HEADERS, REQUEST
from tests.products.test_i04 import GatewayClient

SCRIPT = Path(__file__).parents[2] / "cli/adp-onboard.py"
spec = importlib.util.spec_from_file_location("adp_onboard", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture
async def member(db_session, app, valid_token_context):
    db_session.add(Organization(id=valid_token_context.org_id, name="Test workspace"))
    db_session.add(
        User(
            id=valid_token_context.user_id,
            org_id=valid_token_context.org_id,
            team_id=valid_token_context.team_id,
            email="member@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, valid_token_context)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: valid_token_context
    return valid_token_context


@pytest.mark.asyncio
async def test_cli_resumes_ui_session_through_gateway(test_client, db_session, member, tmp_path):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    root = f"/v1/products/{product_id}/projects/{project_id}/onboarding-sessions"
    browser = await test_client.post(
        root,
        json={},
        headers={
            "Idempotency-Key": "browser_onboarding_123",
            "If-Profile-Revision": "1",
        },
    )
    assert browser.status_code == 201, browser.text
    gateway = GatewayClient(test_client, asyncio.get_running_loop())

    async def invoke(*arguments):
        return await asyncio.to_thread(cli.execute, cli.parser().parse_args(arguments), gateway)

    scope = ("--product", product_id, "--project", project_id)
    resumed = (await invoke("resume", *scope, "--revision", "1", "--idempotency-key", "cli_resume_request_123", "--json"))["detail"]
    assert_resume(
        browser.json(), resumed, (await test_client.get(f"{root}/{browser.json()['id']}")).json(), product_id=product_id, project_id=project_id
    )
    directly = (await invoke("resume", *scope, "--session", resumed["id"], "--json"))["detail"]
    assert directly == resumed
    update = tmp_path / "step.json"
    update.write_text(json.dumps({"action": "later", "reason": "Awaiting review"}), encoding="utf-8")
    saved = (
        await invoke(
            "step",
            *scope,
            "--session",
            resumed["id"],
            "--section",
            "financials",
            "--input",
            str(update),
            "--revision",
            "1",
            "--idempotency-key",
            "cli_step_request_123",
            "--json",
        )
    )["detail"]
    assert saved["revision"] == 2 and saved["steps"]["financials"]["reason"] == "Awaiting review"
    browser_reload = await test_client.get(f"{root}/{resumed['id']}")
    assert browser_reload.status_code == 200
    assert_resume(
        saved, browser_reload.json(), (await test_client.get(f"{root}/{resumed['id']}")).json(), product_id=product_id, project_id=project_id
    )
    db_session.expire_all()
    assert (await db_session.get(ProductOnboardingSession, (member.org_id, resumed["id"]))).steps["financials"]["status"] == "later"
    assert (await invoke("resume", *scope, "--session", resumed["id"], "--json"))["detail"] == saved


def test_resume_rejects_changed_scope_revision_or_choices():
    session = {
        "id": "owned-session",
        "tenant_id": "tenant",
        "product_id": "product",
        "project_id": "project",
        "revision": 2,
        "resume_path": "/products/product/projects/project/onboarding-sessions/owned-session",
        "steps": {"financials": {"status": "later"}},
    }
    assert_resume(session, session.copy(), session.copy(), product_id="product", project_id="project")
    for difference in (
        {"id": "duplicate-session"},
        {"revision": 1},
        {"project_id": "foreign-project"},
        {"steps": {"financials": {"status": "not_started"}}},
    ):
        with pytest.raises(AssertionError):
            assert_resume(session, {**session, **difference}, session, product_id="product", project_id="project")


def test_cli_rejects_missing_write_preconditions_and_unsafe_ids():
    parsed = cli.parser().parse_args(["resume", "--product", "x", "--project", "y"])
    with pytest.raises(cli.common.CliError, match="revision"):
        cli.execute(parsed, object())
    parsed = cli.parser().parse_args(["resume", "--product", "..", "--project", "y", "--session", "s"])
    with pytest.raises(cli.common.CliError, match="exact IDs"):
        cli.execute(parsed, object())


def test_cli_is_delivered_by_both_manifests_and_download_allowlist():
    import re

    from src.cli_download.routes import ALLOWED_SCRIPTS, SCRIPT_MEDIA_TYPES

    for name in ("adp", "install.sh"):
        content = (SCRIPT.parent / name).read_text()
        manifest = re.search(r'CLI_FILES="([^"]+)"', content)
        assert manifest and "adp-onboard.py" in manifest.group(1).split()
    assert ALLOWED_SCRIPTS["adp-onboard.py"] == SCRIPT.resolve()
    assert SCRIPT_MEDIA_TYPES["adp-onboard.py"] == "text/x-python"
    manifest = json.loads((SCRIPT.parent / "command-manifest.json").read_text())
    commands = {item["command"] for item in manifest["commands"] if item["helper"] == "adp-onboard.py"}
    assert commands == {"adp onboard start", "adp onboard resume", "adp onboard step", "adp onboard guide", "adp onboard retry", "adp onboard unlink"}


def test_cli_recovery_uses_server_selected_bindings_and_refuses_denial(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []
            self.session = {
                "id": "saved",
                "revision": 2,
                "discoveries": [{"id": "page", "cursor": "page", "binding_ids": ["selected"], "adapter": "native", "coverage": "partial"}],
            }

        def request(self, method, path, body=None, **kwargs):
            self.calls.append((method, path, body, kwargs.get("extra_headers")))
            return self.session

    monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *a, **kw: None)
    client = Client()
    base = ["--product", "product", "--project", "project", "--session", "saved", "--revision", "2", "--idempotency-key", "recovery_request_123"]
    result = cli.execute(cli.parser().parse_args(["retry", *base, "--operation", "page"]), client)
    assert result["detail"] == client.session
    assert client.calls[1][0] == "POST"
    assert client.calls[1][2] == {"binding_ids": ["selected"], "cursor": "page", "adapter": "native"}
    assert client.calls[1][3] == {"Idempotency-Key": "recovery_request_123", "If-Profile-Revision": "2"}
    client.session["discoveries"][0] = {"id": "page", "coverage": "denied", "complete": False}
    with pytest.raises(cli.common.CliError, match="authorized, resumable"):
        cli.execute(cli.parser().parse_args(["retry", *base, "--operation", "page"]), client)
    assert [call[0] for call in client.calls].count("POST") == 1
    cli.execute(cli.parser().parse_args(["unlink", *base, "--binding", "selected"]), client)
    assert client.calls[-2][0:2] == ("DELETE", "/v1/products/product/projects/project/bindings/selected")
    assert client.calls[-1][0] == "GET"


def test_cli_optional_connector_retry_requires_positive_feature_discovery(monkeypatch):
    class Client:
        def __init__(self, flags):
            self.calls = []
            self.flags = flags

        def request(self, method, path, body=None, **kwargs):
            self.calls.append((method, path))
            if path == "/features":
                if self.flags is None:
                    raise cli.common.CliError("Old server", "not_found", 404)
                return {"features": self.flags}
            return {
                "id": "saved",
                "revision": 2,
                "discoveries": [{"id": "page", "coverage": "partial", "cursor": "page", "adapter": "devlake", "binding_ids": ["binding"]}],
            }

    monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *a, **kw: None)
    args = cli.parser().parse_args(
        [
            "retry",
            "--product",
            "product",
            "--project",
            "project",
            "--session",
            "saved",
            "--revision",
            "2",
            "--idempotency-key",
            "optional_retry_123",
            "--operation",
            "page",
        ]
    )
    for flags in (None, {}, {"devlake_connectors": False}):
        client = Client(flags)
        with pytest.raises(cli.common.CliError, match="Optional connectors are not available"):
            cli.execute(args, client)
        assert [method for method, _ in client.calls] == ["GET", "GET"]
    client = Client({"devlake_connectors": True})
    cli.execute(args, client)
    assert [method for method, _ in client.calls] == ["GET", "GET", "POST", "GET"]
