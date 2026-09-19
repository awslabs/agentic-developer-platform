"""One login: the extension must create no second credential store (Issue #5039).

The upstream Superplane CLI wrote a plaintext `token` into
`~/.superplane/config.yaml`. Every developer laptop therefore held a second
long-lived secret outside ADP's vault and outside its revocation path: revoking
the ADP session did not revoke that file, and nothing tracked which machines had
one.

These tests are the regression fence for that. They run the extension against a
fake API with `$HOME` redirected into a tmp dir, then assert on the WHOLE tree:
no `~/.superplane/`, and no file anywhere under `$HOME` containing the token
value. Asserting on the tree rather than on one expected path is deliberate — a
future change that writes the token to a *different* file would satisfy a
path-specific assertion while reintroducing exactly the problem.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "cli/adp-superplane.py"
spec = importlib.util.spec_from_file_location("adp_superplane_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

TOKEN = "adp-session-token-fixture-value"
PROVIDER_SECRET = "nebius-provider-secret-fixture"


class FakeApi:
    """Records calls; every response is metadata, never a secret value."""

    base = "https://gateway.example.test/api"

    def __init__(self, responses=None):
        self.calls: list[tuple[str, str, object]] = []
        self.responses = responses or {}

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        for prefix, response in self.responses.items():
            if path.startswith(prefix):
                return response
        return {}

    @property
    def paths(self):
        return [path for _, path, _ in self.calls]


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    """Redirect $HOME so nothing touches the developer's own dotfiles."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_token(monkeypatch):
    """The session token comes from the shared transport, never from this helper."""
    monkeypatch.setattr(cli.common, "access_token", lambda: TOKEN)


def files_under(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def secret_bearing_files(root: Path, secret: str) -> list[Path]:
    found = []
    for path in files_under(root):
        try:
            if secret in path.read_text(errors="ignore"):
                found.append(path)
        except OSError:
            continue
    return found


def code_strings() -> list[str]:
    """Every string literal the helper actually EXECUTES.

    Docstrings and comments are excluded on purpose: this module's own docstring
    names `~/.superplane/config.yaml` in order to explain why it must not exist,
    and so does the helper's. Prose describing a retired path is documentation;
    a live constant holding it would be the bug.
    """
    tree = ast.parse(SCRIPT.read_text())
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in docstrings]


def test_the_helper_defines_no_superplane_config_path() -> None:
    """The retired store must not survive as a live constant."""
    for literal in code_strings():
        assert ".superplane" not in literal
        assert "config.yaml" not in literal


def test_the_helper_never_writes_a_token_of_its_own() -> None:
    """It may persist the workspace pointer, but calls no session/credential writer."""
    called = {
        node.func.attr for node in ast.walk(ast.parse(SCRIPT.read_text())) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "save_session" not in called
    assert "write_json" not in called


def test_operational_verbs_create_no_superplane_config(private_home) -> None:
    api = FakeApi({cli.API_BASE + "/workspaces": {"workspaces": []}})
    cli.run(cli.parser().parse_args(["workspace", "list"]), api)

    assert not (private_home / ".superplane").exists()


def test_selecting_a_workspace_stores_no_token(private_home) -> None:
    """`workspace use` is the ONE verb that writes: a non-secret context pointer."""
    cli.run(cli.parser().parse_args(["workspace", "use", "ml-research"]), FakeApi())

    assert not (private_home / ".superplane").exists()
    written = files_under(private_home)
    assert written, "workspace use should persist the selection"
    for path in written:
        contents = json.loads(path.read_text())
        assert contents == {"workspace": "ml-research"}
        assert TOKEN not in path.read_text()


def test_storing_a_provider_credential_leaves_no_local_copy(private_home, monkeypatch) -> None:
    """The provider value goes to the vault and is not persisted anywhere locally."""
    monkeypatch.setattr(cli, "read_provider_value", lambda from_stdin, prompt: PROVIDER_SECRET)
    api = FakeApi({"/auth/credentials": {"id": "cred-123"}, cli.API_BASE + "/providers": {"ok": True}})

    cli.run(cli.parser().parse_args(["provider", "add", "--name", "nebius-research", "--provider", "nebius"]), api)

    assert not secret_bearing_files(private_home, PROVIDER_SECRET)
    assert not secret_bearing_files(private_home, TOKEN)
    assert not (private_home / ".superplane").exists()


def test_the_session_token_is_the_only_credential_path(monkeypatch) -> None:
    """A request must carry the ADP session — proving no second token is consulted."""
    sent: dict = {}

    class RecordingOpener:
        def open(self, request, timeout=None):
            sent["auth"] = request.headers.get("Authorization")
            raise AssertionError("stop after inspecting the header")

    api = cli.Api.__new__(cli.Api)
    api.base = "https://gateway.example.test/api"
    api.opener = RecordingOpener()

    with pytest.raises(AssertionError):
        api.request("GET", cli.API_BASE + "/workspaces")

    assert sent["auth"] == f"Bearer {TOKEN}"


def test_the_state_file_is_not_a_credential_store(private_home) -> None:
    """State holds a workspace name only — no token, no ARN, no provider value."""
    cli.run(cli.parser().parse_args(["workspace", "use", "ws-1"]), FakeApi())

    state = cli.common.read_state(cli.STATE)
    assert set(state) == {"workspace"}
