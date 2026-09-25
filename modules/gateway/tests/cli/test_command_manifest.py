"""The command manifest must agree with what actually ships — Issue #5621.

CLI-08-AC-03. A manifest that is merely *present* is worse than no manifest: it
looks authoritative while drifting from the code, so a user reads it, believes a
command exists, and gets "unknown command". The only property that makes the file
worth keeping is that these tests hold it against the real artifacts in BOTH
directions:

* every manifest command must be dispatchable by the real `adp` script
* every helper a manifest entry names must be installable AND downloadable
* every capability ID a manifest entry names must exist in the server contract
* every shipped command must appear in `adp help`

The last one is the direction people forget. A test that only checked
"manifest -> code" would pass happily while the CLI grew a command the manifest
never heard of, which is exactly how the documentation drift this story exists to
close gets reintroduced.

This is a checked manifest, NOT a plugin framework: nothing here is loaded at
runtime, and adding an entry cannot create a command. The parser remains the
implementation; this file is a claim about the parser that must be true.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).parents[2] / "cli"
ADP = CLI_DIR / "adp"
AUTH = CLI_DIR / "bg-cognito-auth.sh"
INSTALL = CLI_DIR / "install.sh"
MANIFEST_PATH = CLI_DIR / "command-manifest.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture(scope="module")
def commands(manifest) -> list[dict]:
    return manifest["commands"]


@pytest.fixture(scope="module")
def shipped(commands) -> list[dict]:
    return [command for command in commands if command["status"] == "shipped"]


@pytest.fixture(scope="module")
def help_text() -> str:
    result = subprocess.run(["bash", str(ADP), "help"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    return result.stdout


def cli_files(script: Path) -> list[str]:
    match = re.search(r'^CLI_FILES="([^"]+)"', script.read_text(), re.MULTILINE)
    assert match
    return match.group(1).split()


def leaf(command: dict) -> str:
    return command["command"].split()[1]


def parser_leaves(helper: str, prefix: list[str]) -> dict[str, tuple[list[str], list[str]]]:
    path = CLI_DIR / helper
    name = helper.replace("-", "_").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(f"manifest_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CLI_DIR))
    spec.loader.exec_module(module)

    found = {}

    def walk(parser, words):
        subparsers = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        if subparsers:
            for action in subparsers:
                for choice, child in action.choices.items():
                    walk(child, [*words, choice])
            return
        flags, arguments = [], []
        for action in parser._actions:
            if action.dest == "help":
                continue
            if action.option_strings:
                flags.extend(action.option_strings)
            else:
                arguments.append(action.dest)
        found[" ".join(words)] = (sorted(flags), arguments)

    walk(module.parser(), prefix)
    return found


def shell_case_flags(script: Path, function: str) -> list[str]:
    body = script.read_text()
    match = re.search(rf"^{re.escape(function)}\(\) \{{\n(.*?)^\}}", body, re.MULTILINE | re.DOTALL)
    assert match, f"shell parser function {function} is missing"
    flags = {option for option in re.findall(r"^\s+(--[a-z][a-z-]*)(?:=\*)?\)", match.group(1), re.MULTILINE)}
    return sorted(flags)


# --- the manifest is well-formed --------------------------------------------


def test_every_entry_declares_the_full_contract(commands) -> None:
    for command in commands:
        missing = {
            "command",
            "helper",
            "status",
            "mutates",
            "operation",
            "flags",
            "arguments",
            "request",
            "response",
            "required_capabilities",
            "tests",
        } - set(command)
        assert not missing, f"{command.get('command')} omits {missing}"
        assert command["command"].startswith("adp ")
        assert command["status"] in {"shipped", "proposed"}
        assert isinstance(command["mutates"], bool), "mutation class must be explicit, never inferred"
        assert command["tests"], f"{command['command']} claims no owning test"


def test_entries_are_unique(commands) -> None:
    names = [command["command"] for command in commands]
    assert len(names) == len(set(names))


def test_python_manifest_is_one_row_per_real_parser_leaf(shipped) -> None:
    helper_prefixes = [
        ("adp-aws.py", ["adp", "aws"]),
        ("adp-bedrock.py", ["adp", "bedrock"]),
        ("adp-github.py", ["adp", "github"]),
        ("adp-superplane.py", ["adp", "superplane"]),
        ("adp-models.py", ["adp", "models"]),
        ("adp-flow.py", ["adp", "flow"]),
        ("adp-task.py", ["adp", "task"]),
        ("adp-agent.py", ["adp", "agent"]),
        ("adp-doctor.py", ["adp"]),
    ]
    checked_helpers = {helper for helper, _prefix in helper_prefixes} | {
        "adp-admin.py",
        "adp-github-admin.py",
    }
    manifested = {row["command"]: row for row in shipped if row["helper"] in checked_helpers}
    actual = {}
    for helper, prefix in helper_prefixes:
        actual.update(parser_leaves(helper, prefix))
    admin = parser_leaves("adp-admin.py", ["adp", "admin"])
    assert {"adp admin bedrock", "adp admin github"} <= set(admin)
    admin.pop("adp admin bedrock")
    admin.pop("adp admin github")
    actual.update(admin)
    actual.update(parser_leaves("adp-bedrock.py", ["adp", "admin", "bedrock"]))
    actual.update(parser_leaves("adp-github-admin.py", ["adp", "admin", "github"]))
    assert set(manifested) == set(actual)
    for command, (flags, arguments) in actual.items():
        assert manifested[command]["flags"] == flags, command
        assert manifested[command]["arguments"] == arguments, command


def test_shell_helper_flags_match_the_real_case_parsers(shipped) -> None:
    manifested = {row["command"]: row["flags"] for row in shipped}
    assert manifested["adp login"] == shell_case_flags(ADP, "cmd_login")
    assert manifested["adp update"] == shell_case_flags(ADP, "cmd_update")
    assert manifested["adp import"] == shell_case_flags(AUTH, "cmd_import")
    assert manifested["adp serve"] == shell_case_flags(AUTH, "cmd_serve")
    assert manifested["adp status"] == ["--json"]


def test_every_entry_declares_request_and_output_contract(commands) -> None:
    for row in commands:
        assert row["request"]["kind"] in {"local", "http"}
        assert isinstance(row["request"]["methods"], list)
        assert isinstance(row["request"]["paths"], list)
        if row["request"]["kind"] == "http":
            assert row["request"]["methods"], f"{row['command']} omits its HTTP method contract"
            assert row["request"]["paths"], f"{row['command']} omits its HTTP path contract"
            assert set(row["request"]["methods"]) <= {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            }
            assert all(path.startswith("/") for path in row["request"]["paths"])
        assert row["response"]["stream"] in {"single-json", "ndjson"}
        if row["command"] == "adp flow watch":
            assert row["response"]["stream"] == "ndjson"


def test_new_and_reclassified_commands_publish_their_real_wire_contract(
    commands,
) -> None:
    found = {row["command"]: row for row in commands}
    expected = {
        "adp capabilities": ({"GET"}, {"/me/cli-capabilities"}),
        "adp doctor": (
            {"GET"},
            {
                "/auth/me",
                "/me/budget",
                "/me/cli-capabilities",
                "/me/cli-requests/{request_id}",
                "/me/persona-models",
            },
        ),
        "adp aws verify": (
            {"GET", "POST"},
            {
                "/auth/credentials?scope=user",
                "/auth/credentials/aws/verify",
                "/me/cli-capabilities",
            },
        ),
        "adp bedrock verify": (
            {"GET", "POST"},
            {
                "/admin/bedrock-routing/destinations",
                "/admin/bedrock-routing/destinations/{destination_id}/verify",
                "/me/cli-capabilities",
            },
        ),
        "adp admin bedrock verify": (
            {"GET", "POST"},
            {
                "/admin/bedrock-routing/destinations",
                "/admin/bedrock-routing/destinations/{destination_id}/verify",
                "/me/cli-capabilities",
            },
        ),
        "adp admin github revalidate": (
            {"GET", "POST"},
            {"/admin/connections/github/app/revalidate", "/me/cli-capabilities"},
        ),
        "adp admin github status": (
            {"GET"},
            {"/admin/connections", "/admin/connections/github/app/status"},
        ),
    }
    for command, (methods, paths) in expected.items():
        assert set(found[command]["request"]["methods"]) == methods
        assert set(found[command]["request"]["paths"]) == paths


def test_schema_version_matches_the_server_contract(manifest) -> None:
    """One version covers the document and the manifest that interprets it."""
    from src.cli_capabilities import contract

    assert manifest["schema_version"] == contract.SCHEMA_VERSION


# --- manifest -> reality -----------------------------------------------------


def test_every_shipped_command_is_dispatchable(shipped) -> None:
    """The dispatcher must have an arm for each shipped leaf verb.

    Read out of the real `case` block rather than by running the command, so this
    stays a static check that needs no credentials and no gateway.
    """
    body = ADP.read_text()
    arms: set[str] = set()
    for match in re.finditer(r"\n\s{8}([a-z|\-]+)\)", body):
        arms.update(match.group(1).split("|"))
    for command in shipped:
        assert leaf(command) in arms, f"{command['command']} is in the manifest but has no dispatch arm"


def test_every_named_helper_ships(shipped) -> None:
    """A helper in the manifest must reach an installed machine.

    `adp` itself is the front door and is not in CLI_FILES as a helper; every
    other named file must be in both lists or the command breaks after install.
    """
    update_list, install_list = cli_files(ADP), cli_files(INSTALL)
    for command in shipped:
        helper = command["helper"]
        if helper == "adp":
            continue
        assert helper in update_list, f"{command['command']}: {helper} missing from adp's CLI_FILES"
        assert helper in install_list, f"{command['command']}: {helper} missing from install.sh's CLI_FILES"


def test_every_named_helper_exists_on_disk(commands) -> None:
    for command in commands:
        if command["status"] != "shipped":
            continue
        assert (CLI_DIR / command["helper"]).is_file(), f"{command['command']} names a helper that does not exist"


def test_every_python_helper_is_downloadable(shipped) -> None:
    """Served by the gateway, or `adp update` cannot deliver it."""
    from src.cli_download.routes import ALLOWED_SCRIPTS

    for command in shipped:
        helper = command["helper"]
        if not helper.endswith(".py"):
            continue
        assert helper in ALLOWED_SCRIPTS, f"{command['command']}: {helper} is not served"


def test_every_declared_operation_exists_in_the_server_contract(commands) -> None:
    """A capability ID must be real, or the CLI asks about an operation nobody serves.

    An empty operation is valid and means the command is purely local — that is a
    statement that there is nothing to discover, not a skipped field.
    """
    from src.cli_capabilities import contract

    for command in commands:
        for operation_id in command["required_capabilities"]:
            assert operation_id in contract.BY_ID, f"{command['command']} names unknown operation {operation_id!r}"


def test_mutation_class_agrees_with_the_server_contract(commands) -> None:
    """A read must not be backed by an operation the server marks as mutating.

    The CLI refuses unavailable mutations before sending; if a command's class
    disagreed with the contract's, that refusal would protect the wrong calls.
    The converse is allowed — a mutating command (`adp flow gate approve`) can
    legitimately hang off a read operation used for its discovery.
    """
    from src.cli_capabilities import contract

    for command in commands:
        operation_id = command["operation"]
        if not operation_id or command["mutates"]:
            continue
        assert not contract.BY_ID[operation_id].mutates, f"{command['command']} is declared read-only but its operation {operation_id!r} mutates"


# --- reality -> manifest -----------------------------------------------------


def test_every_shipped_command_is_advertised_in_help(shipped, help_text) -> None:
    """Help and the manifest must describe the same CLI."""
    for command in shipped:
        assert leaf(command) in help_text, f"{command['command']} ships but help never mentions it"


def test_help_advertises_nothing_the_manifest_omits(help_text, commands) -> None:
    """The direction that catches drift: a command grew, the manifest did not.

    Scoped to the indented `verb ...` lines of the help's command listing, and
    compared against manifest leaves — so a verb added to help without a manifest
    entry fails here instead of silently becoming undocumented surface.
    """
    known = {leaf(command) for command in commands}
    body = ADP.read_text()
    arms: set[str] = set()
    for match in re.finditer(r"\n\s{8}([a-z|\-]+)\)", body):
        arms.update(match.group(1).split("|"))
    # Only verbs that are BOTH dispatchable and advertised count as shipped
    # surface. Flags, prose and option names in the help text are not verbs.
    advertised = {match.group(1) for match in re.finditer(r"^\s{4}([a-z][a-z\-]*)\b", help_text, re.MULTILINE) if match.group(1) in arms}
    # Aliases the dispatcher accepts but which are not separate commands.
    aliases = {"logout", "token", "refresh", "import", "serve", "deployment", "help"}
    assert advertised - known - aliases == set(), f"advertised but absent from the manifest: {sorted(advertised - known - aliases)}"


def test_every_owning_test_file_exists(commands) -> None:
    """A manifest entry must not point at a test that was never written.

    This is what stops the manifest claiming coverage it does not have — the
    failure mode where `tests: [...]` is aspirational and nothing actually guards
    the command.
    """
    root = Path(__file__).parents[2]
    for command in commands:
        for test_path in command["tests"]:
            assert (root / test_path).is_file(), f"{command['command']} names missing test {test_path}"


def test_the_two_new_commands_are_recorded_as_read_only(commands) -> None:
    """Issue #5621's own commands: discovery and diagnosis never mutate."""
    found = {command["command"]: command for command in commands}
    for name in ("adp capabilities", "adp doctor"):
        assert found[name]["status"] == "shipped"
        assert found[name]["mutates"] is False
        assert found[name]["helper"] == "adp-doctor.py"


def test_manifest_itself_is_installed_updated_and_downloadable() -> None:
    from src.cli_download.routes import ALLOWED_SCRIPTS

    assert MANIFEST_PATH.name in cli_files(ADP)
    assert MANIFEST_PATH.name in cli_files(INSTALL)
    assert MANIFEST_PATH.name in ALLOWED_SCRIPTS


def test_http_mutations_have_capabilities_and_setup_declares_provider_variants(commands):
    for row in commands:
        if row["mutates"] and row["request"]["kind"] == "http":
            if row.get("capability_source") == "/activity/invocations/{invocation_id}/agent/state":
                assert row["command"] in {"adp agent " + action for action in ("pause", "resume", "steer", "abort")}
                assert row["helper"] == "adp-agent.py"
                assert not row["required_capabilities"]  # actual runtime capability, not invented global IDs
            elif row.get("authentication") == "task-service-principal":
                assert row["required_oauth_scopes"], row["command"]
                assert not row["required_capabilities"], "Task service tokens must not use human discovery"
            else:
                assert row["required_capabilities"], row["command"]
    setup = next(row for row in commands if row["command"] == "adp admin setup")
    assert {c for variant in setup["capability_variants"] for c in variant["required_capabilities"]} == {
        "routing.bedrock.write",
        "github.app.admin.setup.write",
    }


def test_workspace_use_declares_its_local_state_write(commands):
    row = next(row for row in commands if row["command"] == "adp superplane workspace use")
    assert row["mutates"] is True
    assert row["request"]["kind"] == "local"
    assert row["request"]["methods"] == row["request"]["paths"] == []
