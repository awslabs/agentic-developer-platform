"""SDK refresh, selection, and fail-closed behavior of the installed Dev Box provider."""

import configparser
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "adp_cred" / "devbox.py"
spec = importlib.util.spec_from_file_location("devbox_credentials", SOURCE)
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)
ENV = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:12345", "ANTHROPIC_API_KEY": "a" * 64}
ROWS = [{"id": "connection-one", "label": "development", "account_id": "123456789012"}]


def payload(key="synthetic-access", seconds=900):
    return {"Version": 1, "AccessKeyId": key, "SecretAccessKey": "synthetic-secret", "SessionToken": "synthetic-session",
            "Expiration": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()}


def reply(value):
    return lambda *args, **kwargs: io.BytesIO(json.dumps(value).encode())


def test_profiles_contain_only_configuration_and_pin_connections(tmp_path):
    config = tmp_path / "config"
    provider.sync(ENV, str(config), reply({"connections": ROWS}))
    parsed = configparser.RawConfigParser()
    parsed.read(config)
    assert parsed["default"]["credential_process"].endswith(" credential-process")
    selected = parsed["profile " + provider.profile(ROWS[0])]["credential_process"]
    assert selected.endswith("--connection-id connection-one")
    assert "synthetic" not in config.read_text()
    assert "a" * 64 not in config.read_text()


def test_explicit_selection_is_sent_without_any_claimed_user():
    seen = []
    def opener(request, **_):
        seen.append(json.loads(request.data))
        return io.BytesIO(json.dumps(payload()).encode())
    provider.credentials(ENV, label="production", opener=opener)
    assert seen == [{"operation": "assume", "label": "production"}]


@pytest.mark.parametrize("result", [payload(seconds=-1), {**payload(), "Version": 2}, {**payload(), "owner": "other"}])
def test_rejects_invalid_provider_credentials(result):
    with pytest.raises(provider.Unavailable):
        provider.credentials(ENV, opener=reply(result))


def test_http_errors_never_reveal_response_body():
    def refused(*_, **__):
        raise urllib.error.HTTPError("http://127.0.0.1:12345", 403, "denied", {}, io.BytesIO(b"provider-secret"))
    with pytest.raises(provider.Unavailable, match="disabled") as error:
        provider.credentials(ENV, opener=refused)
    assert "provider-secret" not in str(error.value)


def test_discovery_outage_leaves_a_refusing_default_instead_of_metadata_fallback(tmp_path):
    def unavailable(*_, **__):
        raise OSError("synthetic-provider-secret")
    with pytest.raises(provider.Unavailable):
        provider.sync(ENV, str(tmp_path / "config"), unavailable)
    assert "credential-process" in (tmp_path / "config").read_text()
    assert "secret" not in (tmp_path / "config").read_text()


def test_sdk_runs_provider_again_for_refresh_without_login_or_saved_keys(tmp_path, monkeypatch):
    boto3 = pytest.importorskip("boto3")
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/aws/credentials"
            assert self.headers["x-api-key"] == "a" * 64
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            response = json.dumps(payload("synthetic-access-" + str(len(calls)), seconds=30)).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(response)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:" + str(server.server_port))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "a" * 64)
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-keys"))
        config = tmp_path / "config"
        config.write_text(f'[default]\ncredential_process = "{sys.executable}" "{SOURCE}" credential-process --connection-id connection-one\n')
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        credentials = boto3.Session().get_credentials()
        first = credentials.get_frozen_credentials().access_key
        second = credentials.get_frozen_credentials().access_key
        assert first != second
        assert all(call == {"operation": "assume", "connection_id": "connection-one"} for call in calls)
        assert not (tmp_path / "no-keys").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_assume_selects_connection_id_with_duplicate_labels(monkeypatch, capsys):
    second = {**ROWS[0], "id": "connection-two"}
    monkeypatch.setattr(provider, "connections", lambda _: ROWS + [second])
    assert provider.main(["assume", "--connection-id", second["id"]], ENV) == 0
    assert capsys.readouterr().out.strip() == provider.profile(second)
