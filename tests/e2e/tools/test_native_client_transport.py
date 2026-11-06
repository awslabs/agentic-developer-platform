"""Start the real installed local proxy; never call a model or live Gateway."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import pytest

from modules.tools.platform.scripts.native_test_session import (
    ApprovedReference,
    NativeSession,
    canonical,
)
from tests.e2e.tools import interpreter_operator_driver as operator
from tests.e2e.tools import native_client_transport as transport
from tests.e2e.tools import test_atlassian_native_matrix as matrix_fixtures

CLI = Path(__file__).resolve().parents[3] / "modules/gateway/cli"


@pytest.fixture
def matrix_runtime(tmp_path, monkeypatch):
    return matrix_fixtures.runtime.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_matrix_starts_the_real_original_session_transport(
    installation, matrix_runtime, monkeypatch, client
):
    from contextlib import contextmanager

    from tests.e2e.tools import atlassian_native_matrix as matrix

    plans, fixtures, _, connection = matrix_runtime
    installed = installation

    @contextmanager
    def install(*args, **kwargs):
        yield (
            installed.installed,
            {**installed.env, "client": client},
            {"adp": "c" * 64, "adp-mcp.py": "d" * 64},
        )

    monkeypatch.setattr(
        matrix.NativeSession, "exchange", lambda environment: installed.session
    )
    monkeypatch.setattr(
        matrix.NativeSession,
        "status",
        lambda session: {
            "principal": {
                "user_id": fixtures[client]["owner_id"],
                "tenant_id": session.reference.tenant_id,
            },
            "run_id": session.reference.run_id,
            "run_started_at": int(time.time()),
            "funding_status": "held",
            "cap_usd": "20",
        },
    )
    monkeypatch.setattr(matrix, "installed_clients", install)
    monkeypatch.setattr(matrix, "resume_installed_clients", install)
    monkeypatch.setattr(
        matrix, "native_client_transport", transport.native_client_transport
    )
    driver = matrix.NativeMatrix(
        plans,
        fixtures,
        gateway_url=installed.session.reference.gateway_origin,
        gateway_revision=installed.session.reference.source_revision,
        client_factory=connection,
    )
    try:
        _, environment, _, launch, _ = driver._start(client)
        assert launch.client == client
        assert environment["ADP_GATEWAY_DUMMY"]
        assert (installed.home / ("native-model-" + client) / "proxy.json").exists()
    finally:
        driver.close()
    assert not (installed.home / ("native-model-" + client) / "proxy.json").exists()
    resumed = matrix.NativeMatrix(
        plans,
        fixtures,
        gateway_url=installed.session.reference.gateway_origin,
        gateway_revision=installed.session.reference.source_revision,
        client_factory=connection,
    )
    try:
        _, environment, _, launch, _ = resumed._start(client)
        assert type(launch) is transport.NativeClientLaunch
        assert environment["ADP_GATEWAY_DUMMY"]
        assert (installed.home / ("native-model-" + client) / "proxy.json").exists()
    finally:
        resumed.close()


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_interrupted_proxy_is_stopped_without_losing_original_log_or_journal(
    installation, client
):
    fixture = installation
    directory = fixture.home / ("native-model-" + client)
    directory.mkdir(mode=0o700)
    identity = directory / "proxy.json"
    log = directory / "proxy.private.log"
    log.write_bytes(b"original proxy log\n")
    log.chmod(0o600)
    journal_directory = fixture.private.parent / fixture.session.reference.run_id
    journal_directory.mkdir(mode=0o700)
    journal = journal_directory / "attempts.json"
    journal.write_bytes(b"retained original journal")
    journal.chmod(0o600)
    with log.open("ab") as output:
        previous = subprocess.Popen(
            [
                sys.executable,
                str(fixture.installed.parent / "bg-gateway-proxy.py"),
                "--gateway-url",
                fixture.session.reference.gateway_origin + "/api",
                "--port",
                "0",
                "--auth-helper",
                str(fixture.installed.parent / "bg-cognito-auth.sh"),
                "--identity-file",
                str(identity),
                "--deployment-id",
                fixture.session.reference.deployment_id,
            ],
            env={"HOME": str(fixture.home), "PATH": os.environ["PATH"]},
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
        )
    try:
        deadline = time.monotonic() + 5
        while (
            not identity.exists()
            and previous.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert identity.exists()
        with transport.native_client_transport(
            fixture.installed,
            fixture.env,
            fixture.session,
            fixture.private,
            client=client,
            model="offline-model",
        ) as (launch, _):
            assert previous.wait(timeout=5) is not None
            assert json.loads(identity.read_text())["pid"] != previous.pid
            assert launch.client == client
            assert journal.read_bytes() == b"retained original journal"
            assert log.read_bytes().startswith(b"original proxy log\n")
    finally:
        if previous.poll() is None:
            previous.terminate()
        previous.wait(timeout=5)


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_foreign_proxy_identity_is_not_terminated_or_replaced(installation, client):
    fixture = installation
    directory = fixture.home / ("native-model-" + client)
    directory.mkdir(mode=0o700)
    identity = directory / "proxy.json"
    content = json.dumps(
        {
            "pid": os.getpid(),
            "process_start": "foreign",
            "proxy": "adp-gateway-proxy",
            "deployment_id": fixture.session.reference.deployment_id,
            "gateway_url": fixture.session.reference.gateway_origin + "/api",
        }
    )
    identity.write_text(content)
    identity.chmod(0o600)
    with pytest.raises(ValueError, match="ownership"):
        with transport.native_client_transport(
            fixture.installed,
            fixture.env,
            fixture.session,
            fixture.private,
            client=client,
            model="offline-model",
        ):
            pytest.fail("A foreign proxy must prevent restart")
    assert identity.read_text() == content


@pytest.fixture
def installation(tmp_path, monkeypatch):
    now = int(time.time())
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    installed = home / ".adp/bin/adp"
    installed.parent.mkdir(parents=True)
    names = (
        "adp",
        "adp-mcp.py",
        "adp_common.py",
        "adp_native_test.py",
        "adp_native_credential_handoff.py",
        "adp_deployments.py",
        "adp_mcp_config.py",
        "bg-gateway-proxy.py",
        "bg-cognito-auth.sh",
    )
    for name in names:
        shutil.copy(CLI / name, installed.parent / name)
    monkeypatch.setattr(
        transport, "source_file", lambda revision, name: (CLI / name).read_bytes()
    )
    reference = ApprovedReference(
        str(uuid4()),
        "tenant",
        str(uuid4()),
        "offline",
        "https://gateway.example.invalid",
        "a" * 40,
        "b" * 40,
        "42",
        "1",
    )
    token = (
        "e30."
        + base64.urlsafe_b64encode(
            json.dumps(
                {"sub": "offline", "custom:org_id": "tenant", "exp": now + 3600}
            ).encode()
        )
        .decode()
        .rstrip("=")
        + ".offline"
    )
    capability = "adpt1." + str(uuid4()) + "." + "s" * 43
    session = NativeSession(reference, token, capability, SimpleNamespace())
    value = {
        "version": 1,
        "reference": transport.asdict(reference),
        "access_token": token,
        "capability": capability,
        "started_at": now,
        "calls": [
            {
                "tool": "knowledge___search",
                "arguments_sha256": "a" * 64,
                "max_requests": 1,
            }
        ],
        "models": [
            {
                "model": "offline-model",
                "route": route,
                "max_requests": 2,
                "max_request_bytes": 65536,
                "max_output_tokens": 64,
                "max_response_bytes": 262144,
            }
            for route in ("/v1/messages", "/openai/v1/responses")
        ],
        "max_discoveries": 1,
    }
    private = tmp_path / "transport.json"
    private.write_text(json.dumps(value))
    private.chmod(0o600)
    config = home / ".bedrock-gateway"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text(
        json.dumps({"gateway_url": reference.gateway_origin + "/api"})
    )
    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "BG_CONFIG_DIR": str(config),
        "AWS_ACCESS_KEY_ID": "ambient-must-not-pass",
        "OPENAI_API_KEY": "ambient-must-not-pass",
    }
    entry = {
        "command": str(installed),
        "args": ["--deployment", "default", "mcp"],
        "env": {
            "BG_CONFIG_DIR": str(config),
            "ADP_DEPLOYMENT_ID": "default",
            "ADP_DEPLOYMENT_URL": reference.gateway_origin + "/api",
        },
    }
    for client, name in (
        ("claude-code", ".claude.json"),
        ("codex", ".codex/config.toml"),
    ):
        subprocess.run(
            [
                sys.executable,
                str(installed.parent / "adp_mcp_config.py"),
                "--client",
                client,
                "--config",
                str(home / name),
                "--state-dir",
                str(home / ".adp/mcp-config"),
                "--entry",
                json.dumps(entry),
            ],
            env=env,
            check=True,
            capture_output=True,
            timeout=10,
        )
    return SimpleNamespace(
        installed=installed, env=env, session=session, private=private, home=home
    )


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_real_local_proxy_keeps_original_session_and_refuses_unplanned_model_before_upstream(
    installation, client
):
    f = installation
    identity = f.home / ("native-model-" + client) / "proxy.json"
    with transport.native_client_transport(
        f.installed, f.env, f.session, f.private, client=client, model="offline-model"
    ) as (launch, env):
        assert launch.client == client and launch.command[0] == (
            "claude" if client == "claude-code" else "codex"
        )
        assert "AWS_ACCESS_KEY_ID" not in env and "OPENAI_API_KEY" not in env
        assert f.session.token not in " ".join(
            launch.command
        ) and f.session.capability not in " ".join(launch.command)
        configuration = f.home / (
            ".claude.json" if client == "claude-code" else ".codex/config.toml"
        )
        raw = configuration.read_text()
        config = json.loads(raw) if client == "claude-code" else tomllib.loads(raw)
        entry = config["mcpServers" if client == "claude-code" else "mcp_servers"][
            "adp-tools"
        ]
        assert entry["env"]["ADP_NATIVE_TEST_TRANSPORT_FILE"] == str(f.private)
        assert f.session.token not in raw and f.session.capability not in raw
        assert launch.configuration_sha256 == transport.digest(
            json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        )
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from adp_native_test import NativeTransport; "
                "v=NativeTransport.configured(); print(v.reference['run_id'])",
            ],
            env={
                "HOME": str(f.home),
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(f.installed.parent),
                **entry["env"],
            },
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert child.stdout.strip() == f.session.reference.run_id
        info = json.loads(identity.read_text())
        request = Request(
            "http://127.0.0.1:" + str(info["port"]) + "/openai/v1/responses",
            headers={
                "Authorization": "Bearer " + env["ADP_GATEWAY_DUMMY"],
                "Content-Type": "application/json",
            },
            data=b'{"model":"unplanned","input":"offline"}',
        )
        with pytest.raises(HTTPError) as failure:
            build_opener(ProxyHandler({})).open(request, timeout=3)
        assert failure.value.code == 403
        assert not (
            f.private.parent / f.session.reference.run_id / "attempts.json"
        ).exists()
    assert not identity.exists()


@pytest.mark.parametrize(
    "name",
    ["adp_native_test.py", "adp_native_credential_handoff.py", "adp_mcp_config.py"],
)
@pytest.mark.parametrize("damage", ["missing", "altered"])
def test_source_substitution_cannot_launch_a_native_proxy(
    installation, monkeypatch, name, damage
):
    f = installation
    path = f.installed.parent / name
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("unreviewed")
    monkeypatch.setattr(
        transport.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("unverified dependency launched proxy"),
    )
    failure = FileNotFoundError if damage == "missing" else ValueError
    with pytest.raises(failure):
        with transport.native_client_transport(
            f.installed,
            f.env,
            f.session,
            f.private,
            client="codex",
            model="offline-model",
        ):
            raise AssertionError("unreviewed client reached launch")
    assert not (f.home / "native-model-codex").exists()


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_foreign_mcp_entry_is_refused_before_proxy_or_config_write(
    installation, monkeypatch, client
):
    f = installation
    path = f.home / (
        ".claude.json" if client == "claude-code" else ".codex/config.toml"
    )
    raw = path.read_text().replace(str(f.installed), "/unowned/adp")
    path.write_text(raw)
    monkeypatch.setattr(
        transport.subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("foreign entry launched a process"),
    )
    with pytest.raises(ValueError, match="native MCP entry differs"):
        with transport.native_client_transport(
            f.installed,
            f.env,
            f.session,
            f.private,
            client=client,
            model="offline-model",
        ):
            pytest.fail("foreign configuration reached launch")
    assert path.read_text() == raw


@pytest.mark.parametrize("damage", ["missing", "altered"])
def test_operator_handoff_dependency_is_verified_before_mcp_or_request(
    installation, monkeypatch, damage
):
    f = installation
    # Explicit offline session document: no approval, credential or provider call.
    binding = {"execute_code": operator.CODE, "file_path": operator.FILE_PATH}
    reference = {
        "producer": "operator_local",
        "run_id": str(uuid4()),
        "tenant_id": "offline",
        "subject_id": "offline",
        "approval_revision": 1,
        "deployment_id": "offline",
        "gateway_origin": "https://gateway.example.invalid",
        "source_revision": "a" * 40,
        "contract_sha256": hashlib.sha256(canonical(binding)).hexdigest(),
    }
    f.private.write_text(
        json.dumps(
            {
                "version": 2,
                "reference": reference,
                "binding": binding,
                "started_at": int(time.time()),
                "access_token": "offline",
                "capability": "offline",
                "models": [],
                "max_discoveries": 1,
            }
        )
    )
    session = operator.OperatorLocalSession.read(f.private)
    monkeypatch.setattr(
        operator, "source_file", lambda revision, name: (CLI / name).read_bytes()
    )
    monkeypatch.setattr(
        operator,
        "MCPClient",
        lambda *args, **kwargs: pytest.fail("unverified dependency launched MCP"),
    )
    monkeypatch.setattr(
        operator,
        "request_json",
        lambda *args, **kwargs: pytest.fail("unverified dependency made a request"),
    )
    path = f.installed.parent / "adp_native_credential_handoff.py"
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("unreviewed")
    failure = FileNotFoundError if damage == "missing" else ValueError
    with pytest.raises(failure):
        operator.run_operator_interpreter(f.installed, f.env, session)
