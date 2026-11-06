"""Check a D2 deployment job's bound target before mutation; not an operations guard."""

import json
import os
import re
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

AUDIENCE = "adp-deployment-credentials"
CONTEXT_PATH = "/orchestration/deployment-credentials/context"
TARGET_FIELDS = ("provider", "account_id", "region", "resource_kind", "resource_id")


class GuardRefusedError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def _response(opener, request):
    with opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise GuardRefusedError()
        data = response.read(65537)
        if len(data) > 65536:
            raise GuardRefusedError()
        return json.loads(data)


def check_job(environ, *, opener=None):
    gateway = environ["ADP_GATEWAY_BASE_URL"]
    oidc = environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    gateway_url, oidc_url = urlsplit(gateway), urlsplit(oidc)
    if (
        gateway_url.scheme != "https"
        or not gateway_url.hostname
        or gateway_url.username
        or gateway_url.password
        or gateway_url.path not in {"", "/"}
        or gateway_url.query
        or gateway_url.fragment
        or oidc_url.scheme != "https"
        or not oidc_url.hostname
        or not oidc_url.hostname.endswith(".actions.githubusercontent.com")
        or oidc_url.username
        or oidc_url.password
        or oidc_url.fragment
    ):
        raise GuardRefusedError()
    correlation = environ["ADP_DEPLOY_CORRELATION"]
    revision = environ["ADP_DEPLOY_SOURCE_REVISION"]
    inputs = json.loads(environ["ADP_DEPLOY_INPUTS_JSON"])
    expected_target = json.loads(environ["ADP_DEPLOY_TARGET_JSON"])
    if (
        not re.fullmatch(r"[0-9a-f]{64}", correlation)
        or not re.fullmatch(r"[0-9a-f]{40}", revision)
        or not isinstance(inputs, dict)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in inputs.items())
        or not isinstance(expected_target, dict)
        or set(expected_target) != set(TARGET_FIELDS)
        or any(not isinstance(expected_target[field], str) or not expected_target[field] for field in TARGET_FIELDS)
    ):
        raise GuardRefusedError()
    opener = opener or build_opener(NoRedirect())
    oidc_request = Request(
        oidc + ("&" if oidc_url.query else "?") + urlencode({"audience": AUDIENCE}),
        headers={"Authorization": "Bearer " + environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    )
    token = _response(opener, oidc_request)["value"]
    if not isinstance(token, str) or not token or len(token) > 8192 or any(character.isspace() for character in token):
        raise GuardRefusedError()
    context_request = Request(
        gateway.rstrip("/") + CONTEXT_PATH,
        data=json.dumps({"correlation": correlation, "source_revision": revision, "inputs": inputs}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    context = _response(opener, context_request)
    if (
        not isinstance(context, dict)
        or context.get("source_revision") != revision
        or context.get("inputs") != inputs
        or not isinstance(context.get("target"), dict)
        or any(context["target"].get(field) != expected_target[field] for field in TARGET_FIELDS)
    ):
        raise GuardRefusedError()


if __name__ == "__main__":
    try:
        check_job(os.environ)
    except Exception:
        print("deployment_job_guard_refused", file=sys.stderr)
        sys.exit(1)
