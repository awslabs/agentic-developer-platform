#!/usr/bin/env python3
"""Shared ADP CLI transport, private state and output contract (stdlib only)."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


class CliError(Exception):
    def __init__(self, message, code="operation_failed", exit_code=5):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError(message, "usage_error", 1)


_UNRESOLVED = object()
_deployment = _UNRESOLVED

# The selection variables the front door exports. Their presence is what tells us
# a selection was actually requested, as opposed to a machine that has simply
# never registered a deployment — the two need opposite treatment when resolution
# fails, so this is checked exactly rather than guessed at.
_SELECTION_VARIABLES = ("ADP_DEPLOYMENT_ID", "ADP_DEPLOYMENT")


def deployment():
    """The one deployment this process runs against — resolved ONCE, then cached.

    Caching is the safety property, not an optimization (Issue #5413). A command
    reads the gateway URL and then fetches a token; if each read re-consulted the
    saved default, a concurrent `adp deployment use` in another terminal could
    change the answer in between and send one deployment's token to another
    deployment's gateway. One resolution per process makes that impossible.

    Returns None on a machine with no named deployment and no selection, which is
    what keeps the pre-#5413 paths working untouched for existing users.
    """
    global _deployment
    if _deployment is _UNRESOLVED:
        _deployment = _resolve_deployment()
    return _deployment


def _resolve_deployment():
    module = load_provider("adp_deployments.py")
    if module is None:
        # A partial install (the sibling file is missing). Degrading to the legacy
        # single-deployment paths keeps `adp login`/`adp update` usable, which is
        # how a user repairs that install.
        return None
    try:
        return module.resolve()
    except module.DeploymentError as exc:
        if any(os.environ.get(variable) for variable in _SELECTION_VARIABLES):
            # A selection WAS requested and could not be honoured. Never fall back:
            # a fallback here is precisely how a credential reaches a deployment
            # the user did not name.
            raise CliError(str(exc), exc.code, exc.exit_code) from None
        return None


def config_path():
    resolved = deployment()
    return (resolved.config_dir if resolved else Path.home() / ".bedrock-gateway") / "config.json"


def state_dir():
    resolved = deployment()
    return resolved.state_dir if resolved else Path.home() / ".adp/state"


def gateway_url():
    """The base URL of the selected deployment's API.

    A registered deployment's URL comes from the registry, because that binding is
    what the user selected and what must stay pinned for the whole command. The
    config file is the fallback, and remains the only source on a legacy machine
    that has no registry at all.
    """
    resolved = deployment()
    try:
        base = (resolved.gateway_url if resolved else "") or json.loads(config_path().read_text())["gateway_url"]
        base = base.rstrip("/")
        parsed = urllib.parse.urlsplit(base)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and local)):
            raise ValueError
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        return base if parsed.path.endswith("/api") else base + "/api"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise CliError("No valid gateway configured. Reinstall using the command on your ADP sign-in page.", "gateway_not_configured", 2) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CliError("ADP redirected the request. Check the gateway URL before sending credentials.", "unexpected_redirect")


def access_token():
    helper = Path(__file__).resolve().with_name("bg-cognito-auth.sh")
    # The helper is handed THIS process's already-resolved deployment rather than
    # being left to resolve again (Issue #5413). Re-resolving in the child is the
    # drift window: it would read the saved default a second time, and this call
    # sits between reading the gateway URL and sending the request — the one place
    # a changed answer means a token posted to the wrong gateway.
    resolved = deployment()
    environment = {**os.environ, **(resolved.environment() if resolved else {})}
    try:
        token = subprocess.run(
            ["bash", str(helper), "token"], capture_output=True, text=True, timeout=120, check=True, env=environment
        ).stdout.strip()
        if not token or any(char.isspace() for char in token):
            raise ValueError
        return token
    except (OSError, subprocess.SubprocessError, ValueError):
        raise CliError("Sign in with adp login or adp admin login.", "authentication_required", 2) from None


class Api:
    def __init__(self):
        self.base = gateway_url()
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None, *, authenticated=True, token=None, timeout=120):
        if not path.startswith("/") or path.startswith("//"):
            raise CliError("Invalid ADP API path.")
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + (token or access_token())
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                payload = response.read()
                # 204/205 are DEFINED to carry no body, and ADP uses 204 for
                # successful deletes (src/auth/vault_routes.py). Parsing that as
                # JSON raised, and the ValueError arm below relabelled a
                # SUCCESSFUL delete as "gateway_unavailable" (Issue #5039).
                # Keyed on those two statuses and an actually-empty body only:
                # an empty or malformed body on any other status is still a
                # failure, so a truncated response cannot pass as success.
                if not payload.strip() and response.status in (204, 205):
                    return {}
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            code = "http_error"
            try:
                detail = json.load(exc).get("detail", {})
                reason = detail.get("error", detail.get("reason", "")) if isinstance(detail, dict) else ""
                if re.fullmatch(r"[a-z_]{1,80}", reason):
                    code = reason
            except (ValueError, AttributeError):
                pass
            hints = {
                401: "Sign in again with adp login or adp admin login.",
                403: "This operation requires an authorized ADP administrator.",
                404: "Check the target; the gateway may need an upgrade.",
                409: "Configuration changed. Read its current status before retrying.",
                429: "Wait a minute before retrying.",
            }
            hint = hints.get(exc.code, "Check status before retrying; an interrupted request may have changed configuration.")
            raise CliError(f"ADP returned HTTP {exc.code} ({code}). {hint}", code, {401: 2, 403: 3}.get(exc.code, 5)) from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise CliError("ADP could not be reached or returned an invalid response. Check status before retrying.", "gateway_unavailable") from None


def api(method, path, body=None, **kwargs):
    return Api().request(method, path, body, **kwargs)


def private_directory(path):
    path = Path(path).absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise CliError("Use a private directory owned by you with permissions 0700.", "unsafe_file")
    return path


def write_json(path, value):
    path = Path(path)
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".adp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CliError("Credential and state files must be owned by you with permissions 0600.", "unsafe_file")
        return json.load(source)


def state_path(name):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise CliError("Invalid state name.")
    return private_directory(state_dir()) / (name + ".json")


def read_state(name):
    path = state_path(name)
    return read_private_json(path) if path.exists() else {}


def write_state(name, value):
    write_json(state_path(name), value)


def save_session(result):
    directory = private_directory(config_path().parent)
    lock = directory / "refresh.lock"
    deadline = time.monotonic() + 30
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise CliError("A token refresh is still running. Retry adp admin login.") from None
            time.sleep(0.1)
    try:
        config = json.loads(config_path().read_text())
        config.update({key: result[key] for key in ("client_id", "user_pool_id", "region")})
        config.update(refresh_via="gateway", identity_pool_id="")
        tokens = {key: result[key] for key in ("access_token", "id_token", "refresh_token")}
        tokens["expires_at"] = int(time.time()) + int(result["expires_in"])
        write_json(config_path(), config)
        write_json(directory / "tokens.json", tokens)
    finally:
        lock.rmdir()


def organization_context(explicit, client):
    return explicit or os.environ.get("ADP_ORG") or client.request("GET", "/auth/cli/admin-session").get("org_id") or None


def envelope(status, command, detail=None, next_action=None):
    return {"status": status, "command": command, "detail": detail or {}, "next_action": next_action}


def emit(result, as_json=False):
    if as_json:
        print(json.dumps(result))
    else:
        print(f"{result['command']}: {result['status']}")
        if result.get("detail"):
            print(json.dumps(result["detail"], indent=2))
        if result.get("next_action"):
            print(result["next_action"])
        if result.get("error"):
            print(result["error"]["message"], file=sys.stderr)
    return 5 if result["status"] == "failed" else 4 if result["status"] in {"pending", "unavailable"} else 0


def report_error(exc, command, as_json):
    if not isinstance(exc, CliError):
        exc = CliError("Invalid response or local file error. Check the current setup before retrying.")
    result = envelope("failed", command)
    result["error"] = {"code": exc.code, "message": str(exc)}
    emit(result, as_json)
    return exc.exit_code


def load_provider(filename):
    path = Path(__file__).resolve().with_name(filename)
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError:
        return None
    return module


def open_browser(url):
    print(url, file=sys.stderr)
    with contextlib.suppress(webbrowser.Error):
        return webbrowser.open(url)
    return False
