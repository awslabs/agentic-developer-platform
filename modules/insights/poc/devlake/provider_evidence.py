"""Check provider observations against independently selected current revisions."""

import hashlib
import json


PROVIDERS = frozenset(("sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear", "slack"))
REVISION_FIELDS = ("tenant_id", "project_id", "connection_id", "connection_revision",
                   "binding_id", "binding_revision", "source_generation", "record_id", "record_type",
                   "record_revision")


def selection_digest(selection, source_revision):
    if (not isinstance(source_revision, str) or not source_revision or
            not isinstance(selection, dict) or set(selection) != {"source_revision", "providers"} or
            selection["source_revision"] != source_revision or
            not isinstance(selection["providers"], dict) or set(selection["providers"]) != PROVIDERS):
        raise ValueError("seven current provider selections are required")
    for current in selection["providers"].values():
        if (not isinstance(current, dict) or set(current) != set(REVISION_FIELDS) or
                any(not isinstance(current.get(field), str) or not current[field]
                    for field in ("tenant_id", "project_id", "connection_id", "binding_id",
                                  "record_id", "record_type", "record_revision")) or
                any(type(current.get(field)) is not int or current[field] < 1
                    for field in ("connection_revision", "binding_revision", "source_generation"))):
            raise ValueError("current provider selection is invalid")
    return hashlib.sha256(json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify(evidence, selection, source_revision, run_id, run_attempt):
    digest = selection_digest(selection, source_revision)
    if not isinstance(evidence, dict) or set(evidence) != PROVIDERS:
        raise ValueError("seven current provider observations are required")
    for provider in PROVIDERS:
        current = selection["providers"][provider]
        observation = evidence[provider]
        if (not isinstance(observation, dict) or
                set(observation) != {"revision", "source_revision", "run_id", "run_attempt", "evidence_ref", "outcome"} or
                observation["revision"] != current or observation["source_revision"] != source_revision or
                observation["run_id"] != run_id or str(observation["run_attempt"]) != str(run_attempt) or
                type(observation["run_attempt"]) not in (str, int) or
                not isinstance(observation["evidence_ref"], str) or not observation["evidence_ref"] or
                observation["outcome"] != "pass"):
            raise ValueError(f"provider {provider} proof is missing, stale or unverified")
    return digest


def from_measurement(raw, reference, selection, source_revision, run_id, run_attempt):
    digest = selection_digest(selection, source_revision)
    try:
        measurement = json.loads(raw)
    except (TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("independent provider measurement is invalid") from error
    if (not isinstance(measurement, dict) or measurement.get("scope") != "live-source-measurement" or
            measurement.get("source_revision") != source_revision or
            measurement.get("provider_selection_digest") != digest or
            not isinstance(measurement.get("observations"), dict) or
            set(measurement["observations"]) != PROVIDERS | {"github", "jira", "profile"}):
        raise ValueError("independent provider measurement differs from selection")
    observations = measurement["observations"]
    for provider in PROVIDERS:
        observation = observations[provider]
        if (not isinstance(observation, dict) or observation.get("revision") != selection["providers"][provider] or
                any(not isinstance(observation.get(field), str) or len(observation[field]) != 64 or
                    any(character not in "0123456789abcdef" for character in observation[field])
                    for field in ("response_sha256", "binding_sha256"))):
            raise ValueError(f"provider {provider} measurement is missing or stale")
    if (not isinstance(observations["profile"], dict) or
            type(observations["profile"].get("revision")) is not int or
            observations["profile"]["revision"] < 1 or
            not isinstance(reference, str) or not reference.isdecimal()):
        raise ValueError("independent provider Profile readback is invalid")
    evidence = {provider: {"revision": selection["providers"][provider],
                           "source_revision": source_revision, "run_id": run_id,
                           "run_attempt": run_attempt, "evidence_ref": reference, "outcome": "pass"}
                for provider in PROVIDERS}
    verify(evidence, selection, source_revision, run_id, run_attempt)
    return evidence
