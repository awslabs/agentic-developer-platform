"""Load explicitly authorized, private targets for Security live selectors."""

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[4]


def load_live_fixture(path):
    supplied = Path(path)
    if not supplied.is_absolute() or supplied.is_relative_to(ROOT):
        raise ValueError("live fixture must be outside the checkout")
    path = supplied.resolve(strict=True)
    if path.is_relative_to(ROOT) or path.stat().st_mode & 0o077:
        raise ValueError("live fixture must be an access-controlled file outside the checkout")
    fixture = json.loads(path.read_text())
    if (fixture.get("schema_version") != 1 or fixture.get("authorized") is not True
            or not fixture.get("fixture_owner") or len(fixture.get("cases", [])) != 2
            or len({case["scope"]["tenant_id"] for case in fixture["cases"]}) != 2
            or len({case["scope"]["product_id"] for case in fixture["cases"]}) != 2):
        raise ValueError("live target has no owner, two-tenant fixtures or explicit authorization")
    if (len({case["cli_home"] for case in fixture["cases"]}) != 2
            or len({case["journal_file"] for case in fixture["cases"]}) != 2
            or any(len({case[name] for case in fixture["cases"]}) != 2 for name in (
                "api_token_env", "mcp_token_env", "ui_access_token_env", "ui_id_token_env"))):
        raise ValueError("tenant fixtures must use separate CLI profiles, journals and authorization")
    limits = fixture["limits"]
    spend = Decimal(str(limits.get("max_spend_usd", "0")))
    if (type(limits.get("max_attempts")) is not int or not 3 <= limits["max_attempts"] <= 10
            or type(limits.get("deadline_seconds")) is not int or not 60 <= limits["deadline_seconds"] <= 900
            or not spend.is_finite() or spend <= 0):
        raise ValueError("live setup requires current, bounded attempts, time and spending authority")
    for component in ("api", "cli", "ui", "mcp", "worker"):
        if not re.fullmatch(r"[0-9a-f]{40}", fixture.get("release", {}).get(component, "")):
            raise ValueError(f"live setup requires a pinned {component} revision")
    allocated = Decimal("0")
    for case in fixture["cases"]:
        allocation = Decimal(str(case.get("spend_limit_usd", "0")))
        if not allocation.is_finite() or allocation <= 0:
            raise ValueError("every live Product needs a positive spend allocation")
        allocated += allocation
        for address in (case["api_url"], case["ui_url"]):
            parsed = urlsplit(address)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise ValueError("live target URL is not a clean HTTPS origin")
        if (not all(re.fullmatch(r"[A-Za-z0-9_-]{16,128}", case.get(name, ""))
                    for name in ("cli_receipt", "ui_receipt", "mcp_receipt"))
                or not case.get("ui_reason") or not case.get("provider", {}).get("label")
                or not case.get("provider", {}).get("account_id")
                or not case.get("provider", {}).get("role_arn")
                or not case.get("run_input", {}).get("sources")
                or not case.get("deployment_id") or not case.get("api_url", "").startswith("https://")
                or not case.get("ui_url", "").startswith("https://")
                or not Path(case["cli_home"]).is_absolute()
                or Path(case["cli_home"]).is_relative_to(ROOT)
                or Path(case["cli_home"]).stat().st_mode & 0o077
                or not Path(case["cli_settings_file"]).is_absolute()
                or not Path(case["journal_file"]).is_absolute()
                or Path(case["journal_file"]).is_relative_to(ROOT)
                or Path(case["cli_settings_file"]).is_relative_to(ROOT)
                or Path(case["cli_settings_file"]).stat().st_mode & 0o077
                or case["selected"]["scope"] != case["scope"]
                or case["ui_region"] == case["cli_region"]):
            raise ValueError("incomplete or ambiguous live setup target")
        scope = case["scope"]
        selected = case["selected"]
        sources = selected["sources"]
        if (any(not scope.get(field) for field in ("tenant_id", "product_id", "project_id"))
                or not sources or case["run_input"].get("sources") != sources
                or case["run_input"].get("binding_id") != selected["binding_id"]
                or selected["provider_identity"].get("account_id") != case["provider"]["account_id"]
                or selected["provider_identity"].get("role_arn") != case["provider"]["role_arn"]
                or any(source.get("tenant_id") != scope["tenant_id"]
                       or source.get("project_id") != scope["project_id"]
                       or not source.get("record_revision") or source["record_revision"].lower() in {
                           "head", "latest", "null"} for source in sources)):
            raise ValueError("live selection does not pin authorized source, binding and provider identity")
        for name in ("api_token_env", "mcp_token_env", "ui_access_token_env", "ui_id_token_env"):
            if not os.environ.get(case[name]):
                raise ValueError(f"missing private authorization for {name}")
    if allocated > spend:
        raise ValueError("live Product allocations exceed authorized total spend")
    for name in ("api_token_env", "mcp_token_env", "ui_access_token_env", "ui_id_token_env"):
        if len({os.environ[case[name]] for case in fixture["cases"]}) != 2:
            raise ValueError("two tenants cannot reuse the same client authorization")
    return fixture
