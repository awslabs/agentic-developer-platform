"""Native glab setup, exact Git credential boundaries and private persistence."""

import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
spec = importlib.util.spec_from_file_location("gitlab_native_auth", CLI / "adp_gitlab_auth.py")
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)

ENTRY = {
    "gateway": "https://adp.example.test/api",
    "gitlab_url": "https://adp.example.test/gitlab",
    "subject": "alice",
    "username": "migrated-login",
    "user_id": 5,
    "client_id": "client",
}


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yml"
    auth.atomic(auth.owners(path), json.dumps({"adp.example.test": ENTRY}).encode())
    return path


@pytest.mark.parametrize(
    "fields",
    [
        "protocol=http\nhost=adp.example.test\npath=gitlab/group/repo.git\n",
        "protocol=https\nhost=foreign.test\npath=gitlab/group/repo.git\n",
        "protocol=https\nhost=adp.example.test\npath=gitlab-other/group/repo.git\n",
        "protocol=https\nhost=adp.example.test\npath=gitlab/../api\n",
        "protocol=https\nhost=adp.example.test\npath=gitlab/%2e%2e/api\n",
        "protocol=https\nhost=adp.example.test\npath=gitlab//repo\n",
        "protocol=https\nhost=adp.example.test\n",
        "protocol=https\nhost=adp.example.test\npath=gitlab/repo\nusername=bob\n",
        "protocol=https\nhost=adp.example.test\nhost=foreign.test\npath=gitlab/repo\n",
        "url=https://adp.example.test/gitlab/repo\n",
    ],
)
def test_foreign_host_path_and_identity_never_resolve_credentials(config, monkeypatch, fields):
    native = Mock()
    monkeypatch.setattr(auth, "native_credential", native)
    out = io.StringIO()
    auth.credential(config, "get", io.StringIO(fields + "\n"), out)
    native.assert_not_called()
    assert out.getvalue() == ""


def test_git_receives_only_access_token_and_expiry(config, monkeypatch):
    monkeypatch.setattr(
        auth,
        "native_credential",
        lambda *a: {"username": "oauth2", "password": "access", "password_expiry_utc": "12345", "oauth_refresh_token": "never-to-git"},
    )
    out = io.StringIO()
    auth.credential(config, "get", io.StringIO("protocol=https\nhost=adp.example.test\npath=gitlab/group/repo.git\n\n"), out)
    assert out.getvalue() == "username=oauth2\npassword=access\npassword_expiry_utc=12345\n\n"


def test_repository_environment_cannot_select_another_credential(monkeypatch):
    for key in ["GITLAB_TOKEN", "GITLAB_API_HOST", "GLAB_IS_OAUTH2", "CI_JOB_TOKEN", "GIT_CONFIG_COUNT", "OAUTH_TOKEN", "GITLAB_SUBFOLDER"]:
        monkeypatch.setenv(key, "foreign")
    env = auth.glab_env(Path("/private/config"))
    assert not any(value == "foreign" for value in env.values())
    assert env["GLAB_CONFIG_DIR"] == "/private/config"


@pytest.mark.skipif(not shutil.which("glab"), reason="Native glab acceptance requires glab >= 1.125.0")
def test_real_glab_roundtrip_preserves_other_hosts_without_secret_argv(config, monkeypatch):
    auth.atomic(config, b"hosts:\n  gitlab.com:\n    token: unrelated\n")
    original = auth.run
    calls = []

    def recording(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(auth, "run", recording)
    auth.write_config(
        config,
        "adp.example.test",
        {
            "use_keyring": "false",
            "api_host": "adp.example.test",
            "subfolder": "gitlab",
            "client_id": "public",
            "oauth2_expiry_date": "2026-10-28T18:00:00Z",
            "is_oauth2": "true",
        },
        {"token": "b" * 64, "oauth2_refresh_token": "c" * 64},
    )
    assert not any("b" * 64 in arg or "c" * 64 in arg for call in calls for arg in call)
    assert auth.config_get(config, "gitlab.com", "token") == "unrelated"
    assert auth.config_get(config, "adp.example.test", "token") == "b" * 64
    assert auth.config_get(config, "adp.example.test", "oauth2_refresh_token") == "c" * 64
    assert auth.config_get(config, "adp.example.test", "is_oauth2") == "true"
    assert config.stat().st_mode & 0o777 == 0o600
    auth.write_config(config, "adp.example.test", {"use_keyring": "false", "token": "", "oauth2_refresh_token": "", "is_oauth2": "false"})
    assert auth.config_get(config, "adp.example.test", "token") == ""
    assert auth.config_get(config, "adp.example.test", "oauth2_refresh_token") == ""
    assert auth.config_get(config, "gitlab.com", "token") == "unrelated"


def test_bootstrap_sends_adp_token_only_to_gateway(config, monkeypatch):
    monkeypatch.setattr(auth, "location", lambda: config)
    monkeypatch.setattr(auth, "load_owners", lambda _: {})
    monkeypatch.setattr(auth, "config_get", lambda *a: "")
    api = Mock(base=ENTRY["gateway"])
    api.request.return_value = {"gitlab_url": ENTRY["gitlab_url"], "subject": "alice", "assertion": "one-time-assertion"}
    monkeypatch.setattr(auth.common, "Api", lambda: api)
    post = Mock(
        return_value={
            "username": "migrated-login",
            "subject": "alice",
            "user_id": 5,
            "token_type": "Bearer",
            "created_at": 100,
            "expires_in": 7200,
            "client_id": "public",
            "access_token": "native-access",
            "refresh_token": "native-refresh",
        }
    )
    monkeypatch.setattr(auth, "post", post)
    monkeypatch.setattr(auth, "write_config", Mock())
    monkeypatch.setattr(auth, "setup_git", Mock())
    auth.setup()
    api.request.assert_called_once_with("POST", "/auth/gitlab-cli", {})
    post.assert_called_once_with(ENTRY["gitlab_url"] + "/-/adp/cli/token", {"assertion": "one-time-assertion"})


def test_other_deployment_cannot_replace_owned_host(config, monkeypatch):
    monkeypatch.setattr(auth, "location", lambda: config)
    api = Mock(base="https://other.example.test/api")
    api.request.return_value = {"gitlab_url": ENTRY["gitlab_url"], "subject": "bob", "assertion": "assertion"}
    monkeypatch.setattr(auth.common, "Api", lambda: api)
    post = Mock()
    monkeypatch.setattr(auth, "post", post)
    with pytest.raises(auth.common.CliError, match="another ADP deployment"):
        auth.setup()
    post.assert_not_called()


@pytest.mark.parametrize(
    "verb,core_status,expected",
    [
        ("login", 0, ["core login", "gitlab setup"]),
        ("import", 0, ["core import", "gitlab setup"]),
        ("login", 1, ["core login"]),
        ("logout", 0, ["gitlab logout", "core logout"]),
    ],
)
def test_real_adp_front_door_bootstraps_only_after_successful_login(tmp_path, verb, core_status, expected):
    install = tmp_path / "bin"
    shutil.copytree(CLI, install)
    calls = tmp_path / "calls"
    (install / "bg-cognito-auth.sh").write_text('#!/bin/bash\nprintf "core %s\\n" "$1" >> "$AUTH_CALLS"\nexit ' + str(core_status) + "\n")
    (install / "adp_gitlab_auth.py").write_text('import os,sys\nwith open(os.environ["AUTH_CALLS"],"a") as f: f.write("gitlab "+sys.argv[1]+"\\n")\n')
    (install / "glab").write_text("#!/bin/sh\nexit 0\n")
    (install / "glab").chmod(0o755)
    args = ["bash", str(install / "adp"), verb]
    if verb in {"login", "import"}:
        args += ["--gateway-url", "https://adp.example.test"]
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=20,
        env={"HOME": str(tmp_path), "PATH": str(install) + ":/usr/bin:/bin:/usr/local/bin", "AUTH_CALLS": str(calls)},
    )
    assert result.returncode == core_status, result.stderr
    assert calls.read_text().splitlines() == expected
