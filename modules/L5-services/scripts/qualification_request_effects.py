"""Fresh, same-run canonical effect export; no native or UI pass assertions."""

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
import re

from qualification_human_observations import HEX, UUID, SELECTION_FIELDS, reference, request_export
from release_context import release_identity

CONTRACT = "l5-request-effect-export/v1"
PATH = "/orchestration/operations-release-job/l5-observations/effects/export"
_ISSUER = object()


@dataclass(frozen=True)
class CollectedRequestEffect:
    _document: str
    _issuer: object = field(repr=False, compare=False)

    @property
    def facts(self):
        if self._issuer is not _ISSUER:
            raise ValueError("Request effects were not collected by this protected reader")
        return json.loads(self._document)

    @property
    def sha256(self):
        if self._issuer is not _ISSUER:
            raise ValueError("Request effects were not collected by this protected reader")
        return sha256(self._document.encode()).hexdigest()


def collect(environment, selector, *, opener=None):
    identity = release_identity(environment)
    if (not identity["correlation"] or not identity["contract_digest"]
            or not isinstance(selector, dict)
            or set(selector) != {"challenge_id", "snapshot_sha256", "selection", "operation", "request_sha256"}
            or not isinstance(selector["challenge_id"], str) or not UUID.fullmatch(selector["challenge_id"])
            or any(not isinstance(selector[key], str) or not HEX.fullmatch(selector[key])
                   for key in ("snapshot_sha256", "request_sha256"))
            or not isinstance(selector["selection"], dict) or set(selector["selection"]) != SELECTION_FIELDS
            or selector["selection"].get("service_id") != identity["service"]
            or selector["selection"].get("check_id") != "ui-cli-mcp-parity"
            or selector["operation"] not in {"save", "check", "replay", "submit"}
            or selector["selection"].get("subject") != ("result" if selector["operation"] == "replay" else "run_submission" if selector["operation"] == "submit" else "configuration")):
        raise ValueError("Select the exact original challenge and request effect; fixture verdicts are not evidence")
    document = request_export(environment, identity, selector, path=PATH, opener=opener)
    if (not isinstance(document, dict)
            or set(document) != {"contract", "effect", "export_receipt", "release_check_completed", "native_qualified"}
            or document["contract"] != CONTRACT or document["release_check_completed"] is not False
            or document["native_qualified"] is not False or not reference(document["export_receipt"])):
        raise ValueError("Gateway effect response is not a bounded partial export")
    facts = document["effect"]
    if (not isinstance(facts, dict) or set(facts) != {
                "challenge_id", "snapshot_sha256", "selection", "release", "operation", "request_sha256", "events",
                "outcome", "decision_references", "provenance", "release_check_completed", "native_qualified",
            } or any(facts[key] != selector[key] for key in selector)
            or facts["provenance"] != "authenticated-canonical-request-effect"
            or facts["release_check_completed"] is not False or facts["native_qualified"] is not False
            or not isinstance(facts["decision_references"], list) or not 2 <= len(facts["decision_references"]) <= 4
            or any(not reference(item) for item in facts["decision_references"])):
        raise ValueError("Export differs from the selected original request effect")
    expected = {key: identity[key] for key in ("source_revision", "run_id", "run_attempt", "correlation", "contract_digest")}
    expected.update(repo=environment["GITHUB_REPOSITORY"], provider_repository_id=identity["repository_id"],
                    dispatch_revision=identity["definition_revision"], workflow=".github/workflows/l5-services-release.yml")
    release = facts["release"]
    if (not isinstance(release, dict) or release.get("provenance") != "oidc-authorized-delivery-dispatch-and-provider-run"
            or not isinstance(release.get("definition_revision"), str)
            or re.fullmatch(r"[a-f0-9]{40}", release["definition_revision"]) is None
            or any(release.get(key) != value for key, value in expected.items())):
        raise ValueError("Effect belongs to another release run, source or accepted contract")
    events = facts["events"]
    if (not isinstance(events, list) or not 1 <= len(events) <= 2
            or [value.get("phase") for value in events if isinstance(value, dict)] != ["before", "after"][:len(events)]
            or facts["outcome"] not in {"returned", "returned_error", "unknown"}
            or facts["outcome"] != events[-1].get("outcome", "unknown")
            or any(event.get("contract") != "l5-request-effect/v1"
                   or event.get("challenge_id") != selector["challenge_id"]
                   or event.get("snapshot_sha256") != selector["snapshot_sha256"]
                   or event.get("operation") != selector["operation"]
                   or event.get("request_sha256") != selector["request_sha256"]
                   or event.get("provenance") != "authenticated-canonical-request-effect"
                   or event.get("release_check_completed") is not False or event.get("native_qualified") is not False
                   for event in events)):
        raise ValueError("Effect history is incomplete or inconsistent")
    return CollectedRequestEffect(json.dumps(document, sort_keys=True, separators=(",", ":")), _ISSUER)


def collect_into(journal, selectors, *, environment=None, opener=None):
    environment = os.environ if environment is None else environment
    if journal.binding["context"] != release_identity(environment):
        raise ValueError("Effect collector and release journal identities differ")
    if (not isinstance(selectors, list) or not 1 <= len(selectors) <= 32
            or len({item.get("challenge_id") for item in selectors if isinstance(item, dict)}) != len(selectors)):
        raise ValueError("Select a bounded list of distinct request effect challenges")
    values = []
    for selector in selectors:
        value = collect(environment, selector, opener=opener)
        journal.append("request-effect-export", {"response_sha256": value.sha256, "response": value.facts})
        values.append(value)
    return tuple(values)
