"""Require a fresh signed native release check immediately before each mutation."""

import json
import os
import re
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

AUDIENCE = "adp-operations-release-job-v1"
CHECK_PATH = "/orchestration/operations-release-job/check"


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
        value = json.loads(data)
        if not isinstance(value, dict):
            raise GuardRefusedError()
        return value


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
    correlation = environ["ADP_RELEASE_CORRELATION"]
    source = environ["ADP_RELEASE_SOURCE_REVISION"]
    dispatch = environ["ADP_RELEASE_DISPATCH_REVISION"]
    workflow = environ["ADP_RELEASE_WORKFLOW"]
    role_arn = environ["ADP_RELEASE_ROLE_ARN"]
    if (
        not re.fullmatch(r"orch:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", correlation)
        or not re.fullmatch(r"[0-9a-f]{40}", source)
        or not re.fullmatch(r"[0-9a-f]{40}", dispatch)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml", workflow)
        or not re.fullmatch(r"arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+", role_arn)
    ):
        raise GuardRefusedError()
    opener = opener or build_opener(NoRedirect())
    token_request = Request(
        oidc + ("&" if oidc_url.query else "?") + urlencode({"audience": AUDIENCE}),
        headers={"Authorization": "Bearer " + environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    )
    token = _response(opener, token_request).get("value")
    if not isinstance(token, str) or not token or len(token) > 8192 or any(character.isspace() for character in token):
        raise GuardRefusedError()
    result = _response(
        opener,
        Request(
            gateway.rstrip("/") + CHECK_PATH,
            data=json.dumps(
                {"correlation": correlation, "source_revision": source, "dispatch_revision": dispatch, "workflow": workflow, "role_arn": role_arn}
            ).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        ),
    )
    if result != {"authorized": True}:
        raise GuardRefusedError()


if __name__ == "__main__":
    try:
        check_job(os.environ)
    except Exception:
        print("operations_job_guard_refused", file=sys.stderr)
        sys.exit(1)
