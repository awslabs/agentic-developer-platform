#!/usr/bin/env python3
"""Bootstrap native glab OAuth from the selected ADP login (stdlib only)."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

import adp_common as common

MIN_GLAB = (1, 125, 0)  # Released subfolder refresh and credential persistence fixes.


def run(args, *, env=None, data=None, cwd=None, allow_failure=False):
    result = subprocess.run(args, input=data, capture_output=True, text=True, env=env, cwd=cwd, timeout=120)
    if result.returncode and not allow_failure:
        # Child diagnostics can contain credentials; never forward them.
        raise common.CliError("GitLab CLI setup failed. Check glab installation and private config permissions.")
    return result.stdout.strip()


def glab_env(config_dir=None):
    # A Git helper inherits a repository's environment. Do not let CI tokens,
    # API overrides or repository-local glab config choose the credential host.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "GLAB_", "CI_", "GIT_")) and k != "OAUTH_TOKEN"}
    env.update(GLAB_CHECK_UPDATE="false", GLAB_PROMPT_DISABLED="true")
    if config_dir:
        env["GLAB_CONFIG_DIR"] = str(config_dir)
    elif os.environ.get("GLAB_CONFIG_DIR"):
        env["GLAB_CONFIG_DIR"] = os.environ["GLAB_CONFIG_DIR"]
    return env


def glab(args, config_dir=None, **kwargs):
    with tempfile.TemporaryDirectory(prefix="adp-glab-cwd-") as directory:
        return run(["glab", *args], env=glab_env(config_dir), cwd=directory, **kwargs)


def location():
    if not shutil.which("glab"):
        raise common.CliError("Install GitLab CLI (glab 1.125.0 or newer), then run adp gitlab setup. No GitLab login is needed.")
    version = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", glab(["version"]))
    if not version or tuple(map(int, version.groups())) < MIN_GLAB:
        raise common.CliError("Upgrade glab to 1.125.0 or newer, then run adp gitlab setup.")
    return Path(glab(["config", "path"])).expanduser().absolute()


def atomic(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".adp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read(path):
    return path.read_bytes() if path.exists() else b""


@contextlib.contextmanager
def locked(config):
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (config.parent / ".adp-gitlab.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def owners(config):
    return config.parent / ".adp-gitlab-hosts.json"


def load_owners(config):
    return json.loads(read(owners(config)) or b"{}")


def validate_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port == 0
        or any(c in value for c in ("%", "\\"))
        or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
        or not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*", parsed.path)
        or any(c in {".", ".."} for c in parsed.path.split("/"))
    ):
        raise common.CliError("ADP returned an invalid GitLab authority.")
    return parsed


def post(url, body):
    # This sends only the purpose-bound assertion or native OAuth credential,
    # never the ADP access token. No redirect may carry it elsewhere.
    request = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with build_opener(common.NoRedirect()).open(request, timeout=30) as response:
        return json.loads(response.read(32_768))


def config_get(config, host, key):
    return glab(["config", "get", key, "--host", host], config.parent, allow_failure=True)


def write_config(config, host, values, secrets=None):
    """Let glab parse YAML; put secrets into a private staged file, never argv.

    A random token marker identifies the serialized host block. Internal OAuth
    fields are replaced there as quoted JSON scalars (a YAML subset); unsupported
    layouts fail closed. Compare and replace under glab's native writer lock.
    """
    before = read(config)
    if config.is_symlink():
        raise common.CliError("GitLab CLI config must be a regular private file.")
    with tempfile.TemporaryDirectory(prefix=".adp-glab-", dir=config.parent) as directory:
        staged = Path(directory) / "config.yml"
        atomic(staged, before or b"{}\n")
        values = dict(values)
        private_fields = {key: values.pop(key) for key in ("is_oauth2", "oauth2_refresh_token", "oauth2_expiry_date") if key in values}
        private_fields.update(secrets or {})
        marker = "adpplaceholder" + uuid.uuid4().hex
        values["token"] = marker
        # Public config commands serialize YAML without exposing any credential.
        for key in ["use_keyring", "token", *[k for k in values if k not in {"use_keyring", "token"}]]:
            if key in values:
                glab(["config", "set", key, str(values[key]), "--host", host], Path(directory))
        lines = staged.read_text().splitlines(keepends=True)
        matches = [(i, re.fullmatch(r'( +)token: ["\']?' + marker + r'["\']?\n?', line)) for i, line in enumerate(lines)]
        matches = [(i, match) for i, match in matches if match]
        if len(matches) != 1:
            raise common.CliError("GitLab CLI configuration format changed; setup stopped safely.")
        index, match = matches[0]
        indent = match[1]
        start = index
        while start and (not lines[start - 1].strip() or lines[start - 1].startswith(indent)):
            start -= 1
        end = index + 1
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith(indent)):
            end += 1
        private_fields.setdefault("token", values.get("token", ""))
        if private_fields["token"] == marker:
            private_fields["token"] = ""
        kept = []
        for line in lines[start:end]:
            field = re.match(re.escape(indent) + r"(\w+):", line)
            if field and field[1] in private_fields:
                # glab emits these internal fields as single quoted/plain scalars.
                # Refuse multiline/custom YAML instead of leaving duplicate keys.
                if line.rstrip().endswith(("|", ">", "|-", ">-")):
                    raise common.CliError("Unsupported GitLab credential configuration.")
                continue
            kept.append(line)
        for key, value in private_fields.items():
            if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                raise common.CliError("Invalid GitLab OAuth response.")
            kept.append(indent + key + ": " + json.dumps(value) + "\n")
        data = "".join(lines[:start] + kept + lines[end:])
        with (config.parent / "config.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if read(config) != before:
                raise common.CliError("GitLab CLI config changed during setup. Run adp gitlab setup again.")
            atomic(config, data.encode())


def native_credential(config, entry):
    parsed = validate_url(entry["gitlab_url"])
    # Verify refresh cannot be redirected through a changed host configuration.
    expected = {
        "api_host": parsed.netloc,
        "api_protocol": "https",
        "subfolder": parsed.path.strip("/"),
        "client_id": entry["client_id"],
        "is_oauth2": "true",
        "user": entry["username"],
    }
    if any(config_get(config, parsed.netloc, key) != value for key, value in expected.items()):
        raise common.CliError(
            "GitLab credentials were changed outside ADP. Use a separate GLAB_CONFIG_DIR or log out that glab host first.", "gitlab_config_changed"
        )
    result = glab(["auth", "git-credential", "get"], config.parent, data=f"protocol=https\nhost={parsed.netloc}\npath={parsed.path.strip('/')}/\n\n")
    return dict(line.split("=", 1) for line in result.splitlines() if "=" in line)


def revoke(config, entry):
    credential = native_credential(config, entry)
    token = credential.get("password")
    if not token:
        raise common.CliError("Could not resolve the GitLab session for revocation.")
    post(entry["gitlab_url"] + "/oauth/revoke", {"client_id": entry["client_id"], "token": token})


def setup_git(config, entry):
    base = "credential." + entry["gitlab_url"]
    helper = "!" + shlex.join([sys.executable, str(Path(__file__).resolve()), "credential", str(config)])
    # Reset inherited helpers for this exact HTTPS path; prevent token persistence
    # in a global 'store' helper. Other hosts/path helpers are left intact.
    run(["git", "config", "--global", "--replace-all", base + ".helper", ""])
    run(["git", "config", "--global", "--add", base + ".helper", helper])
    run(["git", "config", "--global", base + ".useHttpPath", "true"])
    run(["git", "config", "--global", "http." + entry["gitlab_url"] + ".followRedirects", "false"])


def setup():
    config = location()
    api = common.Api()
    handoff = api.request("POST", "/auth/gitlab-cli", {})
    parsed = validate_url(handoff["gitlab_url"])
    with locked(config):
        entries = load_owners(config)
        old = entries.get(parsed.netloc)
        if old and (old["gateway"] != api.base or old["gitlab_url"] != handoff["gitlab_url"]):
            raise common.CliError("This GitLab host is managed by another ADP deployment. Use a separate GLAB_CONFIG_DIR.")
        if not old and (config_get(config, parsed.netloc, "token") or config_get(config, parsed.netloc, "use_keyring") == "true"):
            raise common.CliError("This GitLab host already has credentials. Use a separate GLAB_CONFIG_DIR or glab auth logout for that host first.")
        if old:
            try:
                revoke(config, old)
            except common.CliError as exc:
                if exc.code == "gitlab_config_changed":
                    raise
                print("Previous GitLab grant could not be revoked; replacing local credentials with this ADP login.", file=sys.stderr)
            except Exception:
                print("Previous GitLab grant could not be revoked; replacing local credentials with this ADP login.", file=sys.stderr)
        token = post(handoff["gitlab_url"] + "/-/adp/cli/token", {"assertion": handoff["assertion"]})
        if (
            token.get("subject") != handoff["subject"]
            or token.get("token_type") != "Bearer"
            or not isinstance(token.get("username"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", token["username"])
            or type(token.get("user_id")) is not int
            or token["user_id"] <= 0
        ):
            raise common.CliError("GitLab identity did not match the ADP login.")
        expiry = datetime.fromtimestamp(int(token["created_at"]) + int(token["expires_in"]), UTC).isoformat()
        entry = {
            "gateway": api.base,
            "gitlab_url": handoff["gitlab_url"],
            "subject": handoff["subject"],
            "username": token["username"],
            "user_id": token["user_id"],
            "client_id": token["client_id"],
        }
        try:
            write_config(
                config,
                parsed.netloc,
                {
                    "use_keyring": "false",
                    "api_host": parsed.netloc,
                    "api_protocol": "https",
                    "git_protocol": "https",
                    "subfolder": parsed.path.strip("/"),
                    "client_id": token["client_id"],
                    "user": token["username"],
                    "oauth2_expiry_date": expiry,
                    "is_oauth2": "true",
                },
                {"token": token["access_token"], "oauth2_refresh_token": token["refresh_token"]},
            )
            entries[parsed.netloc] = entry
            atomic(owners(config), json.dumps(entries).encode())
            setup_git(config, entry)
        except Exception:
            # A failed local install must not leave an untracked refresh grant.
            with contextlib.suppress(Exception):
                post(handoff["gitlab_url"] + "/oauth/revoke", {"client_id": token["client_id"], "token": token["access_token"]})
            raise
    print("GitLab CLI and HTTPS Git are ready with your ADP identity.")


def logout():
    if not shutil.which("glab"):
        return
    config = location()
    with locked(config):
        entries = load_owners(config)
        gateway = common.gateway_url()
        failed = False
        for host, entry in list(entries.items()):
            if entry["gateway"] != gateway:
                continue
            try:
                revoke(config, entry)
            except common.CliError as exc:
                if exc.code == "gitlab_config_changed":
                    print("GitLab credentials changed outside ADP; leaving that host's credentials in place.", file=sys.stderr)
                    del entries[host]
                    continue
                failed = True
            except Exception:
                failed = True
            write_config(
                config, host, {"use_keyring": "false", "token": "", "oauth2_refresh_token": "", "oauth2_expiry_date": "", "is_oauth2": "false"}
            )
            del entries[host]
        atomic(owners(config), json.dumps(entries).encode())
        if failed:
            print(
                "GitLab credentials cleared locally; remote revocation could not be confirmed. Revoke ADP CLI in GitLab's Authorized Applications.",
                file=sys.stderr,
            )


def credential(config, operation, source=sys.stdin, target=sys.stdout):
    if operation != "get":
        return
    fields = {}
    for _ in range(32):
        line = source.readline(8193)
        if not line.strip():
            break
        key, separator, value = line.rstrip("\n").partition("=")
        if not separator or key in fields or len(line) > 8192:
            return
        fields[key] = value
    if fields.get("protocol") != "https" or "url" in fields:
        return
    entry = load_owners(config).get(fields.get("host"))
    if not entry:
        return
    root = validate_url(entry["gitlab_url"]).path.strip("/")
    path = fields.get("path", "")
    if (
        (root and not path.startswith(root + "/"))
        or not re.fullmatch(r"[A-Za-z0-9._~/-]+", path)
        or any(p in {".", "..", ""} for p in path.split("/"))
    ):
        return
    if fields.get("username") not in (None, "", "oauth2", entry["username"]):
        return
    with locked(config):
        credential = native_credential(config, entry)
    # Do not give Git a refresh token; glab remains the sole refresh owner.
    for key in ("username", "password", "password_expiry_utc"):
        if credential.get(key):
            target.write(key + "=" + credential[key] + "\n")
    target.write("\n")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv == ["setup"]:
            setup()
        elif argv == ["logout"]:
            logout()
        elif len(argv) == 3 and argv[0] == "credential":
            credential(Path(argv[1]), argv[2])
        else:
            raise common.CliError("Usage: adp gitlab setup")
        return 0
    except Exception as exc:
        # Credentials can appear in HTTP/subprocess exceptions. Only our own
        # deliberately authored errors are safe for the terminal.
        message = str(exc) if isinstance(exc, common.CliError) else "GitLab CLI setup is unavailable. Retry adp gitlab setup."
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
