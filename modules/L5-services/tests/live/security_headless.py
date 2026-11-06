"""Private Security configuration progress; never emits release qualification.

Only Product/API, CLI and MCP reads are performed. Provider execution, UI,
fault injection and publication acceptance remain independently unverified.
"""

import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from clients_setup import LiveSetupDriver


ROOT = Path(__file__).resolve().parents[4]
ACTIONS = ["configuration_readback", "tenant_isolation"]
PENDING = ["served_ui_setup", "native_assessments", "worker_recovery_cleanup", "context_reuse"]


def private_path(value, *, directory=False):
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or path.resolve().is_relative_to(ROOT)
            or path.resolve() != path or path.stat().st_mode & 0o077
            or (not path.is_dir() if directory else not path.is_file())):
        raise ValueError("use a private regular path outside the checkout without symlink redirection")
    return path


def load_fixture(filename, source_sha, *, now=None, kind="security_headless_progress"):
    fixture = json.loads(private_path(filename).read_text())
    now = now or datetime.now(UTC)
    authorization = fixture.get("authorization", {})
    expiry = datetime.fromisoformat(authorization["expires_at"].replace("Z", "+00:00"))
    if (fixture.get("schema_version") != 1 or fixture.get("kind") != kind
            or authorization.get("actions") != ACTIONS or authorization.get("source_sha") != source_sha
            or not re.fullmatch(r"[0-9a-f]{40}", source_sha) or not authorization.get("owner")
            or expiry.tzinfo is None or not now < expiry <= now + timedelta(hours=24)
            or type(fixture.get("deadline_seconds")) is not int
            or not 60 <= fixture["deadline_seconds"] <= 900):
        raise ValueError("headless readback requires a current, scoped authorization and reviewed source")
    cases = fixture["cases"]
    if len(cases) != 2:
        raise ValueError("headless isolation requires two distinct tenants and Products")
    from urllib.parse import urlsplit
    for case in cases:
        address = urlsplit(case["api_url"])
        if (address.scheme != "https" or not address.hostname or address.username or address.password
                or address.query or address.fragment or address.path not in ("", "/api")
                or not case.get("deployment_id") or not case.get("configuration_id")
                or type(case.get("configuration_revision")) is not int or case["configuration_revision"] < 1
                or set(case["scope"]) != {"tenant_id", "product_id", "project_id"}
                or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value) for value in case["scope"].values())):
            raise ValueError("headless readback requires exact Product/configuration/deployment selections")
        private_path(case["cli_home"], directory=True)
        for field in ("api_token_env", "mcp_token_env"):
            if not re.fullmatch(r"ADP_L5_FIXTURE_[A-Z0-9_]+", case[field]) or not os.environ.get(case[field]):
                raise ValueError("missing dedicated fixture session")
    for field in ("tenant_id", "product_id", "project_id"):
        if len({case["scope"][field] for case in cases}) != 2:
            raise ValueError("headless isolation requires distinct tenant, Product and Project scopes")
    if (len({case["api_url"] for case in cases}) != 1 or len({case["cli_home"] for case in cases}) != 2
            or any(len({os.environ[case[field]] for case in cases}) != 2
                   for field in ("api_token_env", "mcp_token_env"))):
        raise ValueError("headless cases require one deployment and separate client homes/sessions")
    return fixture


def compare(case, independent, cli, mcp):
    for field, value in case["scope"].items():
        if independent["scope"].get(field) != value:
            raise AssertionError("configuration scope differs from selected Product")
    if (independent["configuration_id"] != case["configuration_id"]
            or independent["revision"] != case["configuration_revision"]
            or independent["settings"].get("enabled") is not False):
        raise AssertionError("configuration identity/revision changed or provider execution is enabled")
    for client in (cli, mcp):
        for field in ("configuration_id", "revision", "scope", "settings", "state", "gaps", "hash"):
            if client[field] != independent[field]:
                raise AssertionError("CLI/MCP configuration differs from independent API readback")
    if cli["etag"] != f'"{independent["hash"]}"':
        raise AssertionError("CLI configuration ETag differs from independent API readback")


def verify_client_denials(foreign, cli, mcp):
    """Transport errors are not isolation evidence; reject resource disclosure."""
    cli_exit, cli_body = cli
    cli_error = cli_body.get("error") or {}
    mcp_status, mcp_body = mcp
    structured = mcp_body.get("structuredContent") or {}
    mcp_error = structured.get("error") or {}
    codes = {"not_found", "access_denied", "forbidden"}
    if (cli_exit not in {3, 5} or cli_body.get("status") != "failed" or cli_body.get("command") != "service"
            or set(cli_body) - {"status", "command", "detail", "next_action", "error"}
            or cli_body.get("detail") != {} or cli_error.get("code") not in codes
            or cli_error.get("http_status") not in {403, 404} or set(cli_error) - {"code", "message", "http_status"}):
        raise AssertionError("foreign CLI configuration read did not return an explicit authorization denial")
    if (mcp_status != 200 or mcp_body.get("isError") is not True
            or structured.get("http_status") not in {403, 404}
            or mcp_error.get("code") not in codes or set(structured) != {"http_status", "error"}
            or set(mcp_body) != {"isError", "structuredContent", "content"}
            or set(mcp_error) != {"code", "message", "retryable", "request_id"}
            or mcp_error["message"] not in {"Resource not found", "Request failed"}
            or mcp_error["retryable"] is not False):
        raise AssertionError("foreign MCP configuration read did not return an explicit authorization denial")
    content = mcp_body["content"]
    if (not isinstance(content, list) or len(content) != 1 or set(content[0]) != {"type", "text"}
            or content[0]["type"] != "text" or json.loads(content[0]["text"]) != structured):
        raise AssertionError("foreign MCP denial content differs from its structured authorization error")
    encoded = json.dumps([cli_body, mcp_body], sort_keys=True)
    if any(value in encoded for value in [*foreign["scope"].values(), foreign["configuration_id"]]):
        raise AssertionError("foreign CLI/MCP denial disclosed selected resource identifiers")
    return {"cli": cli_error["code"], "mcp": structured["http_status"]}


def inspect_case(case, foreign, driver):
    response = driver.api.get(driver.path + "/configuration", headers={"Cache-Control": "no-cache"},
                              timeout=driver.remaining())
    driver.remaining()
    response.raise_for_status()
    independent = response.json()
    cli = driver.cli_configuration()
    driver.remaining()
    mcp = driver.mcp_call("l5___get_configuration", driver._scope())
    driver.remaining()
    compare(case, independent, cli, mcp)
    # Confirm successful own-scope reads before interpreting a foreign denial.
    path = ("/v1/products/" + foreign["scope"]["product_id"] + "/projects/"
            + foreign["scope"]["project_id"] + "/services/"
            + getattr(driver, "service_id", "security") + "/configuration")
    response = driver.api.get(path, headers={"Cache-Control": "no-cache"}, timeout=driver.remaining())
    driver.remaining()
    payload = response.json()
    if (response.status_code not in (403, 404) or payload not in (
            {"detail": {"code": "not_found"}}, {"detail": {"code": "access_denied"}},
            {"detail": {"code": "forbidden"}})):
        raise AssertionError("foreign Product read did not return a nondisclosing denial")
    cli_denial = driver.denied_cli_configuration(foreign)
    driver.remaining()
    mcp_denial = driver.denied_mcp_configuration(foreign)
    driver.remaining()
    denied = verify_client_denials(foreign, cli_denial, mcp_denial)
    return {"configuration_id": independent["configuration_id"],
            "configuration_revision": independent["revision"], "scope": case["scope"],
            "configuration_gaps": independent["gaps"], "foreign_denial_status": response.status_code,
            "foreign_denials": {"api": response.status_code, **denied},
            "checks": ["api_cli_mcp_configuration_agree", "disabled_configuration", "api_cli_mcp_foreign_tenant_denied"]}


def run(fixture, *, driver_type=LiveSetupDriver):
    expiry = datetime.fromisoformat(fixture["authorization"]["expires_at"].replace("Z", "+00:00"))
    remaining = min(fixture["deadline_seconds"], (expiry - datetime.now(UTC)).total_seconds())
    overall_deadline = time.monotonic() + remaining
    observations = []
    for index, selected in enumerate(fixture["cases"]):
        if time.monotonic() >= overall_deadline:
            raise AssertionError("headless authorization or total deadline expired before the next Product")
        case = {**selected, "deadline_seconds": fixture["deadline_seconds"], "max_attempts": 0,
                "journal_file": str(Path(selected["cli_home"]) / "unused-headless-journal.jsonl")}
        driver = driver_type(case, None)
        try:
            driver.deadline = overall_deadline
            observations.append(inspect_case(case, fixture["cases"][1 - index], driver))
        finally:
            driver.close()
    return {"schema_version": 1, "kind": "security_headless_progress", "release_qualified": False,
            "source_sha": fixture["authorization"]["source_sha"], "observed_at": datetime.now(UTC).isoformat(),
            "completed_checks": ACTIONS, "pending_acceptance": PENDING, "observations": observations}


def main(arguments):
    if len(arguments) != 3:
        raise ValueError("usage: security_headless.py REVIEWED_SHA PRIVATE_FIXTURE PRIVATE_OUTPUT")
    source_sha, filename, destination = arguments
    output = Path(destination)
    private_path(str(output.parent), directory=True)
    fixture = load_fixture(filename, source_sha)
    # Reserve evidence before reading; interrupted attempts cannot overwrite evidence.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as target:
        result = run(fixture)
        json.dump(result, target, sort_keys=True)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    print("Security headless configuration checks passed; full release acceptance remains incomplete.")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        raise SystemExit("Security headless progress did not complete; retain private evidence and inspect selected configuration.") from None
