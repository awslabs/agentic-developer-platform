"""Clone GitHub repositories with per-request, non-persistent credentials."""

from pathlib import Path
import tempfile
from urllib.parse import urlsplit


_ASKPASS = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' 'x-access-token' ;;
  *Password*) printf '%s\\n' "$ADP_DEEPWIKI_GITHUB_TOKEN" ;;
  *) exit 1 ;;
esac
"""


def clone_from_github(remote_url, local_path, *, clone, access_token=None, **kwargs):
    """Use GitHub's token-as-password contract without putting it in the URL.

    GitPython receives a separate environment for each clone, so concurrent
    requests cannot replace one another's credentials. The remote stored in
    .git/config and any command/error output retain the credential-free URL.
    """
    parsed = urlsplit(remote_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected a credential-free HTTPS GitHub repository URL")

    env = {
        **kwargs.pop("env", {}),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "http.followRedirects",
        "GIT_CONFIG_VALUE_1": "false",
    }
    if not access_token:
        return clone(url=remote_url, to_path=local_path, env=env, **kwargs)

    with tempfile.TemporaryDirectory(prefix="deepwiki-git-") as scratch:
        askpass = Path(scratch) / "askpass"
        askpass.write_text(_ASKPASS)
        askpass.chmod(0o700)
        env.update(
            {
                "GIT_ASKPASS": str(askpass),
                "ADP_DEEPWIKI_GITHUB_TOKEN": access_token,
            }
        )
        return clone(url=remote_url, to_path=local_path, env=env, **kwargs)
