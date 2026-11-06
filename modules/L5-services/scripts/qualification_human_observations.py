"""Collect scoped human facts through the protected same-run gateway export.

This performs no UI action and issues no release pass. Fixtures select receipts;
only a fresh authenticated gateway response supplies collected human facts.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from release_context import NoRedirect, _response, release_identity

CONTRACT = "l5-human-observation-export/v1"
PATH = "/orchestration/operations-release-job/l5-observations/export"
SELECTION_FIELDS = {
    "product_id", "project_id", "service_id", "binding_id", "configuration_id",
    "configuration_revision", "check_id", "subject", "result_id", "result_revision", "result_run_id",
}
OBSERVATION_FIELDS = {
    "challenge_id", "snapshot_sha256", "release", "selection", "actor_id", "observed_at", "observed",
    "provenance", "decision_references", "release_check_completed", "native_qualified",
}
HEX = re.compile(r"[a-f0-9]{64}\Z")
UUID = re.compile(r"[a-f0-9-]{36}\Z")
_ISSUER = object()


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def reference(value):
    return (isinstance(value, dict) and set(value) == {"decision_id", "sha256"}
            and isinstance(value["decision_id"], str) and UUID.fullmatch(value["decision_id"])
            and isinstance(value["sha256"], str) and HEX.fullmatch(value["sha256"]))


@dataclass(frozen=True)
class CollectedHumanObservation:
    """Returned only after the collector calls the current protected export."""
    _document: str
    _issuer: object = field(repr=False, compare=False)

    @property
    def facts(self):
        if self._issuer is not _ISSUER:
            raise ValueError("Human facts were not collected by this protected reader")
        return json.loads(self._document)

    @property
    def sha256(self):
        if self._issuer is not _ISSUER:
            raise ValueError("Human facts were not collected by this protected reader")
        return sha256(self._document.encode()).hexdigest()



def request_export(environment, identity, selector, *, path, opener=None):
    gateway = urlsplit(environment["ADP_GATEWAY_BASE_URL"])
    oidc = urlsplit(environment["ACTIONS_ID_TOKEN_REQUEST_URL"])
    if (gateway.scheme != "https" or not gateway.hostname
            or gateway.username or gateway.password or gateway.query or gateway.fragment
            or gateway.path not in {"", "/", "/api", "/api/"}
            or oidc.scheme != "https" or not oidc.hostname or not oidc.hostname.endswith(".actions.githubusercontent.com")
            or oidc.username or oidc.password or oidc.fragment):
        raise ValueError("Human evidence endpoints are invalid")
    opener = opener or build_opener(NoRedirect())
    token_result = _response(opener, Request(
        environment["ACTIONS_ID_TOKEN_REQUEST_URL"] + ("&" if oidc.query else "?")
        + urlencode({"audience": "adp-operations-release-job-v1"}),
        headers={"Authorization": "Bearer " + environment["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    ))
    token = token_result.get("value") if isinstance(token_result, dict) else None
    if not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() for c in token):
        raise ValueError("Current workflow OIDC credential is invalid")
    return _response(opener, Request(
        environment["ADP_GATEWAY_BASE_URL"].rstrip("/")
        + ("" if gateway.path.rstrip("/") == "/api" else "/api") + path,
        data=json.dumps({**selector, "job": {
            "correlation": identity["correlation"], "source_revision": identity["source_revision"],
            "dispatch_revision": identity["definition_revision"], "workflow": "l5-services-release.yml",
            "role_arn": environment["L5_RELEASE_ROLE_ARN"], "mutation_role_arn": environment["L5_MUTATION_ROLE_ARN"],
            "contract_digest": identity["contract_digest"],
        }}).encode(), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST",
    ))

def collect(environment, selector, *, opener=None):
    identity = release_identity(environment)
    if not identity["correlation"] or not identity["contract_digest"]:
        raise ValueError("Human observations require the original engine-owned release")
    if (not isinstance(selector, dict) or set(selector) != {"challenge_id", "snapshot_sha256", "selection"}
            or not isinstance(selector["challenge_id"], str) or not UUID.fullmatch(selector["challenge_id"])
            or not isinstance(selector["snapshot_sha256"], str) or not HEX.fullmatch(selector["snapshot_sha256"])
            or not isinstance(selector["selection"], dict) or set(selector["selection"]) != SELECTION_FIELDS
            or selector["selection"].get("service_id") != identity["service"]):
        raise ValueError("Select the exact original human challenge and fixture scope, never fixture verdicts")
    document = request_export(environment, identity, selector, path=PATH, opener=opener)
    if (not isinstance(document, dict)
            or set(document) != {"contract", "observation", "export_receipt", "release_check_completed", "native_qualified"}
            or document["contract"] != CONTRACT
            or document["release_check_completed"] is not False or document["native_qualified"] is not False
            or not reference(document["export_receipt"])):
        raise ValueError("Gateway human evidence response is not a bounded partial export")
    facts = document["observation"]
    if (not isinstance(facts, dict) or set(facts) != OBSERVATION_FIELDS
            or facts["challenge_id"] != selector["challenge_id"] or facts["snapshot_sha256"] != selector["snapshot_sha256"]
            or facts["selection"] != selector["selection"]
            or facts["provenance"] != "authenticated-human-observation"
            or facts["release_check_completed"] is not False or facts["native_qualified"] is not False
            or not isinstance(facts["actor_id"], str) or not facts["actor_id"]
            or not isinstance(facts["decision_references"], list) or not 2 <= len(facts["decision_references"]) <= 8
            or any(not reference(item) for item in facts["decision_references"])
            or len({item["decision_id"] for item in facts["decision_references"]}) != len(facts["decision_references"])):
        raise ValueError("Export differs from the original human observation selection")
    expected = {key: identity[key] for key in (
        "source_revision", "run_id", "run_attempt", "correlation", "contract_digest",
    )}
    expected.update(repo=environment["GITHUB_REPOSITORY"], provider_repository_id=identity["repository_id"],
                    dispatch_revision=identity["definition_revision"], workflow=".github/workflows/l5-services-release.yml")
    release = facts["release"]
    # The export retains the original accepted workflow definition pin. The
    # journal's historical name `definition_revision` means the actual dispatched
    # GITHUB_WORKFLOW_SHA. A repaired main dispatch may differ from the accepted
    # pin: the Gateway validates that pin against the original accepted contract.
    if (not isinstance(release, dict) or release.get("provenance") != "oidc-authorized-delivery-dispatch-and-provider-run"
            or not isinstance(release.get("definition_revision"), str)
            or re.fullmatch(r"[a-f0-9]{40}", release["definition_revision"]) is None
            or any(release.get(key) != value for key, value in expected.items())):
        raise ValueError("Export belongs to another release run, source or accepted contract")
    observed = facts["observed"]
    if (not isinstance(observed, dict) or set(observed) - {"submitted_run_id"} != {
                "snapshot_sha256", "observation", "displayed_configuration_revision", "displayed_result_id",
                "displayed_result_revision", "displayed_native_hostname", "launch_receipt_id",
            } or observed.get("snapshot_sha256") != selector["snapshot_sha256"]
            or observed.get("observation") not in {"displayed", "denied", "mismatch", "submitted"}):
        raise ValueError("Export has no bounded human observation")
    submitted = observed.get("submitted_run_id")
    if (observed["observation"] == "submitted" and (
            selector["selection"]["service_id"] != "devops" or selector["selection"]["subject"] != "run_submission"
            or not isinstance(submitted, str) or not 1 <= len(submitted) <= 36)
            or observed["observation"] != "submitted" and submitted is not None):
        raise ValueError("Submission evidence needs its exact DevOps run and original human subject")
    return CollectedHumanObservation(json.dumps(document, sort_keys=True, separators=(",", ":")), _ISSUER)


def collect_into(journal, selectors, *, environment=None, opener=None):
    """Driver adapter: preserve fresh partial facts in the existing private journal."""
    environment = os.environ if environment is None else environment
    if journal.binding["context"] != release_identity(environment):
        raise ValueError("Human evidence collector and release journal identities differ")
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= 32:
        raise ValueError("Select a bounded list of original human observations")
    if len({item.get("challenge_id") for item in selectors if isinstance(item, dict)}) != len(selectors):
        raise ValueError("Human observation selection repeats a challenge")
    collected = []
    for selector in selectors:
        value = collect(environment, selector, opener=opener)
        journal.append("human-observation-export", {"response_sha256": value.sha256, "response": value.facts})
        collected.append(value)
    return tuple(collected)
