"""Obtain an engine-owned Product regression receipt from the generic delivery guard."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tests.e2e.product_onboarding.product_completion_regression import (
    engine_identity,
    engine_workflow,
    guard_engine,
)

AUDIENCE = "adp-operations-release-job-v1"
CHECK_PATH = "/orchestration/operations-release-job/check"
ROLE = re.compile(
    r"arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+\Z"
)
ENGINE_CORRELATION = re.compile(r"orch:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def _response(opener, request):
    with opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError("Product job authorization refused")
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("Product job authorization response is too large")
        return json.loads(
            payload,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON")),
        )


def authorize_engine(environment, destination, *, opener=None):
    document = engine_identity(environment)
    gateway = urlsplit(environment["ADP_GATEWAY_BASE_URL"])
    oidc = urlsplit(environment["ACTIONS_ID_TOKEN_REQUEST_URL"])
    role = environment["PRODUCT_DELIVERY_ROLE_ARN"]
    mutation_role = environment["PRODUCT_MUTATION_ROLE_ARN"]
    if (
        gateway.scheme != "https"
        or not gateway.hostname
        or gateway.username
        or gateway.password
        or gateway.path not in ("", "/", "/api", "/api/")
        or gateway.query
        or gateway.fragment
        or oidc.scheme != "https"
        or not oidc.hostname
        or not oidc.hostname.endswith(".actions.githubusercontent.com")
        or oidc.username
        or oidc.password
        or oidc.fragment
        or not ROLE.fullmatch(role)
        or not ROLE.fullmatch(mutation_role)
        or role.split(":")[4] != mutation_role.split(":")[4]
        or not ENGINE_CORRELATION.fullmatch(environment.get("ADP_CORRELATION", ""))
    ):
        raise ValueError("Product job authorization inputs are invalid")
    receipt = Path(destination)
    if (
        not receipt.is_absolute()
        or receipt.parent.is_symlink()
        or not receipt.parent.is_dir()
        or receipt.parent.stat().st_uid != os.getuid()
        or receipt.parent.stat().st_mode & 0o777 != 0o700
    ):
        raise ValueError("Product job authorization needs a private attempt directory")
    opener = opener or build_opener(NoRedirect())
    token_request = Request(
        environment["ACTIONS_ID_TOKEN_REQUEST_URL"]
        + ("&" if oidc.query else "?")
        + urlencode({"audience": AUDIENCE}),
        headers={
            "Authorization": "Bearer " + environment["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
        },
    )
    token_response = _response(opener, token_request)
    if not isinstance(token_response, dict) or set(token_response) != {"value"}:
        raise ValueError("GitHub OIDC response is invalid")
    token = token_response["value"]
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 8192
        or any(character.isspace() for character in token)
    ):
        raise ValueError("GitHub OIDC token is invalid")
    result = _response(
        opener,
        Request(
            environment["ADP_GATEWAY_BASE_URL"].rstrip("/")
            + ("" if gateway.path.rstrip("/") == "/api" else "/api")
            + CHECK_PATH,
            data=json.dumps(
                {
                    "correlation": environment["ADP_CORRELATION"],
                    "source_revision": environment["ADP_SOURCE_REVISION"],
                    "dispatch_revision": environment["ADP_DEFINITION_REVISION"],
                    "workflow": engine_workflow(environment) + ".yml",
                    "role_arn": role,
                    "mutation_role_arn": mutation_role,
                    "contract_digest": environment["ADP_CONTRACT_DIGEST"],
                }
            ).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            method="POST",
        ),
    )
    if result != {"authorized": True}:
        raise ValueError("Gateway did not authorize this Product job")
    descriptor = os.open(
        receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as output:
        json.dump(document, output, sort_keys=True)
    guard_engine(environment, receipt)


if __name__ == "__main__":
    authorize_engine(
        os.environ,
        os.environ["RUNNER_TEMP"]
        + "/" + engine_workflow(os.environ) + "/engine-authorization.json",
    )
