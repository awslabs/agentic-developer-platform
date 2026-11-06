"""GitHub App tokens are passwords, never remote URLs or persistent files."""

import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "images/deepwiki/github_clone.py"
spec = importlib.util.spec_from_file_location("deepwiki_github_clone", SOURCE)
github_clone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(github_clone)


@pytest.mark.parametrize("fail", [False, True])
def test_authenticated_clone_uses_ephemeral_askpass_without_persisting_token(fail):
    token = "fixture-installation-token"
    askpass_path = None

    def clone(*, url, to_path, env, multi_options):
        nonlocal askpass_path
        askpass_path = Path(env["GIT_ASKPASS"])
        assert token not in url
        assert token not in askpass_path.read_text()
        assert url == "https://github.com/example/project.name"
        assert to_path == "/tmp/fixture-repo"
        assert multi_options == ["--depth=1"]
        assert env["GIT_CONFIG_VALUE_0"] == ""
        assert env["GIT_CONFIG_VALUE_1"] == "false"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        for prompt, expected in [
            ("Username for https://github.com:", "x-access-token"),
            ("Password for https://github.com:", token),
        ]:
            result = subprocess.run(
                [str(askpass_path), prompt],
                env={**os.environ, **env},
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout.strip() == expected
        assert "ADP_DEEPWIKI_GITHUB_TOKEN" not in os.environ
        if fail:
            raise RuntimeError("clone failed")
        return "cloned"

    if fail:
        with pytest.raises(RuntimeError, match="clone failed"):
            github_clone.clone_from_github(
                "https://github.com/example/project.name",
                "/tmp/fixture-repo",
                clone=clone,
                access_token=token,
                multi_options=["--depth=1"],
            )
    else:
        assert (
            github_clone.clone_from_github(
                "https://github.com/example/project.name",
                "/tmp/fixture-repo",
                clone=clone,
                access_token=token,
                multi_options=["--depth=1"],
            )
            == "cloned"
        )
    assert askpass_path is not None and not askpass_path.exists()


def test_public_clone_needs_no_token_or_askpass():
    def clone(**kwargs):
        assert "ADP_DEEPWIKI_GITHUB_TOKEN" not in kwargs["env"]
        assert "GIT_ASKPASS" not in kwargs["env"]
        return "public"

    assert (
        github_clone.clone_from_github(
            "https://github.com/example/public", "/tmp/public", clone=clone
        )
        == "public"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example/repo",
        "https://attacker.test/example/repo",
        "https://github.com.attacker.test/repo",
        "https://user:secret@github.com/example/repo",
        "https://github.com:444/example/repo",
        "https://github.com/example/repo?token=secret",
    ],
)
def test_credentials_cannot_be_sent_to_another_destination(url):
    def clone(**kwargs):
        pytest.fail("invalid destination reached git")

    with pytest.raises(ValueError):
        github_clone.clone_from_github(url, "/tmp/fixture", clone=clone, access_token="secret")
