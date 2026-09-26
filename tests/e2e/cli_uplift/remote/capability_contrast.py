#!/usr/bin/env python3
"""E19: live capability contrast and bounded read-only diagnosis."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path

import common


def _write_session(home, gateway, session):
    directory = Path(home) / ".bedrock-gateway"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps({"gateway_url": gateway}))
    expires_at = int(session.get("expires_at") or 0)
    if not expires_at:
        expires_at = int(time.time()) + int(session.get("expires_in") or 3600)
    tokens = {
        "access_token": session.get("access_token", ""),
        "id_token": session.get("id_token", ""),
        "refresh_token": session.get("refresh_token", ""),
        "expires_at": expires_at,
    }
    (directory / "tokens.json").write_text(json.dumps(tokens))
    os.chmod(directory / "config.json", 0o600)
    os.chmod(directory / "tokens.json", 0o600)


def _operations(envelope):
    common.require(envelope.get("status") == "ok", "adp capabilities did not return ok")
    rows = (envelope.get("detail") or {}).get("operations") or []
    return {row.get("id"): row for row in rows if isinstance(row, dict)}


def _revision_matches(expected, observed):
    """Accept the exact SHA or a conventional 7+ hex prefix of that SHA."""
    expected = str(expected or "").strip().lower()
    observed = str(observed or "").strip().lower()
    return bool(
        len(expected) == 40
        and all(character in "0123456789abcdef" for character in expected)
        and (
            observed == expected
            or (
                7 <= len(observed) < len(expected)
                and all(character in "0123456789abcdef" for character in observed)
                and expected.startswith(observed)
            )
        )
    )


def _validate_release_evidence(config, version, capabilities):
    expected_version = str(config.get("expected_cli_version") or "").strip()
    common.require(
        expected_version, "No independently derived CLI version was supplied"
    )
    common.require(
        str(version or "").strip() == f"adp {expected_version}",
        f"The served CLI reported {str(version or '').strip()!r}, expected adp {expected_version}",
    )
    gateway = (capabilities.get("detail") or {}).get("gateway") or {}
    common.require(
        gateway.get("state") == "yes",
        "Capability discovery could not establish the running gateway release",
    )
    common.require(
        _revision_matches(config.get("expected_revision"), gateway.get("release")),
        "Capability discovery reported a gateway release that does not match the expected revision",
    )


def ordinary_session(config, contrast):
    fixture_config = {**config, "credential_secret": contrast["ordinary_fixture_name"]}
    fixture = common.fixture_secret(
        fixture_config, common.clean_env(config), "ordinary_session", default=None
    )
    if fixture is None:
        # Fixture authentication only; product operations below use the served CLI.
        fixture_env = common.clean_env(config)
        username = common.fixture_secret(
            fixture_config, fixture_env, "non_admin_username"
        )
        password = common.fixture_secret(
            fixture_config, fixture_env, "non_admin_password"
        )
        request = urllib.request.Request(
            config["gateway_url"].rstrip("/") + "/auth/cli/password",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                fixture = json.load(response)
        except Exception:
            raise common.RemoteError("Ordinary fixture authentication failed") from None
    common.require(
        isinstance(fixture, dict) and fixture.get("access_token"),
        "The ordinary fixture session is absent",
    )

    return fixture


def execute(config, evidence):
    contrast = config.get("capability_contrast") or {}
    common.require(contrast, "No capability_contrast fixture was supplied")
    common.require(config.get("cli_path"), "install_auth did not retain the served CLI")
    admin = common.session_tokens(config)
    fixture = ordinary_session(config, contrast)

    home = Path(tempfile.mkdtemp(prefix="adp-e18-"))
    common.require(config.get("org_id"), "Verified native tenant required")
    env = common.clean_env(config, HOME=str(home), ADP_TENANT=config["org_id"])
    env["BG_CONFIG_DIR"] = str(home / ".bedrock-gateway")
    for name in (
        "ADP_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "KIMI_HOME",
    ):
        directory = home / name
        directory.mkdir(mode=0o700)
        env[name] = str(directory)
    cli = common.Cli(
        config["cli_path"],
        env,
        evidence["transcript"],
        timeout=min(int(config.get("timeout_seconds", 240)), 300),
    )
    try:
        _write_session(home, config["gateway_url"], admin)
        version_argv = [config["cli_path"], "version"]
        evidence["transcript"].append(common.sanitize(version_argv))
        version_code, version, _version_error = common.bounded(
            version_argv, env=env, timeout=30
        )
        common.require(
            version_code == 0, "The freshly served CLI could not report its version"
        )
        admin_caps = cli.json(["capabilities", "--refresh"])
        _validate_release_evidence(config, version, admin_caps)
        admin_ops = _operations(admin_caps)
        disabled = admin_ops.get(contrast["disabled_operation"]) or {}
        enabled = admin_ops.get(contrast["enabled_operation"]) or {}
        admin_permission = admin_ops.get(contrast["denied_operation"]) or {}
        common.require(
            disabled.get("supported") == "yes" and disabled.get("enabled") == "no",
            "The configured disabled operation was not a supported disabled feature",
        )
        common.require(
            disabled.get("feature_flag") == contrast["disabled_feature"],
            "The disabled operation did not represent the configured feature",
        )
        common.require(
            enabled.get("supported") == "yes" and enabled.get("enabled") == "yes",
            "The configured enabled operation was not supported and enabled",
        )
        common.require(
            enabled.get("feature_flag") == contrast["enabled_feature"],
            "The enabled operation did not represent the configured feature",
        )
        common.require(
            admin_permission.get("permitted") == "yes",
            "The admin identity lacked the permission selected for contrast",
        )
        explanation_argv = [
            config["cli_path"],
            "capabilities",
            "--operation",
            contrast["disabled_operation"],
        ]
        evidence["transcript"].append(common.sanitize(explanation_argv))
        explanation_code, explanation, _explanation_error = common.bounded(
            explanation_argv, env=env, timeout=30
        )
        common.require(
            explanation_code == 0 and "switched off" in explanation.lower(),
            "The unavailable feature lacked an actionable explanation",
        )

        # Same deployment and HOME, different token: this is the cache-isolation proof.
        _write_session(home, config["gateway_url"], fixture)
        ordinary_caps = cli.json(["capabilities"])
        ordinary_ops = _operations(ordinary_caps)
        denied = ordinary_ops.get(contrast["denied_operation"]) or {}
        common.require(
            denied.get("permitted") == "no",
            "The ordinary identity was not denied the configured operation",
        )
        common.require(
            (ordinary_caps.get("detail") or {}).get("tenant")
            != (admin_caps.get("detail") or {}).get("tenant")
            or denied != admin_ops.get(contrast["denied_operation"]),
            "The ordinary call reused the admin capability document",
        )

        doctor = cli.json(
            ["doctor", "--checks", "auth,api,budget,models,agents"], expected=None
        )
        common.require(
            set((doctor.get("detail") or {}).get("checks") or {})
            == {"auth", "api", "budget", "models", "agents"},
            "Doctor omitted a bounded check",
        )
        absent = "00000000-0000-4000-8000-000000000000"
        foreign = contrast["foreign_request_id"]
        first = cli.json(["doctor", "--request-id", foreign], expected=4)
        second = cli.json(["doctor", "--request-id", absent], expected=4)
        common.require(
            first.get("error") == second.get("error"),
            "Foreign and absent request IDs were distinguishable",
        )

        evidence["checks"] = [
            "served_cli_version_recorded",
            "enabled_and_disabled_axes_contrasted",
            "ordinary_permission_denial_contrasted",
            "same_home_identity_cache_isolated",
            "doctor_bounded_read_set",
            "foreign_request_hidden_like_absent",
            "no_mutation_or_inference_command",
        ]
        evidence["detail"] = {
            "criterion": "CLI-08-AC-04",
            "cli_version": str(version or "").strip(),
            "expected_revision": config.get("expected_revision"),
            "gateway_release": (admin_caps.get("detail") or {}).get("gateway"),
            "admin_tenant": (admin_caps.get("detail") or {}).get("tenant"),
            "ordinary_tenant": (ordinary_caps.get("detail") or {}).get("tenant"),
            "disabled_operation": contrast["disabled_operation"],
            "enabled_operation": contrast["enabled_operation"],
            "denied_operation": contrast["denied_operation"],
            "request_ids": [foreign, absent],
            "unavailable_explanation": "switched off on this deployment",
            "cleanup_verified": False,
        }
    finally:
        shutil.rmtree(home)
    common.require(not home.exists(), "The E19 session HOME was not removed")
    evidence["detail"]["cleanup_verified"] = True
    evidence["success"] = True
    evidence["stage"] = "cleanup"


if __name__ == "__main__":
    raise SystemExit(common.run_script(execute))
