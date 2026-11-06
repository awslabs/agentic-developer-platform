"""Curated existing tests and explicit target checks for environment commissioning."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from tests.regression import catalog

DEFINITION = Path(__file__).with_name("environment-smoke.json")
SLUG = re.compile(r"[a-z][a-z0-9-]{0,62}")


def selection(value="", chat_enabled=False):
    definition = json.loads(DEFINITION.read_text())
    requested = (
        definition["default_modules"]
        if not value.strip()
        else list(definition["modules"])
        if value.strip().lower() == "all"
        else catalog.modules(value)
    )
    unsupported = set(requested) - definition["modules"].keys()
    if unsupported:
        raise ValueError(
            "No smoke group for modules: " + ", ".join(sorted(unsupported))
        )
    selected = sorted(set(requested) | set(definition["mandatory_modules"]))
    cli, browser = set(), set()
    rows = {}
    for mid in selected:
        row = definition["modules"][mid]
        cli.update(row.get("cli", []))
        browser.update(row.get("browser", []))
        browser.update(row.get("browser_on" if chat_enabled else "browser_off", []))
        rows[mid] = {"name": catalog.load()["modules"][mid]["name"], **row}
    return {
        "profile": "new-environment-smoke",
        "definition_version": definition["version"],
        "requested_modules": requested,
        "modules": selected,
        "scope": rows,
        "cli_cases": sorted(cli),
        "cli_scope": ",".join("case:" + case for case in sorted(cli)),
        "browser_selectors": sorted(browser),
        "cli_tags": {case: catalog.cli_tags(case) for case in sorted(cli)},
        "browser_tags": {
            node: catalog.pytest_tags(
                catalog.ROOT / node.split("::")[0], node.split("::", 1)[1]
            )
            for node in sorted(browser)
        },
        "coverage_claim": "Selected smoke journeys only, not full module/scenario acceptance. Empty tags mean catalogue mapping remains outstanding.",
    }


def target(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("Configure valid ENVIRONMENT_SMOKE_TARGET_JSON") from None
    keys = {
        "account_id",
        "region",
        "resource_environment",
        "gateway_url",
        "dashboard_url",
        "cognito_user_pool_id",
        "chat_enabled",
        "new_ui_enabled",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Smoke target must contain exactly the documented fields")
    for key in keys - {"chat_enabled", "new_ui_enabled"}:
        if (
            not isinstance(value[key], str)
            or not value[key]
            or any(c.isspace() for c in value[key])
        ):
            raise ValueError("Invalid smoke target field: " + key)
    if not re.fullmatch(r"\d{12}", value["account_id"]):
        raise ValueError("Invalid target account")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", value["region"]):
        raise ValueError("Invalid target region")
    if not SLUG.fullmatch(value["resource_environment"]):
        raise ValueError("Invalid resource environment")
    for key, path in (("gateway_url", "/api"), ("dashboard_url", "")):
        url = urlsplit(value[key])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path != path
            or url.port
        ):
            raise ValueError("Target requires HTTPS origin and gateway /api: " + key)
    if value["gateway_url"] != value["dashboard_url"] + "/api":
        raise ValueError("Smoke browser and CLI must use the same deployment origin")
    if not value["cognito_user_pool_id"].startswith(value["region"] + "_"):
        raise ValueError("Cognito pool region differs from target")
    if any(type(value[k]) is not bool for k in ("chat_enabled", "new_ui_enabled")):
        raise ValueError("Feature expectations must be booleans")
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate_context(value, env):
    if not SLUG.fullmatch(env.get("SMOKE_TARGET_ENVIRONMENT", "")):
        raise ValueError("Invalid protected target environment")
    if value["region"] != env.get("AWS_REGION") or value[
        "resource_environment"
    ] != env.get("ENVIRONMENT"):
        raise ValueError(
            "Requested region/resource environment differs from private target"
        )
    if not re.fullmatch(
        r"[a-f0-9]{40}", env.get("CLI_UPLIFT_EVAL_EXPECTED_REVISION", "")
    ):
        raise ValueError("A full lowercase deployed gateway SHA is required")
    digest = env.get("SMOKE_TARGET_DIGEST", "")
    if digest and digest != fingerprint(value):
        raise ValueError(
            "Target configuration changed or differs between protected environments"
        )


def validate_cli(value, cfg, private):
    # Never obtain network, account or fixture identities from public examples.
    required = {
        "platform_account",
        "region",
        "gateway_url",
        "cognito_user_pool_id",
        "gateway_deployment",
        "vpc_id",
        "private_subnet_id",
        "instance_security_group_id",
        "instance_profile",
        "credential_secret_name",
        "state_bucket",
        "evaluation_target",
    }
    if not isinstance(private, dict) or any(not private.get(k) for k in required):
        raise ValueError(
            "Private CLI bindings must explicitly supply all smoke identity/network/recovery fields"
        )
    for key, source in (
        ("platform_account", "account_id"),
        ("region", "region"),
        ("gateway_url", "gateway_url"),
        ("cognito_user_pool_id", "cognito_user_pool_id"),
    ):
        if cfg[key] != value[source]:
            raise ValueError("Effective CLI config differs from smoke target: " + key)


def write_json(name, value):
    path = Path("test-results") / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def output(**values):
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "snapshot", "cli-guard", "browser"])
    parser.add_argument("--modules", default=os.environ.get("SMOKE_MODULES", ""))
    args = parser.parse_args(argv)
    if args.command == "plan":
        plan = selection(args.modules)
        write_json("environment-smoke-plan.json", plan)
        print(json.dumps(plan, indent=2))
        return 0

    value = target(os.environ.get("ENVIRONMENT_SMOKE_TARGET_JSON", ""))
    validate_context(value, os.environ)
    if args.command in {"browser", "cli-guard"} and not os.environ.get(
        "SMOKE_TARGET_DIGEST"
    ):
        raise ValueError("Initial smoke target fingerprint is required")
    import boto3

    if boto3.client("sts").get_caller_identity()["Account"] != value["account_id"]:
        raise ValueError("Assumed role account differs from smoke target")
    plan = selection(args.modules, value["chat_enabled"])
    if args.command in {"snapshot", "cli-guard"}:
        from tests.e2e.cli_uplift import config, ports

        if not os.environ.get("CLI_UPLIFT_EVAL_GATEWAY_CATALOG", "").strip():
            raise ValueError("Private gateway catalog is required")
        cfg = config.from_environment(os.environ)
        validate_cli(
            value,
            cfg,
            json.loads(os.environ.get("CLI_UPLIFT_EVAL_BINDINGS_JSON", "{}")),
        )
        if args.command == "cli-guard":
            return 0
        from tests.e2e.cli_regression.prepare import snapshot

        transport = ports.default_ports(cfg)
        revision, _ = snapshot(cfg, transport["aws"], transport["http"])
        if revision != os.environ["CLI_UPLIFT_EVAL_EXPECTED_REVISION"]:
            raise ValueError("Deployed gateway revision differs from expected release")
        plan.update(
            target_digest=fingerprint(value),
            target_environment=os.environ["SMOKE_TARGET_ENVIRONMENT"],
            resource_environment=value["resource_environment"],
            region=value["region"],
            expected_revision=revision,
            chat_enabled=value["chat_enabled"],
            new_ui_enabled=value["new_ui_enabled"],
        )
        write_json("environment-smoke-plan.json", plan)
        output(
            revision=revision,
            target_digest=fingerprint(value),
            cli_scope=plan["cli_scope"],
        )
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write(
                    "## New environment E2E smoke\n\n| Module | Smoke scope |\n|---|---|\n"
                )
                for mid, row in plan["scope"].items():
                    summary.write(f"| {mid} — {row['name']} | {row['scope']} |\n")
                summary.write("\n" + plan["coverage_claim"] + "\n")
        return 0

    # Set target before importing existing browser helpers (module-level settings).
    os.environ.update(
        E2E_CLOUDFRONT_URL=value["dashboard_url"],
        E2E_CHAT_ENABLED="1",
        E2E_NEW_UI_ENABLED="1",
        E2E_CHAT_EXPECTED_FLAG="on" if value["chat_enabled"] else "off",
        E2E_NEW_UI_EXPECTED_FLAG="on" if value["new_ui_enabled"] else "off",
    )
    from tests.e2e.chat.helpers import fetch_test_credentials

    if fetch_test_credentials().cognito_user_pool_id != value["cognito_user_pool_id"]:
        raise ValueError("Browser fixture Cognito pool differs from smoke target")
    write_json("environment-smoke-browser-plan.json", plan)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *plan["browser_selectors"],
            "-p",
            "regression_pytest",
            "-v",
            "--tb=short",
            "--timeout=180",
            "--junitxml=test-results/environment-smoke-browser.xml",
        ],
        check=False,
    ).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
