"""Product workspaces require a durable intent and verified platform-admin creation."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift import bundle, cleanup, contracts, live, ports

sys.path.insert(0, str(Path(__file__).resolve().parent / "remote"))
import common
import product_workspace


RUN_ID = "adp-e2e-20261020-113000-abcdef"
WORKSPACE = RUN_ID + "-product-" + "a" * 24
URL = "https://gateway.example.invalid"
TEAMS_ROUTE = "/admin/organizations/{org_id}/teams"
TEAMS_PATH = "/admin/organizations/" + WORKSPACE + "/teams?page=1&page_size=100"


@pytest.fixture
def config():
    return {
        "evaluation_id": RUN_ID,
        "product_workspace_id": WORKSPACE,
        "product_cases": "api_product_lifecycle",
        "instance_id": "owned-instance",
        "work_dir": bundle.REMOTE_DIR,
        "session_ref": bundle.REMOTE_DIR + "/session.json",
        "gateway_url": URL,
        "platform_account": "000000000000",
        "region": "us-east-1",
    }


@pytest.fixture
def gateway(monkeypatch):
    owner = {
        "access_token": "private-bootstrap-token",
        "user_id": "bootstrap-actor",
        "org_id": "shared-bootstrap",
    }
    identity = {
        "verified": True,
        "role": "platform_admin",
        "user_id": owner["user_id"],
        "org_id": owner["org_id"],
    }
    state = {
        "created": False,
        "users": [],
        "team": {"id": "actual-team", "org_id": WORKSPACE},
        "response_lost": False,
    }
    calls = []
    monkeypatch.setattr(product_workspace.common, "load_session", lambda _cfg: owner)

    def api(_cfg, path, token, *, method="GET", body=None, expect=(200,)):
        assert token == owner["access_token"]
        calls.append((method, path, body))
        org = "/api/admin/identity/organizations/" + WORKSPACE
        if path == "/auth/cli/admin-session":
            return 200, identity
        if path == org:
            return (
                (
                    200,
                    {
                        "id": WORKSPACE,
                        "name": "eval-regression-" + RUN_ID,
                        "settings": {},
                    },
                )
                if state["created"]
                else (404, None)
            )
        if path == org + "/users":
            return 200, {"users": state["users"]}
        # The teams collection is NOT served under the identity prefix, and it
        # carries no `/api` segment either: matching the complete path exactly
        # here is what keeps a wrong prefix (which answers HTTP 404 live)
        # failing in this suite instead of passing on a suffix match. The path
        # itself is held to the app's real route table by the assertions in
        # test_canonical_workspace_created_and_read_back, so this double cannot
        # keep agreeing with a guess.
        if path == TEAMS_PATH:
            return 200, {
                "page": 1,
                "page_size": 100,
                "total": 1,
                "has_more": False,
                "items": [state["team"]],
            }
        assert path == "/api/admin/identity/organizations" and method == "POST"
        assert body == {"id": WORKSPACE, "name": "eval-regression-" + RUN_ID}
        state["created"] = True
        if state["response_lost"]:
            raise common.RemoteError("Product create response lost")
        return 201, {"id": WORKSPACE, "name": "eval-regression-" + RUN_ID}

    monkeypatch.setattr(common, "api", api)
    return owner, identity, state, calls


def test_canonical_workspace_created_and_read_back(config, gateway):
    _owner, _identity, _state, calls = gateway
    evidence = {}
    product_workspace.execute(config, evidence)
    assert evidence == {
        "success": True,
        "stage": "complete",
        "workspace_id": WORKSPACE,
        "team_id": "actual-team",
    }
    assert [method for method, _path, _body in calls] == [
        "GET",
        "GET",
        "POST",
        "GET",
        "GET",
        "GET",
    ]
    assert calls[2][1] == "/api/admin/identity/organizations"
    # The complete routes, prefix included: the workspace and its users are
    # identity-scoped, the team inventory is not.
    assert [path for _method, path, _body in calls if "/teams" in path] == [TEAMS_PATH]
    assert (
        calls[1][1] == calls[3][1] == "/api/admin/identity/organizations/" + WORKSPACE
    )
    assert calls[4][1] == "/api/admin/identity/organizations/" + WORKSPACE + "/users"
    # Derived from the gateway's own routers rather than restated here: the team
    # inventory is a route of the `/admin` router, every other admin request
    # belongs to the `/api/admin/identity` router, and a path no router mounts
    # is the 404 this suite exists to catch before a deployment finds it.
    mounted = contracts.admin_routes()
    assert contracts.mounted_route("GET", TEAMS_PATH, mounted) == TEAMS_ROUTE
    for method, path, _body in calls:
        if path.startswith(("/admin/", "/api/admin")):
            assert contracts.mounted_route(method, path, mounted), path


@pytest.mark.parametrize(
    "failure", ["existing", "admin", "owner", "users", "team", "lost", "id"]
)
def test_refuses_unsafe_workspace_or_unknown_create_response(config, gateway, failure):
    owner, identity, state, calls = gateway
    if failure == "existing":
        state["created"] = True
    elif failure == "admin":
        identity["role"] = "org_admin"
    elif failure == "owner":
        owner["org_id"] = WORKSPACE
    elif failure == "users":
        state["users"] = [{"id": "other-user"}]
    elif failure == "team":
        state["team"] = {"id": "foreign-team", "org_id": "other-org"}
    elif failure == "lost":
        state["response_lost"] = True
    else:
        config["product_workspace_id"] = "shared-platform"
    with pytest.raises(common.RemoteError):
        product_workspace.execute(config, {})
    assert sum(method == "POST" for method, _path, _body in calls) == (
        0 if failure in {"existing", "admin", "owner", "id"} else 1
    )
    if failure == "lost":
        with pytest.raises(common.RemoteError):
            product_workspace.execute(config, {})
        assert sum(method == "POST" for method, _path, _body in calls) == 1


def test_caller_records_ownership_durably_before_remote_dispatch(config, tmp_path):
    events = []
    manifest = cleanup.Manifest(
        tmp_path / "intent.json",
        RUN_ID,
        on_change=lambda _document, *, critical: events.append(("persist", critical)),
    )
    manifest.record(
        "ec2_instance",
        "owned-instance",
        account=config["platform_account"],
        region=config["region"],
    )
    events.clear()

    class Ssm:
        def json_result(self, _instance, _commands, *, purpose, timeout):
            events.append(("dispatch", purpose))
            return {"Status": "Success"}, {"success": True}

    def install(_instance, _run_id):
        events.append(("install", _run_id))

    runner = live._run_worker(Ssm(), config, install)
    assert runner("owned-instance", "product_workspace", config, manifest=manifest) == {
        "success": True
    }
    assert events == [
        ("persist", True),
        ("install", RUN_ID),
        ("dispatch", "product_workspace"),
    ]
    assert [(entry["kind"], entry["id"]) for entry in manifest.outstanding()] == [
        ("ec2_instance", "owned-instance"),
        ("adp_org", WORKSPACE),
    ]


@pytest.mark.parametrize(
    "bad", ["manifest", "workspace", "selection", "session", "origin", "instance"]
)
def test_caller_refuses_bad_dispatch_before_mutation(config, tmp_path, bad):
    sent = []
    manifest = cleanup.Manifest(
        tmp_path / "intent.json", RUN_ID, on_change=lambda *_args, **_kwargs: None
    )
    manifest.record(
        "ec2_instance",
        "owned-instance",
        account=config["platform_account"],
        region=config["region"],
    )
    if bad == "manifest":
        manifest = None
    elif bad == "workspace":
        config["product_workspace_id"] = "shared-platform"
    elif bad == "selection":
        config["product_cases"] = "unreviewed_case"
    elif bad == "session":
        config["session_ref"] = "/tmp/other-session.json"
    elif bad == "origin":
        config["gateway_url"] = "https://different.example.invalid"
    else:
        config["instance_id"] = "other-instance"
    runner = live._run_worker(
        SimpleNamespace(),
        {**config, "gateway_url": URL},
        lambda *_args: sent.append("install"),
    )
    with pytest.raises(ports.PortError, match="Product workspace dispatch"):
        runner("owned-instance", "product_workspace", config, manifest=manifest)
    assert sent == []


def test_workspace_requires_recorded_instance_before_persisting_org(config, tmp_path):
    observed = []
    manifest = cleanup.Manifest(
        tmp_path / "intent.json",
        RUN_ID,
        on_change=lambda *_args, **_kwargs: observed.append("persist"),
    )
    runner = live._run_worker(
        SimpleNamespace(), config, lambda *_args: observed.append("install")
    )
    with pytest.raises(ports.PortError, match="Product workspace dispatch"):
        runner("owned-instance", "product_workspace", config, manifest=manifest)
    assert observed == []
    assert manifest.outstanding() == []


def test_missing_durable_commit_refuses_before_remote_dispatch(config, tmp_path):
    manifest = cleanup.Manifest(
        tmp_path / "intent.json",
        RUN_ID,
        on_change=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("store unavailable")
        ),
    )
    manifest.on_change = lambda *_args, **_kwargs: None
    manifest.record(
        "ec2_instance",
        "owned-instance",
        account=config["platform_account"],
        region=config["region"],
    )
    manifest.on_change = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("store unavailable")
    )
    sent = []
    runner = live._run_worker(
        SimpleNamespace(), config, lambda *_args: sent.append("install")
    )
    with pytest.raises(RuntimeError, match="store unavailable"):
        runner("owned-instance", "product_workspace", config, manifest=manifest)
    assert sent == []


def test_shipped_workspace_creator_self_check_refuses_missing_module(tmp_path):
    for name, content in bundle.sources():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    checked = subprocess.run(
        [
            sys.executable,
            "-S",
            "-B",
            str(tmp_path / "remote/dispatcher.py"),
            "--self-check",
        ],
        cwd=tmp_path / "remote",
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
    assert "product_workspace" in json.loads(checked.stdout)["purposes"]
    (tmp_path / "remote/product_workspace.py").unlink()
    missing = subprocess.run(
        [
            sys.executable,
            "-S",
            "-B",
            str(tmp_path / "remote/dispatcher.py"),
            "--self-check",
        ],
        cwd=tmp_path / "remote",
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert missing.returncode != 0
    assert "product_workspace" in json.loads(missing.stdout)["problems"]


def test_protected_workspace_accepts_matched_denial_selection_without_creating_extra_org(
    config, tmp_path
):
    config["product_cases"] = "api_product_lifecycle,api_denial_oracle"
    manifest = cleanup.Manifest(
        tmp_path / "intent.json", RUN_ID, on_change=lambda *_args, **_kwargs: None
    )
    manifest.record(
        "ec2_instance",
        "owned-instance",
        account=config["platform_account"],
        region=config["region"],
    )
    calls = []

    class Ssm:
        def json_result(self, *_args, **_kwargs):
            calls.append("dispatch")
            return {"Status": "Success"}, {"success": True}

    runner = live._run_worker(Ssm(), config, lambda *_args: calls.append("install"))
    assert runner("owned-instance", "product_workspace", config, manifest=manifest) == {
        "success": True
    }
    assert calls == ["install", "dispatch"]
    assert (
        len([entry for entry in manifest.outstanding() if entry["kind"] == "adp_org"])
        == 1
    )


def test_protected_workspace_refuses_wrong_selected_denial_cases(config, tmp_path):
    manifest = cleanup.Manifest(
        tmp_path / "intent.json", RUN_ID, on_change=lambda *_args, **_kwargs: None
    )
    manifest.record(
        "ec2_instance",
        "owned-instance",
        account=config["platform_account"],
        region=config["region"],
    )
    payload = {**config, "product_cases": "api_product_lifecycle,api_denial_oracle"}
    called = []
    runner = live._run_worker(object(), config, lambda *_args: called.append("install"))
    with pytest.raises(ports.PortError, match="Product workspace dispatch"):
        runner("owned-instance", "product_workspace", payload, manifest=manifest)
    assert called == []
    assert not any(entry["kind"] == "adp_org" for entry in manifest.outstanding())
