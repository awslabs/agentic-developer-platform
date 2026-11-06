"""host/adp-github-token relays the owner's GitHub token from the loopback adapter, never from disk (#8555)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "host" / "adp-github-token"
spec = importlib.util.spec_from_loader(
    "adp_github_token", importlib.machinery.SourceFileLoader("adp_github_token", str(SCRIPT))
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

BASE = "http://127.0.0.1:43117"
KEY = "a" * 64
REPLY = {"token": "gho_fixture_owner", "login": "owner", "expires_at": 1_800_000_000}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def opener_for(status=200, body=None, calls=None):
    def opener(request, timeout):
        (calls if calls is not None else []).append(
            (request.full_url, request.get_header("X-api-key"), timeout)
        )
        payload = json.dumps(REPLY if body is None else body).encode()
        if status == 200:
            return FakeResponse(payload)
        raise urllib.error.HTTPError(request.full_url, status, "refused", {}, FakeResponse(payload))

    return opener


def test_prints_the_bare_token_from_the_crew_environment():
    calls, out = [], io.StringIO()
    code = helper.main(
        [],
        environment={"ANTHROPIC_BASE_URL": BASE, "ANTHROPIC_API_KEY": KEY},
        opener=opener_for(calls=calls),
        stdout=out,
    )
    assert code == 0 and out.getvalue() == "gho_fixture_owner\n"
    assert calls == [(BASE + "/github/token", KEY, 45)]


def test_json_mode_and_tools_variables_take_precedence():
    out = io.StringIO()
    environment = {
        "ADP_CREW_TOOLS_URL": BASE,
        "ADP_CREW_TOOLS_TOKEN": KEY,
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
        "ANTHROPIC_API_KEY": "b" * 64,
    }
    calls = []
    assert (
        helper.main(["--json"], environment=environment, opener=opener_for(calls=calls), stdout=out)
        == 0
    )
    assert json.loads(out.getvalue()) == REPLY and calls[0][0] == BASE + "/github/token"


def test_owner_shell_falls_back_to_its_private_model_env_only_when_it_is_private(tmp_path):
    directory = tmp_path / "shell"
    directory.mkdir(mode=0o700)
    settings = directory / "model.env"
    settings.write_text(
        f"ANTHROPIC_BASE_URL={BASE}\nANTHROPIC_API_KEY={KEY}\nANTHROPIC_MODEL=m\nADP_CREW_RUN_ID=x\n"
    )
    settings.chmod(0o600)
    assert helper.adapter({}, directory=str(directory)) == (BASE, KEY)
    settings.chmod(0o644)
    with pytest.raises(helper.Unavailable, match="adapter is not running"):
        helper.adapter({}, directory=str(directory))
    settings.chmod(0o600)
    directory.chmod(0o755)
    with pytest.raises(helper.Unavailable):
        helper.adapter({}, directory=str(directory))
    directory.chmod(0o700)
    with pytest.raises(helper.Unavailable):
        helper.adapter({}, directory=str(tmp_path / "absent"))


@pytest.mark.parametrize(
    "environment",
    [
        {"ANTHROPIC_BASE_URL": "https://attacker.invalid", "ANTHROPIC_API_KEY": KEY},
        {"ANTHROPIC_BASE_URL": BASE, "ANTHROPIC_API_KEY": "short"},
    ],
)
def test_only_a_loopback_adapter_with_a_per_boot_key_is_used(environment, tmp_path):
    with pytest.raises(helper.Unavailable):
        helper.adapter(environment, directory=str(tmp_path / "absent"))


def test_a_refusal_names_the_step_and_never_prints_a_token(capsys):
    body = {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": "GitHub not connected: connect your GitHub account with repository access in ADP Settings → Connections",
        },
    }
    out = io.StringIO()
    code = helper.main(
        [],
        environment={"ANTHROPIC_BASE_URL": BASE, "ANTHROPIC_API_KEY": KEY},
        opener=opener_for(404, body),
        stdout=out,
    )
    assert code == 1 and out.getvalue() == ""
    assert "GitHub not connected" in capsys.readouterr().err


def test_git_credential_protocol_answers_github_https_only_and_stores_nothing(capsys):
    environment = {"ANTHROPIC_BASE_URL": BASE, "ANTHROPIC_API_KEY": KEY}
    out, calls = io.StringIO(), []
    stdin = io.StringIO("protocol=https\nhost=github.com\npath=aws-e/adp.git\n\n")
    assert (
        helper.main(
            ["--git-credential", "get"],
            environment=environment,
            opener=opener_for(calls=calls),
            stdin=stdin,
            stdout=out,
        )
        == 0
    )
    assert (
        out.getvalue() == "username=x-access-token\npassword=gho_fixture_owner\n"
        and len(calls) == 1
    )
    for request in ("protocol=https\nhost=gitlab.com\n\n", "protocol=http\nhost=github.com\n\n"):
        out, calls = io.StringIO(), []
        assert (
            helper.main(
                ["--git-credential", "get"],
                environment=environment,
                opener=opener_for(calls=calls),
                stdin=io.StringIO(request),
                stdout=out,
            )
            == 0
        )
        assert out.getvalue() == "" and calls == []
    for action in ("store", "erase"):
        out, calls = io.StringIO(), []
        stdin = io.StringIO("protocol=https\nhost=github.com\nusername=x\npassword=y\n\n")
        assert (
            helper.main(
                ["--git-credential", action],
                environment=environment,
                opener=opener_for(calls=calls),
                stdin=stdin,
                stdout=out,
            )
            == 0
        )
        assert out.getvalue() == "" and calls == []
    assert helper.main(["--bogus"], environment=environment, opener=opener_for()) == 2
    assert capsys.readouterr().out == ""


def test_scripts_are_executable_and_wrap_the_helper():
    host = SCRIPT.parent
    for name in ("adp-github-token", "gh", "git-credential-adp"):
        assert os.access(host / name, os.X_OK), name
    assert (
        "adp-github-token" in (host / "gh").read_text()
        and 'GH_TOKEN="$token" exec "$real"' in (host / "gh").read_text()
    )
    assert '--git-credential "$@"' in (host / "git-credential-adp").read_text()
    wrapper = (host / "gh").read_text()
    assert '"auth login"' in wrapper and "unsupported" in wrapper and "hosts.yml" in wrapper, "manual login notice (#8748)"
