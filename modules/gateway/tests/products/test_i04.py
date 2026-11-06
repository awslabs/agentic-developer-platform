"""Installed Product CLI operations against the local gateway API."""

import asyncio
import hashlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path
from urllib import error, parse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.auth.dependencies import get_current_user
from src.cli_download.routes import router as download_router
from src.products.models import ProductChange, ProductOperation, ProductProjectBinding, Project
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member

SCRIPT = Path(__file__).parents[2] / "cli/adp-product.py"

spec = importlib.util.spec_from_file_location("adp_product", SCRIPT)
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


class GatewayClient:
    """Sync CLI boundary using the existing async ASGI test client, not a new service."""

    def __init__(self, test_client, loop):
        self.client = test_client
        self.loop = loop

    def request(self, method, path, body=None, **kwargs):
        response = asyncio.run_coroutine_threadsafe(
            self.client.request(method, path, json=body, headers=kwargs.get("extra_headers")), self.loop
        ).result(timeout=15)
        assert 200 <= response.status_code < 300, response.text
        return response.json()


@pytest.mark.asyncio
async def test_product_commands_round_trip(test_client, member, tmp_path, monkeypatch):
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("Product CLI must not call a provider"))
    client = GatewayClient(test_client, asyncio.get_running_loop())

    async def invoke(*arguments):
        parsed = cli.parser().parse_args(arguments)
        return await asyncio.to_thread(cli.execute, parsed, client)

    created = (
        await invoke(
            "create",
            "--name",
            "Example Planner",
            "--purpose",
            "Coordinate releases",
            "--intended-user",
            "Maintainers",
            "--starting-point",
            "idea",
            "--new-project",
            "--idempotency-key",
            "create_cli_roundtrip_1234",
            "--json",
        )
    )["detail"]
    product_id, project_id = created["product_id"], created["project_id"]
    scope = ("--product", product_id, "--project", project_id)
    listed = (await invoke("list", "--limit", "1", "--json"))["detail"]
    assert listed["items"][0]["id"] == product_id
    shown = (await invoke("show", *scope, "--json"))["detail"]
    assert shown["product"]["name"] == "Example Planner"

    patch = tmp_path / "patch.json"
    patch.write_text('{"name":"Planner v2"}', encoding="utf-8")
    updated = (
        await invoke(
            "update",
            *scope,
            "--input",
            str(patch),
            "--revision",
            "1",
            "--idempotency-key",
            "update_cli_roundtrip_1234",
            "--json",
        )
    )["detail"]
    assert updated["revision"] == 2 and updated["product"]["name"] == "Planner v2"
    first_page = (await invoke("history", *scope, "--limit", "1", "--json"))["detail"]
    assert first_page["items"][0]["revision"] == 1 and first_page["next_cursor"]
    second_page = (await invoke("history", *scope, "--limit", "1", "--cursor", first_page["next_cursor"], "--json"))["detail"]
    assert [item["revision"] for item in second_page["items"]] == [2]
    assert second_page["items"][0]["event_id"] != first_page["items"][0]["event_id"]
    frozen = (await invoke("show", *scope, "--revision", "1", "--json"))["detail"]
    assert frozen["product"]["name"] == "Example Planner" and frozen["revision"] == 1
    archived = (
        await invoke(
            "lifecycle",
            *scope,
            "--action",
            "archive",
            "--reason",
            "Run-owned fixture",
            "--yes",
            "--revision",
            "2",
            "--idempotency-key",
            "archive_cli_roundtrip_1234",
            "--json",
        )
    )["detail"]
    assert archived["revision"] == 3 and "archived_at" in archived
    restored = (
        await invoke(
            "lifecycle",
            *scope,
            "--action",
            "restore",
            "--reason",
            "Run-owned fixture",
            "--yes",
            "--revision",
            "3",
            "--idempotency-key",
            "restore_cli_roundtrip_1234",
            "--json",
        )
    )["detail"]
    assert restored["revision"] == 4 and "archived_at" not in restored
    readback = await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")
    assert readback.json() == restored


def test_clean_install_serves_the_product_helper(tmp_path):
    cli_dir = SCRIPT.parent
    home = tmp_path / "home"
    home.mkdir()
    prefix = tmp_path / "bin"
    result = subprocess.run(
        ["bash", str(cli_dir / "install.sh"), "--prefix", str(prefix), "--gateway-url", "https://gw.test", "--no-path-edit"],
        capture_output=True,
        text=True,
        timeout=120,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert result.returncode == 0, result.stderr
    download_app = FastAPI()
    download_app.include_router(download_router)
    with TestClient(download_app) as client:
        for name in ("adp-product.py", "adp", "command-manifest.json"):
            served = client.get(f"/cli/{name}")
            assert served.status_code == 200
            expected_hash = hashlib.sha256((cli_dir / name).read_bytes()).hexdigest()
            assert hashlib.sha256(served.content).hexdigest() == expected_hash
            assert hashlib.sha256((prefix / name).read_bytes()).hexdigest() == expected_hash
    command = subprocess.run(
        ["bash", str(prefix / "adp"), "product", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert command.returncode == 0, command.stderr
    assert "lifecycle" in command.stdout


class AsgiOpener:
    """Run the real shared HTTP parser against ASGI routes without a listening service."""

    def __init__(self, test_client, loop):
        self.client = test_client
        self.loop = loop
        self.drop_next_patch_response = False

    def open(self, request, timeout):
        url = parse.urlsplit(request.full_url)
        response = asyncio.run_coroutine_threadsafe(
            self.client.request(
                request.get_method(),
                url.path + ("?" + url.query if url.query else ""),
                content=request.data,
                headers=dict(request.header_items()),
            ),
            self.loop,
        ).result(timeout=timeout)
        stream = io.BytesIO(response.content)
        if response.status_code >= 400:
            raise error.HTTPError(request.full_url, response.status_code, response.reason_phrase, response.headers, stream)
        if request.get_method() == "PATCH" and self.drop_next_patch_response:
            self.drop_next_patch_response = False
            raise error.URLError("synthetic response loss after commit")
        stream.status = response.status_code
        stream.headers = response.headers
        return stream


def use_asgi_transport(test_client, monkeypatch):
    api = cli.common.Api.__new__(cli.common.Api)
    api.binding = None
    api.base = "http://test"
    api.opener = AsgiOpener(test_client, asyncio.get_running_loop())
    monkeypatch.setattr(cli.common, "Api", lambda: api)
    monkeypatch.setattr(cli.common, "access_token", lambda: "synthetic-test-token")
    monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *args, **kwargs: None)
    return api


async def invoke_json(capsys, *arguments):
    exit_code = await asyncio.to_thread(cli.main, [*arguments, "--json"])
    output = capsys.readouterr()
    assert "synthetic-test-token" not in output.out + output.err
    return exit_code, json.loads(output.out)


@pytest.mark.parametrize(
    "arguments",
    [
        ("create", "--idempotency-key", "create_incomplete_1234"),
        ("show", "--product", "some-id"),
        ("update", "--product", "some-id", "--project", "some-id", "--input", "missing.json"),
        ("lifecycle", "--product", "some-id", "--project", "some-id", "--action", "archive", "--reason", "reason"),
        ("list", "--limit", "101"),
        ("project", "list", "--limit", "101"),
        ("project", "bind", "--product", "some-id", "--revision", "1", "--idempotency-key", "missing_project_input_001"),
        (
            "project",
            "unbind",
            "--product",
            "some-id",
            "--project",
            "scope-id",
            "--revision",
            "1",
            "--idempotency-key",
            "missing_unbind_confirm_001",
        ),
    ],
)
def test_missing_input_never_prompts_or_calls_api(arguments, capsys, monkeypatch):
    class ForbiddenApi:
        def request(self, *args, **kwargs):
            pytest.fail("Invalid input must not call the gateway")

    monkeypatch.setattr(cli.common, "Api", ForbiddenApi)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("Noninteractive CLI must not prompt"))
    exit_code = cli.main([*arguments, "--json"])
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1 and result["error"]["code"] == "usage_error"
    assert result["status"] == "failed" and result["detail"] == {}


@pytest.mark.asyncio
async def test_server_validation_error_does_not_write(test_client, db_session, member, tmp_path, monkeypatch, capsys):
    use_asgi_transport(test_client, monkeypatch)
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text('{"name":"Only a name"}', encoding="utf-8")
    exit_code, result = await invoke_json(
        capsys,
        "create",
        "--input",
        str(incomplete),
        "--idempotency-key",
        "validation_case_12345",
    )
    assert exit_code == 1 and result["error"]["code"] == "validation_error"
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 0
    assert (await test_client.get("/v1/products")).json()["items"] == []


@pytest.mark.asyncio
async def test_stale_revision_reports_conflict_without_write(test_client, db_session, member, tmp_path, monkeypatch, capsys):
    use_asgi_transport(test_client, monkeypatch)
    exit_code, created = await invoke_json(
        capsys,
        "create",
        "--name",
        "Example Planner",
        "--purpose",
        "Coordinate releases",
        "--intended-user",
        "Maintainers",
        "--starting-point",
        "idea",
        "--new-project",
        "--idempotency-key",
        "create_stale_case_12345",
    )
    assert exit_code == 0
    product_id, project_id = created["detail"]["product_id"], created["detail"]["project_id"]
    scope = ("--product", product_id, "--project", project_id)
    patch = tmp_path / "patch.json"
    patch.write_text('{"name":"Planner v2"}', encoding="utf-8")
    exit_code, updated = await invoke_json(
        capsys,
        "update",
        *scope,
        "--input",
        str(patch),
        "--revision",
        "1",
        "--idempotency-key",
        "first_stale_case_12345",
    )
    assert exit_code == 0 and updated["detail"]["revision"] == 2
    patch.write_text('{"name":"Incorrect stale change"}', encoding="utf-8")
    exit_code, refused = await invoke_json(
        capsys,
        "update",
        *scope,
        "--input",
        str(patch),
        "--revision",
        "1",
        "--idempotency-key",
        "second_stale_case_12345",
    )
    assert exit_code == 4 and refused["error"]["code"] == "stale_revision"
    assert product_id not in refused["error"]["message"]
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2
    readback = await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")
    assert readback.json()["product"]["name"] == "Planner v2"


@pytest.mark.asyncio
async def test_foreign_scope_does_not_disclose_or_write(test_client, db_session, app, member, tmp_path, monkeypatch, capsys):
    use_asgi_transport(test_client, monkeypatch)
    exit_code, created = await invoke_json(
        capsys,
        "create",
        "--name",
        "Example Planner",
        "--purpose",
        "Coordinate releases",
        "--intended-user",
        "Maintainers",
        "--starting-point",
        "idea",
        "--new-project",
        "--idempotency-key",
        "create_foreign_case_12345",
    )
    assert exit_code == 0
    product_id, project_id = created["detail"]["product_id"], created["detail"]["project_id"]
    app.dependency_overrides[get_current_user] = lambda: member.model_copy(update={"org_id": "foreign-workspace"})
    patch = tmp_path / "patch.json"
    patch.write_text('{"name":"Unauthorized edit"}', encoding="utf-8")
    for arguments in (
        ("show", "--product", product_id, "--project", project_id),
        (
            "update",
            "--product",
            product_id,
            "--project",
            project_id,
            "--input",
            str(patch),
            "--revision",
            "1",
            "--idempotency-key",
            "foreign_update_case_12345",
        ),
    ):
        exit_code, refused = await invoke_json(capsys, *arguments)
        assert exit_code == 5 and refused["error"]["code"] == "not_found"
        assert product_id not in refused["error"]["message"]
    exit_code, denied = await invoke_json(capsys, "list")
    assert exit_code == 3 and denied["error"]["code"] == "access_denied"
    assert product_id not in denied["error"]["message"]
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 1
    app.dependency_overrides[get_current_user] = lambda: member
    readback = await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")
    assert readback.json()["revision"] == 1


@pytest.mark.asyncio
async def test_lost_update_response_replays_once(test_client, db_session, member, tmp_path, monkeypatch, capsys):
    api = use_asgi_transport(test_client, monkeypatch)
    exit_code, created = await invoke_json(
        capsys,
        "create",
        "--name",
        "Example Planner",
        "--purpose",
        "Coordinate releases",
        "--intended-user",
        "Maintainers",
        "--starting-point",
        "idea",
        "--new-project",
        "--idempotency-key",
        "create_retry_case_12345",
    )
    assert exit_code == 0
    product_id, project_id = created["detail"]["product_id"], created["detail"]["project_id"]
    patch = tmp_path / "patch.json"
    patch.write_text('{"name":"Planner v2"}', encoding="utf-8")
    arguments = (
        "update",
        "--product",
        product_id,
        "--project",
        project_id,
        "--input",
        str(patch),
        "--revision",
        "1",
        "--idempotency-key",
        "same_retry_key_12345",
    )
    api.opener.drop_next_patch_response = True
    exit_code, uncertain = await invoke_json(capsys, *arguments)
    assert exit_code == 4 and uncertain["error"]["code"] == "unknown_mutation_outcome"
    assert "unchanged --idempotency-key" in uncertain["error"]["message"]
    readback = await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")
    assert readback.json()["revision"] == 2 and readback.json()["product"]["name"] == "Planner v2"

    exit_code, replay = await invoke_json(capsys, *arguments)
    assert exit_code == 0 and replay["status"] == "ok"
    assert replay["detail"] == readback.json()
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2

    patch.write_text('{"name":"Different body under old key"}', encoding="utf-8")
    exit_code, conflict = await invoke_json(capsys, *arguments)
    assert exit_code == 5 and conflict["error"]["code"] == "idempotency_conflict"
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2


def test_existing_cli_version_and_product_json_remain_separate(tmp_path, monkeypatch, capsys):
    class FakeApi:
        def request(self, method, path, body=None, **kwargs):
            assert method == "GET" and path == "/v1/products?limit=100"
            return {"items": [], "next_cursor": None}

    monkeypatch.setattr(cli.common, "Api", FakeApi)
    assert cli.main(["list", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "ok" and parsed["detail"]["items"] == []
    assert "synthetic-test-token" not in json.dumps(parsed)
    legacy = subprocess.run(
        ["bash", str(SCRIPT.parent / "adp"), "version"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert legacy.returncode == 0 and legacy.stdout.strip() == "adp 1.0.0"
    assert "synthetic-test-token" not in legacy.stdout + legacy.stderr


@pytest.mark.asyncio
async def cli_install_and_create(test_client, db_session, member, tmp_path, monkeypatch, capsys):
    from datetime import UTC, datetime

    cli_dir = SCRIPT.parent
    home = tmp_path / "home"
    home.mkdir()
    prefix = tmp_path / "bin"
    installed = subprocess.run(
        ["bash", str(cli_dir / "install.sh"), "--prefix", str(prefix), "--gateway-url", "https://gw.test", "--no-path-edit"],
        capture_output=True,
        text=True,
        timeout=120,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert installed.returncode == 0, installed.stderr
    download_app = FastAPI()
    download_app.include_router(download_router)
    with TestClient(download_app) as downloader:
        for name in ("adp", "adp-product.py", "adp_common.py", "command-manifest.json"):
            served = downloader.get(f"/cli/{name}")
            assert served.status_code == 200
            expected_hash = hashlib.sha256((cli_dir / name).read_bytes()).hexdigest()
            assert hashlib.sha256(served.content).hexdigest() == expected_hash
            assert hashlib.sha256((prefix / name).read_bytes()).hexdigest() == expected_hash

    installed_spec = importlib.util.spec_from_file_location("installed_product", prefix / "adp-product.py")
    installed_cli = importlib.util.module_from_spec(installed_spec)
    installed_spec.loader.exec_module(installed_cli)
    assert installed_cli.common is cli.common
    use_asgi_transport(test_client, monkeypatch)
    observed_at = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: observed_at)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("Product CLI must not call a provider"))

    async def command(*arguments):
        exit_code = await asyncio.to_thread(installed_cli.main, [*arguments, "--json"])
        output = capsys.readouterr()
        assert exit_code == 0 and "synthetic-test-token" not in output.out + output.err
        envelope = json.loads(output.out)
        assert envelope["status"] == "ok" and envelope["command"] == "product " + arguments[0]
        return envelope["detail"]

    created = await command(
        "create",
        "--name",
        "Example Planner",
        "--purpose",
        "Coordinate releases",
        "--intended-user",
        "Maintainers",
        "--starting-point",
        "idea",
        "--new-project",
        "--idempotency-key",
        "install_create_case_12345",
    )
    product_id, project_id = created["product_id"], created["project_id"]
    scope = ("--product", product_id, "--project", project_id)
    url = f"/v1/products/{product_id}/projects/{project_id}"
    assert created["revision"] == 1 and (await test_client.get(url)).json() == created
    listed = await command("list", "--limit", "1")
    assert listed["items"][0]["id"] == product_id
    shown = await command("show", *scope)
    assert shown == created
    patch = tmp_path / "patch.json"
    patch.write_text('{"purpose":"Coordinate deployments"}', encoding="utf-8")
    updated = await command(
        "update",
        *scope,
        "--input",
        str(patch),
        "--revision",
        "1",
        "--idempotency-key",
        "install_update_case_12345",
    )
    assert updated["revision"] == 2 and (await test_client.get(url)).json() == updated
    history = await command("history", *scope, "--limit", "1")
    assert [change["revision"] for change in history["items"]] == [1]
    assert history["next_cursor"]
    second_page = await command("history", *scope, "--limit", "1", "--cursor", history["next_cursor"])
    assert [change["revision"] for change in second_page["items"]] == [2]
    frozen = await command("show", *scope, "--revision", "1")
    assert frozen["product"]["purpose"] == "Coordinate releases"
    archived = await command(
        "lifecycle",
        *scope,
        "--action",
        "archive",
        "--reason",
        "Run-owned fixture",
        "--yes",
        "--revision",
        "2",
        "--idempotency-key",
        "install_archive_case_12345",
    )
    assert archived["revision"] == 3 and "archived_at" in archived
    assert (await test_client.get(url)).json() == archived
    restored = await command(
        "lifecycle",
        *scope,
        "--action",
        "restore",
        "--reason",
        "Run-owned fixture",
        "--yes",
        "--revision",
        "3",
        "--idempotency-key",
        "install_restore_case_12345",
    )
    assert restored["revision"] == 4 and "archived_at" not in restored
    assert (await test_client.get(url)).json() == restored
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 4


@pytest.mark.asyncio
async def cli_errors(test_client, db_session, app, member, tmp_path, monkeypatch, capsys):
    from datetime import UTC, datetime

    use_asgi_transport(test_client, monkeypatch)
    observed_at = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: observed_at)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("Product commands must not prompt"))
    exit_code, incomplete = await invoke_json(capsys, "create", "--idempotency-key", "missing_fields_case_12345")
    assert exit_code == 1 and incomplete["error"]["code"] == "usage_error"
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 0

    invalid_body = tmp_path / "invalid.json"
    invalid_body.write_text('{"name":"Only a name"}', encoding="utf-8")
    exit_code, invalid = await invoke_json(
        capsys,
        "create",
        "--input",
        str(invalid_body),
        "--idempotency-key",
        "invalid_body_case_12345",
    )
    assert exit_code == 1 and invalid["error"]["code"] == "validation_error"
    assert (await test_client.get("/v1/products")).json()["items"] == []

    exit_code, created = await invoke_json(
        capsys,
        "create",
        "--name",
        "Example Planner",
        "--purpose",
        "Coordinate releases",
        "--intended-user",
        "Maintainers",
        "--starting-point",
        "idea",
        "--new-project",
        "--idempotency-key",
        "errors_create_case_12345",
    )
    assert exit_code == 0
    product_id, project_id = created["detail"]["product_id"], created["detail"]["project_id"]
    url = f"/v1/products/{product_id}/projects/{project_id}"
    scope = ("--product", product_id, "--project", project_id)
    patch = tmp_path / "patch.json"
    patch.write_text('{"name":"Planner v2"}', encoding="utf-8")
    exit_code, updated = await invoke_json(
        capsys,
        "update",
        *scope,
        "--input",
        str(patch),
        "--revision",
        "1",
        "--idempotency-key",
        "errors_update_case_12345",
    )
    assert exit_code == 0 and updated["detail"]["revision"] == 2
    expected_profile = (await test_client.get(url)).json()
    assert expected_profile == updated["detail"]

    patch.write_text('{"name":"Stale change"}', encoding="utf-8")
    exit_code, stale = await invoke_json(
        capsys,
        "update",
        *scope,
        "--input",
        str(patch),
        "--revision",
        "1",
        "--idempotency-key",
        "errors_stale_case_12345",
    )
    assert exit_code == 4 and stale["error"]["code"] == "stale_revision"
    assert (await test_client.get(url)).json() == expected_profile

    app.dependency_overrides[get_current_user] = lambda: member.model_copy(update={"org_id": "other-workspace"})
    for arguments in (
        ("show", *scope),
        ("update", *scope, "--input", str(patch), "--revision", "2", "--idempotency-key", "errors_foreign_case_12345"),
    ):
        exit_code, refused = await invoke_json(capsys, *arguments)
        assert exit_code == 5 and refused["error"]["code"] == "not_found"
        assert product_id not in refused["error"]["message"]
    exit_code, denied = await invoke_json(capsys, "list")
    assert exit_code == 3 and denied["error"]["code"] == "access_denied"
    assert product_id not in denied["error"]["message"]

    app.dependency_overrides[get_current_user] = lambda: member
    assert (await test_client.get(url)).json() == expected_profile
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2


@pytest.mark.asyncio
async def cli_retry_compatibility(test_client, db_session, member, tmp_path, monkeypatch, capsys, caplog):
    from datetime import UTC, datetime

    home = tmp_path / "home"
    home.mkdir()
    prefix = tmp_path / "bin"
    installed = subprocess.run(
        ["bash", str(SCRIPT.parent / "install.sh"), "--prefix", str(prefix), "--gateway-url", "https://gw.test", "--no-path-edit"],
        capture_output=True,
        text=True,
        timeout=120,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert installed.returncode == 0, installed.stderr
    installed_spec = importlib.util.spec_from_file_location("retry_installed_product", prefix / "adp-product.py")
    installed_cli = importlib.util.module_from_spec(installed_spec)
    installed_spec.loader.exec_module(installed_cli)
    api = use_asgi_transport(test_client, monkeypatch)
    observed_at = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: observed_at)

    async def command(*arguments):
        exit_code = await asyncio.to_thread(installed_cli.main, [*arguments, "--json"])
        output = capsys.readouterr()
        assert "synthetic-test-token" not in output.out + output.err
        return exit_code, json.loads(output.out)

    exit_code, created = await command(
        "create",
        "--name",
        "Example Planner",
        "--purpose",
        "Coordinate releases",
        "--intended-user",
        "Maintainers",
        "--starting-point",
        "idea",
        "--new-project",
        "--idempotency-key",
        "retry_create_case_12345",
    )
    assert exit_code == 0
    product_id, project_id = created["detail"]["product_id"], created["detail"]["project_id"]
    patch = tmp_path / "patch.json"
    patch.write_text('{"name":"Planner v2"}', encoding="utf-8")
    arguments = (
        "update",
        "--product",
        product_id,
        "--project",
        project_id,
        "--input",
        str(patch),
        "--revision",
        "1",
        "--idempotency-key",
        "retry_update_case_12345",
    )
    api.opener.drop_next_patch_response = True
    exit_code, unknown = await command(*arguments)
    assert exit_code == 4 and unknown["error"]["code"] == "unknown_mutation_outcome"
    assert "unchanged --idempotency-key" in unknown["error"]["message"]
    independent = (await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")).json()
    assert independent["revision"] == 2 and independent["product"]["name"] == "Planner v2"

    exit_code, replay = await command(*arguments)
    assert exit_code == 0 and replay["status"] == "ok" and replay["detail"] == independent
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2

    legacy = subprocess.run(
        ["bash", str(prefix / "adp"), "version"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert legacy.returncode == 0 and legacy.stdout.strip() == "adp 1.0.0"
    assert "synthetic-test-token" not in legacy.stdout + legacy.stderr + caplog.text


@pytest.mark.parametrize("value", ["governed:project.example", "p" * 200])
def test_project_ids_use_the_gateway_identifier_contract(value):
    assert cli.identifier(value) == parse.quote(value, safe="")


@pytest.mark.parametrize("value", ["/foreign", "../foreign", ".", "..", " " + "p", "p" * 256])
def test_project_ids_reject_ambiguous_path_segments(value):
    with pytest.raises(cli.common.CliError):
        cli.identifier(value)


@pytest.mark.asyncio
async def test_project_commands_use_scoped_gateway_ids(test_client, db_session, member, tmp_path):
    client = GatewayClient(test_client, asyncio.get_running_loop())

    async def invoke(*arguments):
        return (await asyncio.to_thread(cli.execute, cli.parser().parse_args(arguments), client))["detail"]

    created = await test_client.post(
        "/v1/products",
        json={
            "name": "Planner",
            "purpose": "Plan releases",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "create_cli_scope_owner_001"},
    )
    assert created.status_code == 201, created.text
    product_id, first_id = created.json()["product_id"], created.json()["project_id"]
    scope = ("project",)
    assert first_id in {item["id"] for item in (await invoke(*scope, "list", "--limit", "1"))["items"]}
    body = tmp_path / "new-project.json"
    body.write_text('{"mode":"create","name":"Beta"}', encoding="utf-8")
    new = await invoke(
        *scope,
        "bind",
        "--product",
        product_id,
        "--input",
        str(body),
        "--revision",
        "1",
        "--idempotency-key",
        "cli_create_second_scope_001",
    )
    second_id = new["project_id"]
    assert second_id != first_id and new["project"]["name"] == "Beta" and new["product"]["name"] == "Planner"
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{second_id}")).json()["project_id"] == second_id
    history = await invoke(*scope, "bindings", "--product", product_id, "--project", second_id)
    version = history["items"][-1]["relationship_versions"][0]
    binding_id = version["relationship_id"]
    assert version["to_ref"]["record_id"] == version["to_ref"]["project_id"] == second_id
    assert version["from_ref"]["record_id"] == product_id
    binding = await db_session.scalar(select(ProductProjectBinding).where(ProductProjectBinding.project_id == second_id))
    assert binding_id == binding.id
    existing_id = "existing-owned-project"
    db_session.add(Project(id=existing_id, tenant_id=member.org_id, name="Existing", owner_user_id=member.user_id))
    await db_session.commit()
    body.write_text('{"mode":"select","project_id":"existing-owned-project"}', encoding="utf-8")
    linked = await invoke(
        *scope,
        "bind",
        "--product",
        product_id,
        "--input",
        str(body),
        "--revision",
        "2",
        "--idempotency-key",
        "cli_link_existing_scope_001",
    )
    assert linked["project_id"] == existing_id and linked["revision"] == 3
    body.write_text('{"configuration":{"governance":{"criticality":"low"}}}', encoding="utf-8")
    updated = await invoke(
        *scope,
        "update",
        "--product",
        product_id,
        "--project",
        existing_id,
        "--input",
        str(body),
        "--revision",
        "3",
        "--idempotency-key",
        "cli_edit_existing_scope_001",
    )
    assert updated["configuration"]["governance"]["criticality"] == "low"
    remaining = await invoke(
        *scope,
        "unbind",
        "--product",
        product_id,
        "--project",
        existing_id,
        "--yes",
        "--revision",
        "4",
        "--idempotency-key",
        "cli_unlink_existing_scope_001",
    )
    assert remaining["project_id"] in {first_id, second_id}
    saved = await db_session.get(Project, (member.org_id, existing_id))
    assert saved.configuration == {"governance": {"criticality": "low"}}
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{existing_id}")).status_code == 404
