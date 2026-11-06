"""Evaluate original Transform criteria from verifier-issued, attempt-bound facts."""

from dataclasses import dataclass, field
from pathlib import Path
import json

from qualification_transform_journal import digest

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "docs/l5-services/transform/acceptance-catalog.json"
SCOPE = "transform_release_acceptance"
NEXT = {
    "EC-01": "Collect installed compatibility, qualified provider/executor and ready/missing-input Product configuration observations.",
    "EC-02": "Collect scoped client/broker/source denials and exact installed executor isolation controls.",
    "EC-03": "Independently verify both selected native refactor and dependency-upgrade results, original patches and detached tests.",
    "EC-04": "Collect original-attempt lost-ack/restart/replay/stale/cancel traces and independent native-effect counts.",
    "EC-05": "Read retained patch/tests after receipt-owned termination and cleanup; verify adopted resources and SCM remain unchanged.",
    "EC-06": "Collect installed UI/CLI/MCP journeys, one recurring occurrence and separate SCM authorization. UI needs authenticated human observation; no browser automation.",
    "EC-07": "Collect both consumer acknowledgements, later entitled task use and foreign/revoked reuse denials.",
    "EC-08": "Verify two distinct non-ADP projects on unchanged artifacts with independent deployment, retention, cleanup and actual/unknown cost readbacks.",
}


def catalog():
    value = json.loads(CATALOG.read_text())
    return value, "sha256:" + digest(value)


@dataclass(frozen=True)
class VerifiedEvidence:
    """Issued in memory after a collector executes its independent verifier.

    This is not an input/document schema. Fixtures and serialized reports cannot
    instantiate evidence. Emission re-runs collectors rather than trusting an old
    result's pass flag.
    """
    identifier: str
    binding_sha256: str
    receipt_sha256: str
    facts_sha256: str
    _issuer: object = field(repr=False, compare=False)


class EvidenceSet:
    def __init__(self, journal):
        self.journal = journal
        self._issuer = object()
        self._verified = {}
        self.progress = []

    def _collected(self, identifier, facts):
        if identifier in self._verified:
            raise ValueError("duplicate Transform criterion observation")
        allowed = set(NEXT) | {item["id"] for item in catalog()[0]["live_checks"]}
        if identifier not in allowed:
            raise ValueError("unknown Transform criterion")
        value = {"criterion": identifier, "facts_sha256": digest(facts), "facts": facts}
        receipt = self.journal.append("verified-observation", value)
        record = VerifiedEvidence(identifier, self.journal.binding_sha256, receipt, value["facts_sha256"], self._issuer)
        self._verified[identifier] = record
        return record

    def verified(self):
        for key, record in self._verified.items():
            if (type(record) is not VerifiedEvidence or record._issuer is not self._issuer
                    or record.identifier != key or record.binding_sha256 != self.journal.binding_sha256
                    or not any(digest(row) == record.receipt_sha256 and row["kind"] == "verified-observation"
                               and row["value"].get("criterion") == key
                               and row["value"].get("facts_sha256") == record.facts_sha256
                               and digest(row["value"].get("facts")) == record.facts_sha256
                               for row in self.journal.records)):
                raise ValueError("Transform facts are not issued by this collector/attempt")
        return dict(self._verified)


def evaluate(evidence):
    if type(evidence) is not EvidenceSet:
        raise ValueError("Transform acceptance requires collected facts, not fixture verdicts")
    known = evidence.verified()
    original, revision = catalog()
    context = evidence.journal.binding["context"]
    epics = set(original["epic_criteria"]) & set(known)

    def row(identifier, dependencies):
        complete = set(dependencies) <= epics
        receipts = [known[item].receipt_sha256 for item in dependencies if item in known]
        if identifier in known:
            complete = True
            receipts = [known[identifier].receipt_sha256]
        return {"id": identifier, "outcome": "passed" if complete else "incomplete",
                "evidence_verified": complete, "observation": "independent-live",
                "verification_layer": "installed-product-and-native",
                "evidence_ref": "sha256:" + digest(receipts),
                "collector_receipts": receipts}

    story_rows = [row(item["id"], item["epics"]) for item in original["story_criteria"]]
    scenario_rows = [row(name, item["epics"]) for item in original["story_criteria"] for name in item["scenarios"]]
    checks = [row(item["id"], item["epics"]) for item in original["live_checks"]]
    missing = sorted(set(original["epic_criteria"]) - epics)
    return {
        "schema_version": 1, **{key: context[key] for key in (
            "source_revision", "definition_revision", "repository_id", "run_id", "run_attempt", "contract_digest")},
        "workflow_path": ".github/workflows/l5-services-release.yml", "correlation_id": context["correlation"],
        "requirements_revision": revision, "qualification_scope": SCOPE,
        "release_context": context,
        "fixture_sha256": evidence.journal.binding["fixture_sha256"],
        "binding_sha256": evidence.journal.binding_sha256,
        "observed_criteria": sorted(known),
        "collector_receipts": {key: {"receipt_sha256": value.receipt_sha256, "facts_sha256": value.facts_sha256}
                               for key, value in known.items()},
        "epic_criteria": [row(name, [name]) for name in original["epic_criteria"]],
        "story_criteria": story_rows, "scenarios": scenario_rows, "live_checks": checks,
        "cleanup": {"outcome": "passed" if "EC-05" in epics else "incomplete"},
        "unresolved": [{"code": name + ":not-observed", "next_action": NEXT[name]} for name in missing],
        "progress": list(evidence.progress),
    }


def validate_catalog(report):
    """The publisher refuses incomplete catalogs and internally inconsistent pass claims."""
    original, revision = catalog()
    expected = {"epic_criteria": {name: [name] for name in original["epic_criteria"]},
                "story_criteria": {item["id"]: item["epics"] for item in original["story_criteria"]},
                "scenarios": {name: item["epics"] for item in original["story_criteria"] for name in item["scenarios"]},
                "live_checks": {item["id"]: item["epics"] for item in original["live_checks"]}}
    if report.get("qualification_scope") != SCOPE or report.get("requirements_revision") != revision:
        raise ValueError("Transform report does not use the original acceptance catalog")
    observed = report.get("observed_criteria")
    receipts = report.get("collector_receipts")
    import re
    context = report.get("release_context")
    if (not isinstance(context, dict) or context.get("service") != "transform"
            or any(report.get(key) != context.get(key) for key in (
                "source_revision", "definition_revision", "repository_id", "run_id", "run_attempt", "contract_digest"))
            or report.get("correlation_id") != context.get("correlation")
            or not isinstance(report.get("fixture_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", report["fixture_sha256"])
            or report.get("binding_sha256") != digest({"context": context,
                "fixture_sha256": report["fixture_sha256"], "requirements_revision": revision})):
        raise ValueError("Transform acceptance is not bound to its original release attempt and fixture")
    if (not isinstance(observed, list) or any(not isinstance(item, str) for item in observed)
            or len(observed) != len(set(observed))
            or not set(observed) <= set(expected["epic_criteria"]) | set(expected["live_checks"])
            or not isinstance(receipts, dict) or set(receipts) != set(observed)
            or any(not isinstance(item, dict) or set(item) != {"receipt_sha256", "facts_sha256"}
                   or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                          for value in item.values()) for item in receipts.values())):
        raise ValueError("Transform report has no bounded collector provenance")
    if len({value["receipt_sha256"] for value in receipts.values()}) != len(receipts):
        raise ValueError("Transform criteria reused a collector receipt")
    for section, dependencies in expected.items():
        rows = report.get(section)
        if (not isinstance(rows, list) or len(rows) != len(dependencies)
                or any(not isinstance(row, dict) for row in rows)
                or {row.get("id") for row in rows} != set(dependencies)):
            raise ValueError("Transform report omits or changes original acceptance identifiers")
        for row in rows:
            names = [row["id"]] if row["id"] in observed else dependencies[row["id"]]
            passed = all(name in observed for name in names)
            required = [receipts[name]["receipt_sha256"] for name in names if name in receipts]
            if (row.get("outcome") != ("passed" if passed else "incomplete")
                    or row.get("evidence_verified") is not passed
                    or row.get("collector_receipts") != required
                    or row.get("observation") != "independent-live"
                    or row.get("verification_layer") != "installed-product-and-native"
                    or row.get("evidence_ref") != "sha256:" + digest(required)):
                raise ValueError("Transform verdict differs from collected criterion coverage")
    missing = sorted(set(expected["epic_criteria"]) - set(observed))
    if (report.get("unresolved") != [{"code": name + ":not-observed", "next_action": NEXT[name]} for name in missing]
            or report.get("cleanup") != {"outcome": "passed" if "EC-05" in observed else "incomplete"}):
        raise ValueError("Transform report concealed unresolved criteria or cleanup")


def validate_journal(report, directory, report_sha256):
    """Publication reopens the private journal; report hashes alone are insufficient."""
    from hashlib import sha256
    from qualification_transform_journal import ReleaseJournal, private_json

    validate_catalog(report)
    directory = Path(directory)
    if not (directory / "release-journal.jsonl").is_file():
        raise ValueError("Transform report has no original collector journal")
    journal = ReleaseJournal(directory, report["release_context"], report["fixture_sha256"],
                             report["requirements_revision"])
    with journal.locked():
        last = journal.records[-1]
        if last["kind"] != "report" or last["value"].get("sha256") != report_sha256:
            raise ValueError("Transform report is not the latest completed collection")
        name = last["value"].get("path")
        if not isinstance(name, str) or Path(name).name != name or not name.startswith("acceptance-report-"):
            raise ValueError("Transform immutable report reference is invalid")
        if (private_json(directory / name) != report
                or sha256((directory / name).read_bytes()).hexdigest() != report_sha256):
            raise ValueError("Transform report differs from its preserved collector output")
        indexed = {digest(row): row for row in journal.records}
        for identifier, receipt in report["collector_receipts"].items():
            row = indexed.get(receipt["receipt_sha256"])
            if (row is None or row["kind"] != "verified-observation"
                    or row["value"].get("criterion") != identifier
                    or row["value"].get("facts_sha256") != receipt["facts_sha256"]
                    or digest(row["value"].get("facts")) != receipt["facts_sha256"]):
                raise ValueError("Transform collector receipt has no matching private observations")
