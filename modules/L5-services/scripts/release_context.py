"""Validate shared L5 publication inputs and authenticate engine-owned attempts.

This is the artifact phase, not a live-qualification receipt. Service drivers
must independently qualify the runtime before publishing delivery evidence.
"""

import json
import os
import re
import subprocess
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


WORKFLOW = "l5-services-release.yml"
SHA = re.compile(r"[0-9a-f]{40}\Z")
ROLE = re.compile(r"arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+\Z")
TRANSPORT = ("ADP_CORRELATION", "ADP_SOURCE_REVISION", "ADP_DEFINITION_REVISION", "ADP_CONTRACT_DIGEST")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def release_identity(environment):
    operation = environment.get("L5_RELEASE_OPERATION", "release")
    if operation not in {"release", "install", "publish"}:
        raise ValueError("Select release, standalone installation or artifact publication")
    if operation != "release" and any(environment.get(key) for key in TRANSPORT):
        raise ValueError("Standalone installation or publication cannot use engine delivery authority or emit delivery evidence")
    service = environment.get("L5_SERVICE", "security").replace("-", "_")
    target = environment.get("L5_ENVIRONMENT")
    if service not in {"security", "devops", "finops", "transform", "well_architected"} or target not in {"dev", "test", "prod"}:
        raise ValueError("Unsupported L5 service or protected environment")
    repository = environment["GITHUB_REPOSITORY"]
    reference = f"{repository}/.github/workflows/{WORKFLOW}@refs/heads/main"
    if (environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or environment.get("GITHUB_REF") != "refs/heads/main"
            or environment.get("GITHUB_WORKFLOW_REF") != reference):
        raise ValueError("Release must use the repository's dispatched main workflow")
    for key in ("GITHUB_REPOSITORY_ID", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        if not re.fullmatch(r"[1-9][0-9]*", environment.get(key, "")):
            raise ValueError("Release provider identity is incomplete")
    present = [bool(environment.get(key)) for key in TRANSPORT]
    if any(present) and not all(present):
        raise ValueError("Engine transport inputs must be supplied together")
    engine = all(present)
    source = environment.get("ADP_SOURCE_REVISION") if engine else environment.get("REVIEWED_SOURCE_SHA", "")
    definition = environment.get("GITHUB_WORKFLOW_SHA", "")
    if (not SHA.fullmatch(source) or not SHA.fullmatch(definition)
            or source != environment.get("L5_CHECKOUT_REVISION")):
        raise ValueError("Release source does not match the immutable checkout")
    legacy_source = environment.get("REVIEWED_SOURCE_SHA")
    if legacy_source and legacy_source != source:
        raise ValueError("Conflicting reviewed and engine source revisions")
    if engine and (
            definition != environment["ADP_DEFINITION_REVISION"]
            or definition != environment.get("GITHUB_SHA")
            or not re.fullmatch(r"orch:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", environment["ADP_CORRELATION"])
            or not re.fullmatch(r"[0-9a-f]{64}", environment["ADP_CONTRACT_DIGEST"])):
        raise ValueError("Release differs from the engine's pinned dispatch")
    return {
        "schema_version": 1,
        "phase": "artifacts",
        "service": service,
        "environment": target,
        "repository_id": int(environment["GITHUB_REPOSITORY_ID"]),
        "run_id": int(environment["GITHUB_RUN_ID"]),
        "run_attempt": int(environment["GITHUB_RUN_ATTEMPT"]),
        "source_revision": source,
        "definition_revision": definition,
        "correlation": environment.get("ADP_CORRELATION") or None,
        "contract_digest": environment.get("ADP_CONTRACT_DIGEST") or None,
    }


def _response(opener, request):
    with opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError("Release authorization refused")
        data = response.read(65537)
        if len(data) > 65536:
            raise ValueError("Release authorization response exceeds its bound")
        return json.loads(data)


def authorize_engine(environment, identity, *, opener=None, target=False):
    # Do not allow callers to supply a less restrictive, precomputed identity.
    if identity != release_identity(environment):
        raise ValueError("Release identity changed before authorization")
    if identity["correlation"] is None:
        return None
    if target and identity["contract_digest"] is None:
        return None
    gateway = urlsplit(environment["ADP_GATEWAY_BASE_URL"])
    oidc = urlsplit(environment["ACTIONS_ID_TOKEN_REQUEST_URL"])
    role = environment["L5_RELEASE_ROLE_ARN"]
    mutation_role = environment.get("L5_MUTATION_ROLE_ARN") or None
    if (gateway.scheme != "https" or not gateway.hostname
            or gateway.username or gateway.password or gateway.query or gateway.fragment
            or gateway.path not in {"", "/", "/api", "/api/"}
            or oidc.scheme != "https" or not oidc.hostname
            or not oidc.hostname.endswith(".actions.githubusercontent.com")
            or oidc.username or oidc.password or oidc.fragment
            or not ROLE.fullmatch(role)
            or (mutation_role is not None and (
                not ROLE.fullmatch(mutation_role)
                or role.split(":")[4] != mutation_role.split(":")[4]))):
        raise ValueError("Release authorization endpoints or selected role are invalid")
    opener = opener or build_opener(NoRedirect())
    token_response = _response(opener, Request(
        environment["ACTIONS_ID_TOKEN_REQUEST_URL"]
        + ("&" if oidc.query else "?") + urlencode({"audience": "adp-operations-release-job-v1"}),
        headers={"Authorization": "Bearer " + environment["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    ))
    token = token_response.get("value") if isinstance(token_response, dict) else None
    if not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() for c in token):
        raise ValueError("Release OIDC credential is invalid")
    result = _response(opener, Request(
        environment["ADP_GATEWAY_BASE_URL"].rstrip("/")
        + ("" if gateway.path.rstrip("/") == "/api" else "/api")
        + "/orchestration/operations-release-job/" + ("target" if target else "check"),
        data=json.dumps({
            "correlation": identity["correlation"],
            "source_revision": identity["source_revision"],
            "dispatch_revision": identity["definition_revision"],
            "workflow": WORKFLOW,
            "role_arn": role,
            "contract_digest": identity["contract_digest"],
            "mutation_role_arn": mutation_role,
        }).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    ))
    fields = {"authorized", "target", "source_revision", "contract_digest", "run_id", "run_attempt"} if target else {"authorized"}
    if not isinstance(result, dict) or set(result) != fields or result["authorized"] is not True:
        raise ValueError("Engine did not authorize this L5 release attempt")
    if not target:
        return None
    selected = result["target"]
    if (result["source_revision"] != identity["source_revision"]
            or result["contract_digest"] != identity["contract_digest"]
            or result["run_id"] != str(identity["run_id"])
            or result["run_attempt"] != str(identity["run_attempt"])
            or not isinstance(selected, dict)
            or set(selected) != {"connection_id", "resource_kind", "resource_id", "account_id", "region"}
            or any(not isinstance(selected[key], str) for key in selected)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", selected["connection_id"])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", selected["resource_kind"])
            or not 0 < len(selected["resource_id"]) <= 512
            or any(ord(character) < 32 for character in selected["resource_id"])
            or not re.fullmatch(r"[0-9]{12}", selected["account_id"])
            or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]", selected["region"])):
        raise ValueError("Engine target does not match this L5 release attempt")
    return selected


def verify_principal(environment, principal):
    role = environment.get("L5_MUTATION_ROLE_ARN") or environment["L5_RELEASE_ROLE_ARN"]
    if not ROLE.fullmatch(role):
        raise ValueError("Publication requires an exact selected role")
    partition, account = role.split(":")[1], role.split(":")[4]
    prefix = f"arn:{partition}:sts::{account}:assumed-role/{role.rsplit('/', 1)[-1]}/"
    if principal.get("Account") != account or not principal.get("Arn", "").startswith(prefix):
        raise ValueError("Publication credential differs from the selected role")


if __name__ == "__main__":
    try:
        identity = release_identity(os.environ)
        authorize_engine(os.environ, identity)
        if sys.argv[1:] == ["--check-aws"]:
            principal = json.loads(subprocess.check_output(
                ["aws", "sts", "get-caller-identity", "--output", "json"], timeout=30,
            ))
            verify_principal(os.environ, principal)
        elif sys.argv[1:]:
            raise ValueError("Unknown release context option")
    except Exception:
        raise SystemExit("L5 release inputs or engine authorization were refused") from None
    # Provenance only: no token, fixture, target URL or live-acceptance assertion.
    json.dump(identity, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
