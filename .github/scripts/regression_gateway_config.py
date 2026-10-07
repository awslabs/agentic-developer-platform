"""Resolve reviewed gateway test bindings without historical deployment defaults."""

import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


def resolve(raw, account):
    bindings = json.loads(raw)
    fields = {
        "gateway_url": "GATEWAY_URL",
        "api_gateway_url": "API_GATEWAY_URL",
        "m2m_secret_name": "GATEWAY_M2M_SECRET",
        "organization_id": "GATEWAY_TEST_ORG_ID",
        "organization_name": "GATEWAY_TEST_ORG_NAME",
    }
    if bindings.get("account_id") != account or not re.fullmatch(r"\d{12}", account):
        raise ValueError(
            "Gateway test bindings must match the authenticated AWS account"
        )
    result = {}
    for key, target in fields.items():
        value = bindings.get(key)
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
            raise ValueError(f"Missing or invalid gateway binding: {key}")
        result[target] = value
    for key in ("gateway_url", "api_gateway_url"):
        url = urlsplit(bindings[key])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                f"{key} must be an HTTPS service origin/path without credentials"
            )
    if urlsplit(bindings["gateway_url"]).path not in {"", "/"}:
        raise ValueError("gateway_url must be the CloudFront origin without /api")
    if not bindings["organization_name"].startswith("eval-regression-"):
        raise ValueError(
            "Gateway mutation fixtures must be an owned eval-regression-* organization"
        )
    result["GATEWAY_URL"] = bindings["gateway_url"].rstrip("/")
    result["CLOUDFRONT_DOMAIN"] = urlsplit(bindings["gateway_url"]).netloc
    return result


def main():
    raw = os.environ.get("GATEWAY_LIVE_TEST_BINDINGS_JSON", "").strip()
    if not raw:
        raise ValueError(
            "Configure GATEWAY_LIVE_TEST_BINDINGS_JSON in the protected checks environment"
        )
    account = subprocess.check_output(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        text=True,
    ).strip()
    values = resolve(raw, account)
    with Path(os.environ["GITHUB_ENV"]).open("a") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
