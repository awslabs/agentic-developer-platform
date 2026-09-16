#!/usr/bin/env python3
"""E14 on the instance: update, rollback, an interrupted install, and the tools.

This is the "can a user keep this CLI working?" case, and every assertion below
drives a real product command against a real installation:

- `adp update` re-pulls the release from the gateway that installed it and keeps
  the outgoing copies as `*.prev`. The proof is not that it printed success: the
  installed bytes must match the release manifest the orchestrator derived from
  the revision under test, and each `*.prev` must hold the bytes that were there
  before.
- `adp update --rollback` must put those copies back, and must REFUSE with its
  own message once there are none. A rollback that silently succeeded on an empty
  prefix is the dangerous outcome — the operator would believe they had returned
  to a known version while nothing was restored.
- An interrupted install must leave the working installation usable. The
  installer stages every file and commits only when all of them arrived, so this
  drives a genuinely failing install at the same prefix and asserts nothing
  changed.
- `adp codex setup` and `adp claude setup` write into files a user already owns
  (`~/.codex/config.toml`, `~/.claude/settings.json`). Both are seeded with
  foreign content first, because clobbering somebody else's configuration is the
  failure that matters, and both are run twice: the second run must be
  byte-identical, or "re-run setup" is not a safe instruction.
- Launching goes through the launchers themselves, with pinned provider releases,
  so argument forwarding is proved by the version the TOOL reports rather than by
  the wrapper's own output. A dead session must refuse to launch instead of
  dropping the user into a tool that will fail on its first request.

No AWS resource is created here, so this journey reports no resources: E14's
subject is the installation on disk and the two tools that consume it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import common
from common import require

# The installer does not install itself, so it is the one release file that is
# legitimately absent from a prefix. Asserted rather than assumed: if a future
# release did install it, the check below says so instead of silently skipping it.
NOT_INSTALLED = ("install.sh",)

# Appended to every installed file so "the version that was there before" is
# distinguishable from the release. A trailing comment leaves each file valid, so
# the installation stays usable and the rollback assertion is about exact bytes.
DRIFT_LINE = "# cli-uplift-eval previous version {marker}\n"

# The provider block `adp codex setup` owns, and the keys it must write.
CODEX_PROVIDER = "adp-gateway"

# Foreign Codex configuration. `[mcp_servers.local]` is what makes the ordering
# rule load-bearing: a top-level `model_provider` written after this header would
# silently become `mcp_servers.local.model_provider` and Codex would ignore it.
FOREIGN_CODEX = """approval_policy = "never"

[mcp_servers.local]
command = "echo"
args = ["kept"]
"""

FOREIGN_CODEX_KEYS = ("approval_policy", "[mcp_servers.local]", "command", "args")

# Foreign Claude settings, including an `env` entry that must survive the merge.
FOREIGN_CLAUDE = {
    "permissions": {"allow": ["Bash(ls)"]},
    "env": {"EVAL_FOREIGN_KEY": "kept"},
    "hooks": {"PreToolUse": []},
}


def _session(config, home):
    """The session install_auth established, in this journey's own HOME.

    `adp update` reads the gateway URL from this config file and the setup verbs
    refuse to run without a session, so both exist before anything is installed.
    The installer merges into `config.json` rather than replacing it, which is why
    writing it first is safe.
    """
    directory = home / ".bedrock-gateway"
    directory.mkdir(mode=0o700, exist_ok=True)
    config_file = directory / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "gateway_url": config["gateway_url"].rstrip("/"),
                "refresh_via": config.get("refresh_via", "gateway"),
            }
        )
    )
    config_file.chmod(0o600)
    tokens = directory / "tokens.json"
    tokens.write_text(
        json.dumps(
            {
                "access_token": config["access_token"],
                "id_token": config.get("id_token", ""),
                "refresh_token": config.get("refresh_token", ""),
                "expires_at": config["session_expires_at"],
            }
        )
    )
    tokens.chmod(0o600)
    return directory


def _installer(config, home, env, transcript):
    """Download the published installer into a directory of its own.

    Its own directory matters: `install.sh` copies from its own folder when it is
    run inside a checkout, so an installer sitting beside the CLI files would test
    a file copy instead of the gateway's download route.
    """
    directory = home / "installer"
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / "install.sh"
    argv = [
        "curl",
        "-fsS",
        "-o",
        str(target),
        config["gateway_url"].rstrip("/") + "/cli/install.sh",
    ]
    transcript.append(common.sanitize(argv))
    code, _out, _err = common.bounded(argv, env=env, timeout=120)
    require(code == 0, "The published install.sh could not be downloaded")
    return target


def _install(installer, prefix, gateway, env, transcript, *, expected=0):
    """Run the published installer against one prefix. Returns its exit code."""
    argv = [
        "sh",
        str(installer),
        "--prefix",
        str(prefix),
        "--gateway-url",
        gateway,
        "--no-path-edit",
    ]
    transcript.append(common.sanitize(argv))
    code, _out, _err = common.bounded(argv, env=env, timeout=300)
    if expected is not None:
        require(code == expected, f"install.sh exited {code}; expected {expected}")
    return code


def _installed_names(prefix, expected):
    """The release files that actually land in a prefix.

    Derived from the prefix rather than hardcoded, then checked against the one
    file the release is allowed to omit. A release that stopped installing a
    helper therefore fails here instead of quietly dropping out of every later
    comparison.
    """
    present = tuple(sorted(name for name in expected if (prefix / name).is_file()))
    absent = sorted(set(expected) - set(present))
    require(
        absent == sorted(NOT_INSTALLED),
        "The install placed an unexpected set of release files; absent: "
        + (", ".join(absent) or "none"),
    )
    return present


def _digests(prefix, names):
    return {
        name: hashlib.sha256((prefix / name).read_bytes()).hexdigest()
        for name in names
        if (prefix / name).is_file()
    }


def _drifted_from_release(prefix, names, expected):
    """Installed files whose bytes are not the release under test."""
    found = _digests(prefix, names)
    return sorted(name for name in names if found.get(name) != expected[name])


def _drift(prefix, names, marker):
    """Make the installed copies distinguishable from the release.

    This is what `*.prev` must contain after an update and what a rollback must
    restore. Nothing is removed and every file stays runnable, so a rollback that
    claims success can be checked against exact bytes.
    """
    for name in names:
        with (prefix / name).open("a") as handle:
            handle.write(DRIFT_LINE.format(marker=marker))
    return _digests(prefix, names)


def _usable(cli, prefix):
    """A usable installation: `adp version` runs from the installed prefix."""
    code, _payload = cli.run(["version"], expected=None, json_output=False)
    return code == 0 and os.access(prefix / "adp", os.X_OK)


def _previous_copies(prefix, names):
    return {
        name: hashlib.sha256((prefix / (name + ".prev")).read_bytes()).hexdigest()
        for name in names
        if (prefix / (name + ".prev")).is_file()
    }


def _update(config, evidence, cli, prefix, names, expected, marker):
    """`adp update` keeps the outgoing copies and lands the release."""
    evidence["stage"] = "update"
    drifted = _drift(prefix, names, marker)
    code, _payload = cli.run(["update"], expected=0, json_output=False)
    require(code == 0, "adp update failed")

    mismatched = _drifted_from_release(prefix, names, expected)
    previous = _previous_copies(prefix, names)
    absent_previous = sorted(set(names) - set(previous))
    wrong_previous = sorted(
        name for name, digest in previous.items() if digest != drifted[name]
    )
    evidence["update"] = {
        "installed_matches_release": not mismatched,
        "mismatched_after_update": mismatched,
        "previous_kept": sorted(previous),
        "previous_missing": absent_previous,
        "previous_wrong_bytes": wrong_previous,
        "usable": _usable(cli, prefix),
    }
    require(
        not mismatched,
        "adp update did not land the release under test: " + ", ".join(mismatched),
    )
    require(
        not wrong_previous,
        "adp update kept a .prev copy that is not the version it replaced: "
        + ", ".join(wrong_previous),
    )
    require(
        not absent_previous,
        "adp update kept no previous copy of: " + ", ".join(absent_previous),
    )
    require(evidence["update"]["usable"], "The installation is not usable after update")
    evidence["checks"].append("update_lands_release_and_keeps_previous")
    return drifted


def _rollback(evidence, cli, prefix, names, drifted, env):
    """`adp update --rollback` restores, then refuses when there is nothing to."""
    evidence["stage"] = "rollback"
    code, _payload = cli.run(["update", "--rollback"], expected=0, json_output=False)
    require(code == 0, "adp update --rollback failed")

    restored = _digests(prefix, names)
    unrestored = sorted(
        name for name, digest in drifted.items() if restored.get(name) != digest
    )
    leftovers = sorted(
        path.name for path in prefix.iterdir() if path.name.endswith(".prev")
    )

    # The negative: with every .prev consumed, a second rollback must fail and say
    # so. Driven through `bounded` rather than `Cli` because the assertion is on
    # the product's own message, which it prints to stderr.
    argv = [str(prefix / "adp"), "update", "--rollback"]
    evidence["transcript"].append(common.sanitize(argv))
    again, _out, err = common.bounded(argv, env=env, timeout=120)
    refused = again != 0 and "No previous version to roll back to." in (err or "")

    evidence["rollback"] = {
        "restored_previous_bytes": not unrestored,
        "unrestored": unrestored,
        "prev_files_left": leftovers,
        "second_rollback_exit_code": again,
        "second_rollback_refused_with_message": refused,
        "usable": _usable(cli, prefix),
    }
    require(
        not unrestored,
        "adp update --rollback did not restore: " + ", ".join(unrestored),
    )
    require(
        not leftovers,
        "adp update --rollback left .prev copies behind: " + ", ".join(leftovers),
    )
    require(
        refused,
        "A second adp update --rollback did not refuse with 'No previous version "
        "to roll back to.'; an operator could believe they had returned to a "
        "known version while nothing was restored",
    )
    require(
        evidence["rollback"]["usable"], "The installation is not usable after rollback"
    )
    evidence["checks"].append("rollback_restores_then_refuses_without_previous")


def _interrupted(config, evidence, cli, installer, prefix, names, expected, env):
    """A failing install must leave the working installation alone.

    The installer stages every file to a temp and commits only once all of them
    arrived, so this drives a genuinely failing install — a gateway path that
    serves no CLI files — against the same prefix and asserts nothing changed.
    Either outcome of that request is a refusal the guard must catch: a 404 fails
    `curl -f`, and an SPA HTML fallback with a 200 fails the shebang validation.
    """
    evidence["stage"] = "interrupted_install"
    before = _digests(prefix, names)
    absent = (
        config["gateway_url"].rstrip("/")
        + "/cli-uplift-eval-absent-"
        + str(config["evaluation_id"])
    )
    code = _install(
        installer, prefix, absent, env, evidence["transcript"], expected=None
    )
    temporaries = sorted(
        path.name
        for path in prefix.iterdir()
        if path.name.startswith(".") and ".tmp." in path.name
    )
    evidence["interrupted_install"] = {
        "installer_exit_code": code,
        "refused": code != 0,
        "installation_unchanged": _digests(prefix, names) == before,
        "still_matches_release": not _drifted_from_release(prefix, names, expected),
        "staged_temporaries_left": temporaries,
        "usable": _usable(cli, prefix),
    }
    report = evidence["interrupted_install"]
    require(
        report["refused"],
        "An install from a gateway path that serves no CLI reported success",
    )
    require(
        report["installation_unchanged"] and report["still_matches_release"],
        "A failed install changed the working installation",
    )
    require(
        not temporaries,
        "A failed install left staged temporaries behind: " + ", ".join(temporaries),
    )
    require(
        report["usable"],
        "The installation is not usable after an interrupted install",
    )
    evidence["checks"].append("interrupted_install_preserves_usable_installation")


def _codex_setup(evidence, cli, home, port):
    """`adp codex setup` must own its own keys and nothing else."""
    evidence["stage"] = "codex_setup"
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(FOREIGN_CODEX)

    code, _payload = cli.run(["codex", "setup"], expected=0, json_output=False)
    require(code == 0, "adp codex setup failed")
    text = path.read_text()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    require(lines, "adp codex setup wrote an empty Codex config")
    found = re.search(r'base_url = "http://127\.0\.0\.1:(\d+)/', text)
    preserved = sorted(
        key for key in FOREIGN_CODEX_KEYS if any(line.startswith(key) for line in lines)
    )

    # Byte-identical rerun, or "re-run setup" is not a safe instruction.
    cli.run(["codex", "setup"], expected=0, json_output=False)

    evidence["codex_setup"] = {
        "model_provider_is_top_level": lines[0]
        == f'model_provider = "{CODEX_PROVIDER}"',
        "provider_block_present": f"[model_providers.{CODEX_PROVIDER}]" in lines,
        "base_url_port": int(found.group(1)) if found else None,
        "wire_api_responses": 'wire_api = "responses"' in lines,
        "env_key_is_placeholder": 'env_key = "ADP_GATEWAY_DUMMY"' in lines,
        "foreign_keys_preserved": preserved,
        "rerun_byte_identical": path.read_text() == text,
    }
    report = evidence["codex_setup"]
    require(
        report["model_provider_is_top_level"],
        "model_provider is not the first key in the Codex config; written after a "
        "[section] header it would belong to that table and Codex would ignore it",
    )
    require(
        report["provider_block_present"]
        and report["wire_api_responses"]
        and report["env_key_is_placeholder"],
        "The Codex provider block does not name the ADP gateway proxy",
    )
    require(
        report["base_url_port"] == port,
        f"The Codex provider points at port {report['base_url_port']}, not the "
        f"proxy port {port} this session runs on",
    )
    require(
        preserved == sorted(FOREIGN_CODEX_KEYS),
        "adp codex setup did not preserve the user's own Codex configuration; kept: "
        + (", ".join(preserved) or "nothing"),
    )
    require(
        report["rerun_byte_identical"],
        "A second adp codex setup changed the file; the command is not idempotent",
    )
    evidence["checks"].append("codex_setup_preserves_foreign_config_and_reruns_clean")


def _claude_setup(config, evidence, cli, home, prefix):
    """`adp claude setup` must merge into settings a user already owns."""
    evidence["stage"] = "claude_setup"
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps(FOREIGN_CLAUDE))

    code, _payload = cli.run(["claude", "setup"], expected=0, json_output=False)
    require(code == 0, "adp claude setup failed")
    text = path.read_text()
    document = json.loads(text)
    env_block = document.get("env") or {}
    cli.run(["claude", "setup"], expected=0, json_output=False)

    evidence["claude_setup"] = {
        "api_key_helper_is_installed_absolute_path": document.get("apiKeyHelper")
        == f"{prefix / 'adp'} token",
        "api_key_helper_ttl_ms": document.get("apiKeyHelperTtlMs"),
        "bedrock_enabled": env_block.get("CLAUDE_CODE_USE_BEDROCK") == "1",
        "bedrock_auth_skipped": env_block.get("CLAUDE_CODE_SKIP_BEDROCK_AUTH") == "1",
        "base_url_is_gateway": env_block.get("ANTHROPIC_BEDROCK_BASE_URL")
        == config["gateway_url"].rstrip("/"),
        "foreign_settings_preserved": document.get("permissions")
        == FOREIGN_CLAUDE["permissions"]
        and document.get("hooks") == FOREIGN_CLAUDE["hooks"],
        "foreign_env_preserved": env_block.get("EVAL_FOREIGN_KEY") == "kept",
        "mode": oct(path.stat().st_mode & 0o777),
        "rerun_byte_identical": path.read_text() == text,
    }
    report = evidence["claude_setup"]
    require(
        report["api_key_helper_is_installed_absolute_path"],
        "apiKeyHelper is not the absolute path of the installed adp; Claude Code "
        "invokes it from a shell where the install directory is not on PATH",
    )
    require(
        report["bedrock_enabled"]
        and report["bedrock_auth_skipped"]
        and report["base_url_is_gateway"],
        "Claude settings do not route Bedrock traffic through this gateway",
    )
    require(
        report["foreign_settings_preserved"] and report["foreign_env_preserved"],
        "adp claude setup overwrote settings it does not own",
    )
    require(
        path.stat().st_mode & 0o077 == 0,
        f"Claude settings are not private to their owner ({report['mode']})",
    )
    require(
        report["rerun_byte_identical"],
        "A second adp claude setup changed the file; the command is not idempotent",
    )
    evidence["checks"].append("claude_setup_merges_and_reruns_clean")


def _provider_clis(config, evidence, home, env):
    """Install the pinned Claude Code and Codex releases for the launch checks.

    Pinned, into this journey's own prefix: the launchers are what E14 tests, and
    a provider release that moved under us would make a forwarding failure look
    like a launcher failure. The pinned version is also how forwarding is proved —
    `--version` reaching the tool is the TOOL answering, not the wrapper.
    """
    evidence["stage"] = "provider_clis"
    versions = {
        "claude": config.get("claude_version"),
        "codex": config.get("codex_version"),
    }
    require(
        all(versions.values()),
        "No pinned Claude/Codex versions were supplied; E14 cannot prove which "
        "release the launchers handed over to",
    )
    # `npm` is `npm-22` on the AL2023 nodejs22 package unless something linked it.
    npm = shutil.which("npm", path=env.get("PATH")) or shutil.which(
        "npm-22", path=env.get("PATH")
    )
    require(
        npm,
        "Neither npm nor npm-22 is on this instance; the pinned Claude/Codex "
        "releases cannot be installed and the launchers cannot be exercised",
    )
    prefix = home / "npm"
    argv = [
        npm,
        "install",
        "--silent",
        "--prefix",
        str(prefix),
        "--global",
        f"@anthropic-ai/claude-code@{versions['claude']}",
        f"@openai/codex@{versions['codex']}",
    ]
    evidence["transcript"].append(common.sanitize(argv))
    code, _out, _err = common.bounded(argv, env=env, timeout=900)
    require(
        code == 0,
        "The pinned Claude/Codex releases could not be installed on the instance",
    )
    binaries = prefix / "bin"
    missing = sorted(name for name in versions if not (binaries / name).exists())
    require(
        not missing,
        "The pinned provider install produced no launcher for: " + ", ".join(missing),
    )
    evidence["provider_clis"] = {"pinned": versions, "bin": str(binaries)}
    return binaries


def _launch(config, evidence, prefix, env, home):
    """The launchers: session preflight, proxy start, and argument forwarding."""
    evidence["stage"] = "launch"
    launched = {}
    for tool, version in (
        ("claude", config["claude_version"]),
        ("codex", config["codex_version"]),
    ):
        # `--` is the documented escape: everything after it belongs to the tool,
        # so this is the path a user's own flags take.
        argv = [str(prefix / "adp"), tool, "--", "--version"]
        evidence["transcript"].append(common.sanitize(argv))
        code, out, _err = common.bounded(argv, env=env, timeout=300)
        launched[tool] = {
            "exit_code": code,
            "forwarded_version_reported": str(version) in (out or ""),
        }
        require(code == 0, f"adp {tool} did not launch (exit {code})")
        require(
            launched[tool]["forwarded_version_reported"],
            f"adp {tool} did not forward --version to the pinned {tool} release; "
            "the launcher is not handing arguments to the tool",
        )

    # Codex's launcher starts the local auth proxy. It is a child of this journey,
    # so it must not outlive it — a lingering listener would hold the port against
    # a resumed attempt.
    pidfile = home / ".bedrock-gateway" / "proxy.pid"
    launched["codex"]["proxy_started"] = pidfile.is_file()
    require(
        launched["codex"]["proxy_started"],
        "adp codex launched without starting the local auth proxy; Codex would "
        "have had no credential to use",
    )
    launched["codex"]["proxy_stopped"] = common.stop_proxy(home)
    require(
        launched["codex"]["proxy_stopped"],
        "The auth proxy adp codex started could not be stopped; it would outlive "
        "this journey and hold its port",
    )

    # A dead session must refuse to launch rather than handing over to a tool that
    # will fail on its first request.
    dead = home / "dead-session" / ".bedrock-gateway"
    dead.mkdir(mode=0o700, parents=True, exist_ok=True)
    (dead / "config.json").write_text(
        json.dumps({"gateway_url": config["gateway_url"].rstrip("/")})
    )
    tokens = dead / "tokens.json"
    tokens.write_text(
        json.dumps({"access_token": "", "refresh_token": "expired", "expires_at": 0})
    )
    tokens.chmod(0o600)
    argv = [str(prefix / "adp"), "claude", "--", "--version"]
    evidence["transcript"].append(common.sanitize(argv) + "  # dead session")
    code, _out, err = common.bounded(
        argv, env={**env, "HOME": str(dead.parent)}, timeout=180
    )
    launched["dead_session_refused"] = code != 0 and "adp login" in (err or "")
    require(
        launched["dead_session_refused"],
        "A launcher with a dead session did not refuse; the user would have been "
        "dropped into a tool with no working credential",
    )
    evidence["launch"] = launched
    evidence["checks"].append("launchers_forward_arguments_and_refuse_dead_sessions")


def execute(config, evidence):
    os.umask(0o077)
    expected = config.get("expected_hashes") or {}
    require(
        expected,
        "No expected release hashes were supplied; an update could not be checked "
        "against the release under test",
    )
    require(
        config.get("access_token"),
        "update_rollback needs the session established by install_auth",
    )
    port = int(config.get("proxy_port") or 9191)

    with tempfile.TemporaryDirectory(prefix="adp-update-") as temporary:
        home = Path(temporary)
        prefix = home / ".adp" / "bin"
        env = common.clean_env(
            config,
            HOME=str(home),
            AWS_CONFIG_FILE=str(home / "aws-config"),
            AWS_SHARED_CREDENTIALS_FILE=str(home / "no-credentials"),
            ADP_PROXY_PORT=str(port),
        )
        _session(config, home)

        evidence["stage"] = "install"
        installer = _installer(config, home, env, evidence["transcript"])
        _install(
            installer,
            prefix,
            config["gateway_url"].rstrip("/"),
            env,
            evidence["transcript"],
        )
        cli = common.Cli(prefix / "adp", env, evidence["transcript"])
        names = _installed_names(prefix, expected)
        mismatched = _drifted_from_release(prefix, names, expected)
        require(
            not mismatched,
            "The baseline install does not match the release under test: "
            + ", ".join(mismatched),
        )
        require(_usable(cli, prefix), "The baseline install is not usable")
        evidence["baseline"] = {"installed": list(names), "usable": True}

        drifted = _update(
            config, evidence, cli, prefix, names, expected, config["evaluation_id"]
        )
        _rollback(evidence, cli, prefix, names, drifted, env)

        # Back to the release before the remaining checks: they must exercise the
        # revision under test, not the drifted copies the rollback restored.
        cli.run(["update"], expected=0, json_output=False)
        for name in names:
            previous = prefix / (name + ".prev")
            if previous.is_file():
                previous.unlink()
        require(
            not _drifted_from_release(prefix, names, expected),
            "The release could not be reinstalled after the rollback checks",
        )

        _interrupted(config, evidence, cli, installer, prefix, names, expected, env)

        # PATH carries the provider launchers only. `adp` is invoked by absolute
        # path throughout, so nothing here can pick up an unrelated copy.
        tools = _provider_clis(config, evidence, home, env)
        env = {**env, "PATH": f"{tools}:{env.get('PATH', '/usr/bin:/bin')}"}
        cli = common.Cli(prefix / "adp", env, evidence["transcript"])

        _codex_setup(evidence, cli, home, port)
        _claude_setup(config, evidence, cli, home, prefix)
        _launch(config, evidence, prefix, env, home)

        evidence["correlation"] = {
            "update_release_files": len(names),
            "update_proxy_port": port,
        }
        evidence["detail"] = {
            "update": evidence["update"],
            "rollback": evidence["rollback"],
            "interrupted_install": evidence["interrupted_install"],
            "codex_setup": evidence["codex_setup"],
            "claude_setup": evidence["claude_setup"],
            "launch": evidence["launch"],
            "checks": evidence["checks"],
        }
        # The provider install is hundreds of megabytes of node_modules in a temp
        # directory the instance will delete anyway; removed here so a resumed
        # attempt on the same instance is not competing for disk.
        shutil.rmtree(home / "npm", ignore_errors=True)
        evidence.update(stage="complete", success=True)


if __name__ == "__main__":
    import sys

    sys.exit(common.run_script(execute))
