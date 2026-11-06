"""Refuse adapter publication or mutation without current native release authority."""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

AUDIENCE = "adp-operations-release-job-v1"
WORKFLOW = "tools-platform-deploy.yml"
MAX_RESPONSE = 16_384
IDENTITY_TIMEOUT_SECONDS = 15
# The gateway verifies the workflow, current policy and scoped cloud authority.
# A valid check can exceed the identity provider's shorter request timeout.
AUTHORITY_TIMEOUT_SECONDS = 45


class GuardRefused(RuntimeError):
    pass


def require(condition):
    if not condition:
        raise GuardRefused("native_release_guard_refused")


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise GuardRefused("native_release_guard_refused")


def read_json(opener, url, *, headers, body=None, timeout=IDENTITY_TIMEOUT_SECONDS):
    request_headers = {**headers, "Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
        headers=request_headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            require(response.status == 200)
            payload = response.read(MAX_RESPONSE + 1)
        require(len(payload) <= MAX_RESPONSE)
        value = json.loads(payload)
        require(isinstance(value, dict))
        return value
    except (OSError, ValueError, UnicodeError, urllib.error.URLError):
        raise GuardRefused("native_release_guard_refused") from None


def authorize(
    environment, opener=None, *, publication_digest=None, settlement=False, workflow=WORKFLOW, transition=None
):
    require(workflow in {WORKFLOW, "gateway-deploy.yml"})
    require(workflow == WORKFLOW or (publication_digest is None and not settlement))
    require(not settlement or publication_digest is not None)
    require(transition is None or (workflow == WORKFLOW and publication_digest is None and not settlement))
    require(
        transition is None
        or (
            type(transition) is dict
            and set(transition) <= {"action", "plan", "observed"}
            and {"action", "plan"} <= set(transition)
        )
    )
    require(publication_digest is None or re.fullmatch(r"sha256:[0-9a-f]{64}", publication_digest))
    contract_digest = environment.get("ADP_IDENTITY_CONTRACT_DIGEST", "")
    require(not contract_digest or re.fullmatch(r"[0-9a-f]{64}", contract_digest))
    require(
        environment.get("GITHUB_ACTIONS") == "true"
        and environment.get("GITHUB_REF") == "refs/heads/main"
        and environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and environment.get("GITHUB_RUN_ATTEMPT") == "1"
    )
    source = environment.get("ADP_SOURCE_REVISION", "")
    definition = environment.get("ADP_DEFINITION_REVISION", "")
    correlation = environment.get("ADP_CORRELATION", "")
    role = environment.get("ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN", "")
    mutation_role = environment.get("ADP_TOOLS_DEPLOY_ROLE_ARN", "")
    require(
        re.fullmatch(r"[0-9a-f]{40}", source)
        and re.fullmatch(r"[0-9a-f]{40}", definition)
        and re.fullmatch(r"orch:[0-9a-f-]{36}", correlation)
        and re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+", role)
        and re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/adp-[a-z0-9-]+-trusted-deployment", mutation_role)
        and role.split(":")[4] == mutation_role.split(":")[4]
    )
    try:
        oidc = urllib.parse.urlsplit(environment.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""))
        gateway = urllib.parse.urlsplit(environment.get("ADP_TOOLS_EVAL_GATEWAY_URL", ""))
        oidc_port, gateway_port = oidc.port, gateway.port
    except ValueError:
        raise GuardRefused("native_release_guard_refused") from None
    secret = environment.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    require(
        oidc.scheme == "https"
        and bool(oidc.hostname)
        and oidc.hostname.endswith(".actions.githubusercontent.com")
        and oidc_port in (None, 443)
        and not oidc.username
        and not oidc.password
        and not oidc.fragment
        and 0 < len(secret) <= 8192
        and not any(character.isspace() for character in secret)
    )
    require(
        gateway.scheme == "https"
        and bool(gateway.hostname)
        and gateway.path in ("", "/")
        and not gateway.username
        and not gateway.password
        and not gateway.query
        and not gateway.fragment
        and gateway_port in (None, 443)
    )
    client = opener or urllib.request.build_opener(NoRedirects())
    oidc_query = [(key, value) for key, value in urllib.parse.parse_qsl(oidc.query) if key != "audience"]
    oidc_url = urllib.parse.urlunsplit(
        oidc._replace(query=urllib.parse.urlencode([*oidc_query, ("audience", AUDIENCE)]))
    )
    identity = read_json(client, oidc_url, headers={"Authorization": "Bearer " + secret}).get("value")
    require(
        isinstance(identity, str)
        and len(identity) <= 8192
        and re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", identity)
    )
    target = urllib.parse.urlunsplit(
        gateway._replace(
            path=(
                "/api/tools/adapter-release/transition"
                if transition is not None
                else (
                    "/api/orchestration/operations-release-job/publication-observed"
                    if settlement
                    else "/api/orchestration/operations-release-job/publication-intent"
                )
                if publication_digest is not None
                else "/api/orchestration/operations-release-job/check"
            )
        )
    )
    job = {
        "correlation": correlation,
        "source_revision": source,
        "dispatch_revision": definition,
        "workflow": workflow,
        "mutation_role_arn": mutation_role,
        "role_arn": role,
        **({"contract_digest": contract_digest} if contract_digest else {}),
        **({"image_digest": publication_digest} if publication_digest is not None else {}),
    }
    decision = read_json(
        client,
        target,
        headers={"Authorization": "Bearer " + identity},
        body=job if transition is None else {"job": job, **transition},
        timeout=AUTHORITY_TIMEOUT_SECONDS,
    )
    if transition is not None:
        require(set(decision) == {"state"} and (decision["state"] is None or type(decision["state"]) is dict))
        return decision["state"]
    require(
        decision
        == (
            {"settled": True}
            if settlement
            else {"recorded": True}
            if publication_digest is not None
            else {"authorized": True}
        )
    )


if __name__ == "__main__":
    try:
        authorize(os.environ)
    except GuardRefused:
        raise SystemExit("native_release_guard_refused") from None
