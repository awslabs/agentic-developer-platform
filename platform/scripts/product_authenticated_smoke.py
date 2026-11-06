"""Authenticate the maintained test fixture and read the deployed Product API.

This preflight precedes the separately required Product lifecycle, denial,
recovery and installed-CLI acceptance. It never supplies their evidence.
"""

import json
import os
import re
import subprocess
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


def require(value, reason):
    if not value:
        raise ValueError(reason)


def aws(operation, payload, region):
    # The AWS CLI may read JSON input more than once. A stdin pipe is consumed
    # by the first read; a private temporary file is repeatable and keeps fixture
    # passwords out of process arguments. The context removes it on every exit.
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="adp-smoke-input-"
    ) as request:
        json.dump(payload, request)
        request.flush()
        result = subprocess.run(
            [
                "aws",
                *operation.split(),
                "--region",
                region,
                "--output",
                "json",
                "--cli-input-json",
                "file://" + request.name,
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
    require(result.returncode == 0, "Product smoke AWS operation failed")
    return json.loads(result.stdout)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def get(url, token):
    request = Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            payload = response.read(65537)
            require(len(payload) <= 65536, "Product smoke response exceeds its bound")
            return response.status, json.loads(payload)
    except HTTPError as error:
        return error.code, {}


def check(environment, account, fixture_arn, *, invoke=aws, read=get):
    require(environment == "dev", "Product smoke is qualified only for dev")
    require(re.fullmatch(r"[0-9]{12}", account), "Invalid Product smoke account")
    region = "us-east-1"
    require(
        re.fullmatch(
            rf"arn:aws:secretsmanager:{region}:{account}:secret:adp/{environment}/gateway/test-admin-credentials-[A-Za-z0-9]{{6}}",
            fixture_arn,
        ),
        "Product smoke requires the exact protected test fixture",
    )
    identity = invoke("sts get-caller-identity", {}, region)
    require(
        identity.get("Account") == account
        and re.fullmatch(
            rf"arn:aws:sts::{account}:assumed-role/adp-{environment}-trusted-checks/[^/]+",
            identity.get("Arn", ""),
        ),
        "Product smoke is not using the selected checks identity",
    )
    values = {}
    for key in ("cognito-user-pool-id", "cognito-client-id", "cloudfront-domain"):
        values[key] = invoke(
            "ssm get-parameter",
            {
                "Name": f"/adp/{environment}/gateway/{key}",
            },
            region,
        )["Parameter"]["Value"]
    domain = values["cloudfront-domain"]
    require(
        re.fullmatch(r"[a-z0-9]+\.cloudfront\.net", domain),
        "Invalid Product smoke origin",
    )
    secret = invoke(
        "secretsmanager get-secret-value", {"SecretId": fixture_arn}, region
    )["SecretString"]
    require(len(secret) <= 65536, "Product smoke fixture exceeds its bound")
    fixture = json.loads(secret)
    require(
        fixture.get("cognito_user_pool_id") == values["cognito-user-pool-id"]
        and fixture.get("cognito_client_id") == values["cognito-client-id"],
        "Product smoke fixture differs from the selected Cognito target",
    )
    require(
        all(
            isinstance(fixture.get(key), str) and fixture[key]
            for key in ("username", "password")
        ),
        "Product smoke fixture credentials are missing",
    )
    result = invoke(
        "cognito-idp admin-initiate-auth",
        {
            "UserPoolId": values["cognito-user-pool-id"],
            "ClientId": values["cognito-client-id"],
            "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
            "AuthParameters": {
                "USERNAME": fixture["username"],
                "PASSWORD": fixture["password"],
            },
        },
        region,
    )
    token = result.get("AuthenticationResult", {}).get("AccessToken")
    require(
        isinstance(token, str) and bool(token),
        "Product smoke did not obtain an access token",
    )
    status, registered = read(f"https://{domain}/api/access/status", token)
    require(
        status == 200
        and isinstance(registered, dict)
        and registered.get("status") == "registered",
        "Product smoke login is not registered",
    )
    status, products = read(f"https://{domain}/api/v1/products?limit=1", token)
    require(
        status == 200
        and isinstance(products, dict)
        and set(products) == {"items", "next_cursor"}
        and isinstance(products["items"], list)
        and len(products["items"]) <= 1
        and (
            products["next_cursor"] is None or isinstance(products["next_cursor"], str)
        ),
        "Product smoke did not read a bounded Product collection",
    )
    for item in products["items"]:
        require(
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and re.fullmatch(r"[a-f0-9-]{36}", item["id"])
            and type(item.get("revision")) is int
            and item["revision"] > 0
            and isinstance(item.get("project_ids"), list),
            "Product smoke returned an invalid Product",
        )
    return {"authenticated_product_smoke": "passed", "acceptance_evidence": False}


if __name__ == "__main__":
    try:
        print(
            json.dumps(
                check(
                    os.environ["ENVIRONMENT"],
                    os.environ["ACCOUNT_ID"],
                    os.environ["ADP_PRODUCT_SMOKE_FIXTURE_ARN"],
                )
            )
        )
    except Exception:
        # AWS/auth responses and fixture credentials must not be emitted to CI.
        raise SystemExit(
            "Authenticated Product smoke failed; no Product acceptance is established."
        ) from None
