#!/usr/bin/env python3
"""Validate S20 source reconciliation and disposition aggregates."""

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[4]
CANONICAL_COMMIT = "3193c78b167f583eed5026c582ab58794c88c34e"
CANONICAL_PATH = "docs/security/runs/2026-09-21/findings.json"
OWNERSHIP_PLAN_COMMIT = "42ade12e0f09570179c86a75a1c224cabab77cb4"
OWNERSHIP_PLAN_PATH = "docs/security/runs/2026-09-21/work-packages.md"
OWNERSHIP_EVIDENCE_PATH = HERE / "s20-ownership-evidence.json"
OWNERSHIP_CONTRACTS_PATH = HERE / "s20-ownership-contracts.json"
OWNERSHIP_CONTRACTS_SHA256 = "c89a617572317c1a6ee8774d2736123eedb87bcded9d7f977411ade15610f3aa"
OWNERSHIP_IDENTITY_COMMIT = "4683b3ea479c7609a08f99677d9625a5e99895d9"
OWNERSHIP_IDENTITY_PATH = "doc/ai-dlc-engine/ai-dlc-topology-preview.json"
OWNERSHIP_IDENTITY_SHA256 = "8cb7ce37479ab6140a41d9640bea034f2598eb7d97c74f2f752dbaedf8388b47"
OWNERSHIP_DECISION_BASES = {
    "S12": (
        "The KMS grants and worker execution policies directly control the vault/worker "
        "authority named by S12."
    ),
    "S14": (
        "Both runner IAM trees and the shared CodeBuild administrator role are CI escalation "
        "surfaces named by S14; the CodeBuild record remains open under that existing owner."
    ),
    "S17": (
        "The cyber ECR and KMS policies are worker infrastructure within the cyber execution "
        "and object-access boundary named by S17."
    ),
    "S21": (
        "The shared platform ECR tag policy is reserved for S21 by the S20 assignment boundary "
        "for shared release pins and global scanner dispositions."
    ),
}
TOPIC_SOURCE_REVISION = "428eac3b2151f24d6c829179141239da3532257d"
SOURCE_EXPORT_SHA256 = "bbac1c39fe13bc971062e36e8ddbc1ceebc966f769e0cc5ea2701ce066b45a8b"
TOPIC_DISPOSITIONS_SHA256 = "c4180d9b7990a0e099e7a46982f8ef02f5401d1dbb17088c177cfd73e474ebb1"
CANONICAL_ATTESTATION_COMMIT = "cf8f0c936a050405ad4481b4c6c8c9d48ab81061"
CANONICAL_ATTESTATION_PATH = "data/code-review/review-20260921-pr-5702.md"
CANONICAL_ATTESTATION_SHA256 = "314699db041d806ba3b385091540c560e1607fa2ec02890a4a5640908ddb7fe9"

OWNERSHIP_PLAN_QUOTES = {
    "S12": "[Security 2026-09-21] S12: Bind vault credentials and worker authority to the verified run",
    "S14": "[Security 2026-09-21] S14: Constrain CI runner IAM escalation in both infrastructure trees",
    "S17": "[Security 2026-09-21] S17: Secure cyber worker script execution and tenant-scoped object access",
    "S21": "[Security 2026-09-21] S21: Integrate fixes, reconcile AWS Security Agent results and verify the complete run",
}

ROUTED_DOMAIN_REQUIREMENTS = {
    "S12-vault-and-worker-keys": {
        "owner": "S12",
        "result_indices": {
            1, 2, 3, 22, 23, 24, 28, 29, 30, 84, 85, 86, 126, 127, 128,
            136, 137, 138, 142, 143, 144, 211, 212, 213,
        },
        "rules": {"CKV_AWS_109", "CKV_AWS_111", "CKV_AWS_356"},
        "files": {
            "modules/agent-context/terraform/kms.tf",
            "modules/agent-factory/infra/kms.tf",
            "modules/agent-factory/webhook-ingress/infra/kms.tf",
            "modules/gateway/cloudwatch-agent/terraform/kms.tf",
            "modules/gateway/infra/kms.tf",
            "platform/infra/kms.tf",
        },
    },
    "S12-worker-execution-authority": {
        "owner": "S12",
        "result_indices": {130, 131, 243},
        "rules": {"CKV_AWS_290", "CKV_AWS_355"},
        "files": {
            "modules/gateway/cloudwatch-agent/terraform/main.tf",
            "platform/infra/modules/iam/main.tf",
        },
    },
    "S14-two-runner-infrastructure-trees": {
        "owner": "S14",
        "result_indices": {
            45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57,
            63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75,
        },
        "rules": {
            "CKV_AWS_286", "CKV_AWS_287", "CKV_AWS_288", "CKV_AWS_289",
            "CKV_AWS_290", "CKV_AWS_355",
        },
        "files": {
            "modules/agent-factory/infra/modules/runner-iam/main.tf",
            "modules/agent-factory/runner-infra/infrastructure/iam.tf",
        },
    },
    "S14-shared-codebuild-role": {
        "owner": "S14",
        "result_indices": {219},
        "rules": {"CKV_AWS_274"},
        "files": {"platform/infra/modules/codebuild/main.tf"},
    },
    "S17-cyber-worker-infrastructure": {
        "owner": "S17",
        "result_indices": {106, 114, 115, 116},
        "rules": {"CKV_AWS_51", "CKV_AWS_109", "CKV_AWS_111", "CKV_AWS_356"},
        "files": {
            "modules/domain-apps/cyber/infra/ecr.tf",
            "modules/domain-apps/cyber/infra/kms.tf",
        },
    },
    "S21-shared-release-ecr": {
        "owner": "S21",
        "result_indices": {231, 233, 235, 237},
        "rules": {"CKV_AWS_51"},
        "files": {"platform/infra/modules/ecr/main.tf"},
    },
}

UNOWNED_FOLLOWON_SCOPE_FILES = {
    "FOLLOWON-C": {
        "modules/agent-factory/runner-infra/infrastructure/variables.tf",
        "modules/domain-apps/cyber/infra/variables.tf",
    },
    "FOLLOWON-G": {"modules/research/gbrain/terraform/main.tf"},
    "FOLLOWON-H": {"modules/agent-context/deploy.sh"},
}


def git_blob(revision, path):
    """Read an immutable repository blob or fail validation."""
    return subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    ).stdout


def ownership_context(plan_text, owner):
    """Return only headings or leading table rows that define an owner."""
    lines = plan_text.splitlines()
    owner_pattern = re.compile(rf"(?<![a-z0-9]){owner.lower()}(?![a-z0-9])", re.IGNORECASE)
    heading_pattern = re.compile(r"^(#{1,6})\s+")
    plan_quote = OWNERSHIP_PLAN_QUOTES.get(owner)
    contexts = []
    for index, line in enumerate(lines):
        heading = heading_pattern.match(line)
        if heading and owner_pattern.search(line):
            level = len(heading.group(1))
            end = index + 1
            while end < len(lines):
                next_heading = heading_pattern.match(lines[end])
                if next_heading and len(next_heading.group(1)) <= level:
                    break
                end += 1
            contexts.append("\n".join(lines[index:end]))
            continue
        if not line.lstrip().startswith("|"):
            continue
        leading_cells = [cell.strip() for cell in line.strip().strip("|").split("|")[:2]]
        defines_owner = any(
            re.fullmatch(rf"(?:[\[*`_]+)?{owner}(?:[\]*`_]+)?", cell, re.IGNORECASE)
            for cell in leading_cells
        )
        if defines_owner or (plan_quote and plan_quote in line):
            contexts.append(line)
    return "\n".join(contexts)


def ownership_scopes(context):
    """Extract repository path scopes quoted in an ownership-plan definition."""
    scopes = set()
    for value in re.findall(r"`([^`]+)`", context):
        for scope in re.split(r"[,\s]+", value):
            scope = scope.strip("'\"()[]{}:;")
            if "/" in scope and not scope.startswith(("http://", "https://")):
                scopes.add(scope)
    return scopes


def scope_matches(path, scope):
    """Return whether a repository path falls under a quoted plan scope."""
    normalized_scope = scope.removeprefix("./")
    if any(character in normalized_scope for character in "*?["):
        return fnmatch.fnmatch(path, normalized_scope)
    normalized_scope = normalized_scope.rstrip("/")
    return path == normalized_scope or path.startswith(f"{normalized_scope}/")


def validate_existing_owner_domains(existing_owner_records, record_key):
    """Require each mapped record to match one explicit, non-overlapping domain."""
    domain_record_keys = set()
    for domain, requirement in ROUTED_DOMAIN_REQUIREMENTS.items():
        domain_records = [
            record
            for record in existing_owner_records
            if record["result_index"] in requirement["result_indices"]
        ]
        assert {record["result_index"] for record in domain_records} == requirement["result_indices"]
        assert {record["owner"] for record in domain_records} == {requirement["owner"]}
        assert {record["rule_id"] for record in domain_records} <= requirement["rules"]
        assert {record["file"] for record in domain_records} == requirement["files"]
        current_keys = {record_key(record) for record in domain_records}
        assert domain_record_keys.isdisjoint(current_keys), f"routed domain overlap: {domain}"
        domain_record_keys.update(current_keys)
    assert domain_record_keys == {record_key(record) for record in existing_owner_records}


def matching_scopes(path, scopes_by_owner):
    """Return ownership scopes that contain a repository path."""
    return [
        (len(scope.rstrip("/")), owner, scope)
        for owner, scopes in scopes_by_owner.items()
        for scope in scopes
        if scope_matches(path, scope)
    ]


def record_key_digest(records):
    """Hash a stable ordered projection of source record keys."""
    keys = sorted((record["tool"], record["artifact"], record["result_index"]) for record in records)
    payload = "".join(
        f"{json.dumps(key, separators=(',', ':'))}\n"
        for key in keys
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_record_summary(summary, records):
    """Require evidence counts, keys, and paths to describe the same records."""
    assert summary["record_count"] == len(records)
    assert summary["record_keys_sha256"] == record_key_digest(records)
    assert summary["files"] == sorted({record["file"] for record in records})


def validate_bundled_ownership_evidence(evidence, dispositions, disposition_data, record_key):
    """Validate the self-contained owner and unowned-follow-on disposition map."""
    assert evidence["schema_version"] == 2
    assert evidence["work_package"] == "S20"
    assert evidence["issue"] == 5619
    assert evidence["source_plan"] == {
        "commit": OWNERSHIP_PLAN_COMMIT,
        "path": OWNERSHIP_PLAN_PATH,
    }
    assert evidence["contract_projection"] == {
        "path": str(OWNERSHIP_CONTRACTS_PATH.relative_to(REPOSITORY_ROOT)),
        "sha256": OWNERSHIP_CONTRACTS_SHA256,
    }
    assert evidence["record_key_digest"] == (
        "SHA-256 of sorted compact JSON [tool,artifact,result_index] tuples, "
        "one tuple per line"
    )
    assert evidence["review_method"] == [
        "Compare every open record source location and required remediation to each "
        "S01-S19 work-package contract.",
        "Assign an existing owner only where the record behavior is within that named "
        "contract; do not infer ownership from a broad parent directory.",
        "Classify a follow-on as unowned only after all S01-S19 contracts were considered; "
        "retain every source file and record-key digest for review.",
    ]

    identity = evidence["identity_attestation"]
    assert identity == {
        "commit": OWNERSHIP_IDENTITY_COMMIT,
        "path": OWNERSHIP_IDENTITY_PATH,
        "sha256": OWNERSHIP_IDENTITY_SHA256,
        "scope": "issue numbers and exact work-package titles only",
    }
    topology_bytes = git_blob(OWNERSHIP_IDENTITY_COMMIT, OWNERSHIP_IDENTITY_PATH)
    assert hashlib.sha256(topology_bytes).hexdigest() == OWNERSHIP_IDENTITY_SHA256
    topology = json.loads(topology_bytes)
    topology_packages = {}
    for node in topology["nodes"]:
        owner = node["address"].rsplit("/", 1)[-1].upper()
        if owner.startswith("S") and owner[1:].isdigit():
            topology_packages[owner] = {
                "issue": int(node["issue_ref"]),
                "title": node["title"],
            }

    expected_work_packages = {f"S{number:02d}" for number in range(1, 22)}
    assert set(topology_packages) == expected_work_packages
    evidence_packages = {entry["id"]: entry for entry in evidence["work_packages"]}
    assert len(evidence_packages) == len(evidence["work_packages"]) == 21
    assert set(evidence_packages) == expected_work_packages
    existing_owners = {"S12", "S14", "S17", "S21"}
    for owner, package in evidence_packages.items():
        assert {field: package[field] for field in ("issue", "title")} == topology_packages[owner]
        expected_role = (
            "existing-owner"
            if owner in existing_owners
            else "triage-owner"
            if owner == "S20"
            else "reviewed-no-assignment"
        )
        assert package["inventory_role"] == expected_role

    compared_owners = [f"S{number:02d}" for number in range(1, 20)]
    assert evidence["compared_existing_work_packages"] == compared_owners
    mappings = {entry["owner"]: entry for entry in evidence["existing_owner_mappings"]}
    assert len(mappings) == len(evidence["existing_owner_mappings"])
    assert set(mappings) == existing_owners
    existing_owner_records = [
        record for record in dispositions if record["owner"] in existing_owners
    ]
    for owner, mapping in mappings.items():
        assert mapping["classification"] == "existing-work-package"
        assert mapping["decision_basis"] == OWNERSHIP_DECISION_BASES[owner]
        assert {field: mapping[field] for field in ("issue", "title")} == topology_packages[owner]
        validate_record_summary(
            mapping,
            [record for record in existing_owner_records if record["owner"] == owner],
        )

    followon_definitions = disposition_data["proposed_followons"]
    followons = {entry["followon"]: entry for entry in evidence["unowned_followons"]}
    assert len(followons) == len(evidence["unowned_followons"])
    assert set(followons) == set(followon_definitions)
    unowned_followon_records = [
        record for record in dispositions if record["owner"].startswith("FOLLOWON-")
    ]
    for followon, entry in followons.items():
        assert entry["classification"] == "validated-unowned"
        assert entry["overlapping_work_packages"] == []
        assert entry["additional_scope_files"] == sorted(
            UNOWNED_FOLLOWON_SCOPE_FILES.get(followon, set())
        )
        matching_records = [
            record for record in unowned_followon_records if record["owner"] == followon
        ]
        assert all(record["verdict"] == "needs-followon" for record in matching_records)
        validate_record_summary(entry, matching_records)

    open_records = [
        record
        for record in dispositions
        if record["verdict"] in {"needs-followon", "routed-existing-owner"}
    ]
    assert {record_key(record) for record in open_records} == {
        record_key(record) for record in [*existing_owner_records, *unowned_followon_records]
    }
    partition = evidence["open_record_partition"]
    assert partition == {
        "record_count": len(open_records),
        "record_keys_sha256": record_key_digest(open_records),
        "existing_owner_record_count": len(existing_owner_records),
        "unowned_followon_record_count": len(unowned_followon_records),
    }
    assert len(open_records) == 396
    assert len(existing_owner_records) == 62
    assert len(unowned_followon_records) == 334
    validate_existing_owner_domains(existing_owner_records, record_key)


def validate_ownership_plan(plan_text, routed_records, followon_records, record_key):
    """Bind reviewed domain selectors to the actual canonical plan definitions.

    The plan uses Markdown issue links and prose scope bullets, not issue-title
    strings or a machine-readable path allowlist. The separately hash-pinned
    contract projection below resolves finding domains; directory overlap alone
    cannot prove ownership (for example, an image dependency owner does not own
    every IAM finding in that component).
    """
    contracts = ownership_contracts["contracts"]
    assert len(contracts) == 21
    for contract in contracts:
        owner = contract["id"]
        context = ownership_context(plan_text, owner)
        assert context, f"ownership plan lacks definition for {owner}"
        title = contract["title"].split(f"{owner}: ", 1)[1]
        heading = (
            f"### [{owner} / #{contract['issue']}]"
            f"(https://github.com/aws-e/adp/issues/{contract['issue']}) — {title}"
        )
        assert heading in context, f"ownership plan identity/title mismatch for {owner}"
    # These are exact scope statements in the canonical plan. The reviewed
    # projection supplies the more specific per-record remediation decisions.
    required_scopes = {
        "S12": "Own worker ScaledJob IAM, not CI runner IAM (S14) or cyber IAM (S17).",
        "S14": "modules/agent-factory/infra/modules/runner-iam/main.tf",
        "S17": "modules/domain-apps/cyber/infra/worker-irsa.tf and cape-host.tf",
        "S21": "Integrate fixes, reconcile AWS Security Agent results and verify the complete run",
    }
    for owner, quote in required_scopes.items():
        assert quote in ownership_context(plan_text, owner), f"ownership scope missing for {owner}"
    validate_existing_owner_domains(routed_records, record_key)


def selector_matches(record, selector):
    """Return whether one contract selector owns the record's finding domain."""
    return (
        record["group"] == selector["group"]
        and record["rule_id"] in selector["rule_ids"]
        and any(scope_matches(record["file"], scope) for scope in selector["path_scopes"])
    )


def validate_ownership_contract_projection(
    projection, existing_records, followon_records, record_key, scanned_revision
):
    """Validate every open record against the hash-pinned contract projection."""
    assert projection["schema_version"] == 1
    assert projection["source_plan"] == {
        "commit": OWNERSHIP_PLAN_COMMIT,
        "path": OWNERSHIP_PLAN_PATH,
    }
    assert projection["projection_scope"] == (
        "Exact work-package identities and bounded contract/path selectors used to resolve "
        "every open S20 record"
    )
    assert projection["identity_attestation"] == {
        "commit": OWNERSHIP_IDENTITY_COMMIT,
        "path": OWNERSHIP_IDENTITY_PATH,
        "sha256": OWNERSHIP_IDENTITY_SHA256,
    }

    topology_bytes = git_blob(OWNERSHIP_IDENTITY_COMMIT, OWNERSHIP_IDENTITY_PATH)
    assert hashlib.sha256(topology_bytes).hexdigest() == OWNERSHIP_IDENTITY_SHA256
    topology = json.loads(topology_bytes)
    topology_packages = {}
    for node in topology["nodes"]:
        owner = node["address"].rsplit("/", 1)[-1].upper()
        if owner.startswith("S") and owner[1:].isdigit():
            topology_packages[owner] = {
                "issue": int(node["issue_ref"]),
                "title": node["title"],
            }

    expected_packages = {f"S{number:02d}" for number in range(1, 22)}
    contracts = {entry["id"]: entry for entry in projection["contracts"]}
    assert len(contracts) == len(projection["contracts"]) == 21
    assert set(contracts) == set(topology_packages) == expected_packages

    selector_hits = Counter()
    for owner, contract in contracts.items():
        assert {field: contract[field] for field in ("issue", "title")} == topology_packages[owner]
        assert contract["contract_domains"]
        assert contract["path_scopes"] == sorted(set(contract["path_scopes"]))
        assert all(scope and not scope.startswith("/") for scope in contract["path_scopes"])
        for scope in contract["path_scopes"]:
            if owner == "S20":
                continue
            assert subprocess.run(
                ["git", "cat-file", "-e", f"{scanned_revision}:{scope}"],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
            ).returncode == 0, f"contract scope absent at scanned revision: {owner} {scope}"
        for selector_index, selector in enumerate(contract["s20_record_selectors"]):
            assert set(selector) == {"group", "rule_ids", "path_scopes"}
            assert selector["rule_ids"] == sorted(set(selector["rule_ids"]))
            assert selector["path_scopes"] == sorted(set(selector["path_scopes"]))
            assert all(
                any(scope_matches(scope, contract_scope) for contract_scope in contract["path_scopes"])
                for scope in selector["path_scopes"]
            )
            for record in [*existing_records, *followon_records]:
                if selector_matches(record, selector):
                    selector_hits[(owner, selector_index)] += 1

    def matching_contracts(record):
        return {
            owner
            for owner, contract in contracts.items()
            if any(
                selector_matches(record, selector)
                for selector in contract["s20_record_selectors"]
            )
        }

    for record in existing_records:
        matches = matching_contracts(record)
        assert matches == {record["owner"]}, (
            f"contract projection resolves {record_key(record)} to {sorted(matches)}, "
            f"not {record['owner']}"
        )
    for record in followon_records:
        matches = matching_contracts(record)
        assert not matches, (
            f"unowned {record_key(record)} overlaps contract selectors {sorted(matches)}"
        )

    expected_selectors = {
        (owner, selector_index)
        for owner, contract in contracts.items()
        for selector_index, _ in enumerate(contract["s20_record_selectors"])
    }
    assert set(selector_hits) == expected_selectors


def validated_attestation():
    """Return the hash- and ancestry-checked independent review attestation."""
    attestation_bytes = git_blob(CANONICAL_ATTESTATION_COMMIT, CANONICAL_ATTESTATION_PATH)
    assert hashlib.sha256(attestation_bytes).hexdigest() == CANONICAL_ATTESTATION_SHA256
    attestation_parent = subprocess.run(
        ["git", "rev-parse", f"{CANONICAL_ATTESTATION_COMMIT}^"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert attestation_parent == TOPIC_SOURCE_REVISION
    return " ".join(attestation_bytes.decode("utf-8").split())


def validate_canonical_attestation(source_by_key, record_key):
    """Validate the local chain from canonical review to retained source fields."""
    topic_bytes = git_blob(
        TOPIC_SOURCE_REVISION,
        "docs/security/runs/2026-09-21/triage/s20-dispositions.json",
    )
    assert hashlib.sha256(topic_bytes).hexdigest() == TOPIC_DISPOSITIONS_SHA256
    topic_records = json.loads(topic_bytes)["dispositions"]
    topic_by_key = {record_key(record): record for record in topic_records}
    assert len(topic_records) == len(topic_by_key) == 860
    for source_key, source_record in source_by_key.items():
        assert all(
            topic_by_key[source_key][field] == value
            for field, value in source_record.items()
        )

    attestation = validated_attestation()
    required_statements = (
        f"Verified head: `{TOPIC_SOURCE_REVISION}`",
        f"pinned commit `{CANONICAL_COMMIT}` (`{CANONICAL_PATH}`)",
        "`semgrep_unrated_error_findings` = 234, `checkov_unrated_findings` = 626, source keys = 860",
        "860 keys, 0 duplicates, 0 missing, 0 extra vs. source",
        "The 13 `suppressed_explicit_findings` records have zero key-overlap with the 860",
    )
    for statement in required_statements:
        assert statement in attestation


parser = argparse.ArgumentParser()
parser.add_argument("--canonical-findings", type=Path)
parser.add_argument("--canonical-sha256")
parser.add_argument("--require-canonical", action="store_true")
parser.add_argument("--ownership-plan", type=Path)
parser.add_argument("--ownership-contracts", type=Path)
parser.add_argument("--ownership-contracts-sha256")
parser.add_argument("--ownership-sha256")
parser.add_argument("--require-ownership-plan", action="store_true")
args = parser.parse_args()

if args.canonical_findings and not args.canonical_sha256:
    parser.error("--canonical-findings requires a trusted --canonical-sha256")
if args.canonical_sha256 and not args.canonical_findings:
    parser.error("--canonical-sha256 requires --canonical-findings")
if args.ownership_contracts and not args.ownership_contracts_sha256:
    parser.error("--ownership-contracts requires a trusted --ownership-contracts-sha256")
if args.ownership_contracts_sha256 and not args.ownership_contracts:
    parser.error("--ownership-contracts-sha256 requires --ownership-contracts")
if args.ownership_plan and not args.ownership_sha256:
    parser.error("--ownership-plan requires a trusted --ownership-sha256")
if args.ownership_sha256 and not args.ownership_plan:
    parser.error("--ownership-sha256 requires --ownership-plan")

disposition_data = json.loads((HERE / "s20-dispositions.json").read_text())
source_bytes = (HERE / "s20-source-records.json").read_bytes()
source_data = json.loads(source_bytes)
dispositions = disposition_data["dispositions"]
source_records = source_data["records"]

assert hashlib.sha256(source_bytes).hexdigest() == SOURCE_EXPORT_SHA256
assert source_data["provenance"] == disposition_data["generated_from"]
assert source_data["record_count"] == 860
assert source_data["tool_counts"] == {"checkov": 626, "semgrep": 234}

def record_key(record):
    return record["tool"], record["artifact"], record["result_index"]


source_by_key = {record_key(record): record for record in source_records}
disposition_by_key = {record_key(record): record for record in dispositions}

assert len(source_records) == len(source_by_key) == 860
assert len(dispositions) == len(disposition_by_key) == 860
assert Counter(record["tool"] for record in source_records) == {"semgrep": 234, "checkov": 626}
assert source_by_key.keys() == disposition_by_key.keys()

topic_bytes = git_blob(
    TOPIC_SOURCE_REVISION,
    "docs/security/runs/2026-09-21/triage/s20-dispositions.json",
)
assert hashlib.sha256(topic_bytes).hexdigest() == TOPIC_DISPOSITIONS_SHA256
topic_dispositions = json.loads(topic_bytes)["dispositions"]
topic_by_key = {record_key(record): record for record in topic_dispositions}
assert topic_by_key.keys() == source_by_key.keys()

for source_key, source_record in source_by_key.items():
    disposition = disposition_by_key[source_key]
    assert all(disposition[field] == value for field, value in source_record.items())
    assert all(topic_by_key[source_key][field] == value for field, value in source_record.items())
    assert disposition["verdict"] and disposition["assessed_severity"] and disposition["owner"] and disposition["rationale"]


def project_canonical_record(tool, record):
    projected = {"tool": tool}
    for field in ("artifact", "result_index", "run_id", "rule_id", "check_name", "file", "line", "end_line"):
        if field in record:
            projected[field] = record[field]
    # The canonical findings export stores the location as one nested entry
    # and Checkov's check name in message. Reconcile those fields too; dropping
    # them would miss a changed source location or check description.
    if "locations" in record:
        assert len(record["locations"]) == 1
        for field in ("file", "line", "end_line"):
            value = record["locations"][0][field]
            assert field not in projected or projected[field] == value
            projected[field] = value
    if tool == "checkov" and "message" in record:
        assert "check_name" not in projected or projected["check_name"] == record["message"]
        projected["check_name"] = record["message"]
    return projected


canonical_bytes = None
canonical_source = None
if args.canonical_findings:
    canonical_bytes = args.canonical_findings.read_bytes()
    assert hashlib.sha256(canonical_bytes).hexdigest() == args.canonical_sha256
    canonical_source = str(args.canonical_findings)
else:
    local_canonical = subprocess.run(
        ["git", "show", f"{CANONICAL_COMMIT}:{CANONICAL_PATH}"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    if local_canonical.returncode == 0:
        canonical_bytes = local_canonical.stdout
        canonical_source = f"{CANONICAL_COMMIT}:{CANONICAL_PATH}"

if canonical_bytes is not None:
    canonical = json.loads(canonical_bytes)
    canonical_records = [
        *(project_canonical_record("semgrep", record) for record in canonical["semgrep_unrated_error_findings"]),
        *(project_canonical_record("checkov", record) for record in canonical["checkov_unrated_findings"]),
    ]
    canonical_by_key = {record_key(record): record for record in canonical_records}
    assert len(canonical_records) == len(canonical_by_key) == 860
    assert canonical_by_key == source_by_key
    suppressed_records = [
        project_canonical_record(record.get("tool", "semgrep"), record)
        for record in canonical["suppressed_explicit_findings"]
    ]
    suppressed_keys = {record_key(record) for record in suppressed_records}
    assert len(suppressed_records) == len(suppressed_keys) == 13
    assert suppressed_keys.isdisjoint(canonical_by_key)
elif args.require_canonical:
    validate_canonical_attestation(source_by_key, record_key)
    canonical_source = (
        f"immutable independent attestation {CANONICAL_ATTESTATION_COMMIT}:"
        f"{CANONICAL_ATTESTATION_PATH}"
    )

ownership_bytes = None
ownership_source = None
ownership_text = None
if args.ownership_plan:
    ownership_bytes = args.ownership_plan.read_bytes()
    assert hashlib.sha256(ownership_bytes).hexdigest() == args.ownership_sha256
    ownership_source = str(args.ownership_plan)
else:
    local_ownership_plan = subprocess.run(
        ["git", "show", f"{OWNERSHIP_PLAN_COMMIT}:{OWNERSHIP_PLAN_PATH}"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    if local_ownership_plan.returncode == 0:
        ownership_bytes = local_ownership_plan.stdout
        ownership_source = f"{OWNERSHIP_PLAN_COMMIT}:{OWNERSHIP_PLAN_PATH}"

if ownership_bytes is not None:
    ownership_text = ownership_bytes.decode("utf-8")
elif args.require_ownership_plan:
    contract_path = args.ownership_contracts or OWNERSHIP_CONTRACTS_PATH
    contract_sha256 = args.ownership_contracts_sha256 or OWNERSHIP_CONTRACTS_SHA256
    ownership_source = (
        f"hash-pinned bundled contract projection {contract_path} "
        f"(sha256 {contract_sha256})"
    )

for field, metadata_key in (
    ("assessed_severity", "severity_counts"),
    ("verdict", "verdict_counts"),
    ("owner", "owner_counts"),
    ("group", "group_counts"),
):
    assert dict(Counter(record[field] for record in dispositions)) == disposition_data[metadata_key]

workflow_records = [record for record in dispositions if record["group"] == "workflow-shell-interpolation"]
assert len(workflow_records) == 54
assert Counter(record["verdict"] for record in workflow_records) == {
    "needs-followon": 41,
    "false-positive-constrained-context": 11,
    "false-positive-callsite-controlled": 2,
}
assert all("All 53 interpolate" not in record["rationale"] for record in workflow_records)

scanned_commit = disposition_data["generated_from"]["scanned_commit"]
deploy_eks_records = [record for record in workflow_records if record["result_index"] in {50, 51}]
assert len(deploy_eks_records) == 2
assert all(
    record["verdict"] == "needs-followon"
    and record["assessed_severity"] == "medium"
    and record["owner"] == "FOLLOWON-F"
    and "cross-repository" in record["rationale"]
    for record in deploy_eks_records
)
deploy_eks_source = subprocess.run(
    ["git", "show", f"{scanned_commit}:.github/workflows/_deploy-eks.yml"],
    cwd=REPOSITORY_ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout
assert "workflow_call:" in deploy_eks_source
assert "runs-on: arc-runner-org" in deploy_eks_source
assert "aws eks update-kubeconfig --name ${{ inputs.cluster_name }}" in deploy_eks_source
assert "kubectl apply -f . -n ${{ inputs.namespace }}" in deploy_eks_source
assert "deployment/${{ inputs.module }} -n ${{ inputs.namespace }}" in deploy_eks_source

secret_inherit_records = [record for record in dispositions if record["group"] == "workflow-secrets-inherit"]
assert len(secret_inherit_records) == 9
same_repository_secret_record = next(record for record in secret_inherit_records if record["result_index"] == 343)
assert (
    same_repository_secret_record["file"] == ".github/workflows/nightly-cli-regression.yml"
    and same_repository_secret_record["verdict"] == "accepted-risk-low"
    and same_repository_secret_record["assessed_severity"] == "low"
    and same_repository_secret_record["owner"] == "S20-reviewed"
    and "same repository" in same_repository_secret_record["rationale"]
)
cross_repository_secret_records = [
    record for record in secret_inherit_records if record["result_index"] in set(range(2368, 2376))
]
assert len(cross_repository_secret_records) == 8
assert all(
    record["verdict"] == "needs-followon"
    and record["assessed_severity"] == "medium"
    and record["owner"] == "FOLLOWON-I"
    and "mutable branch ref" in record["rationale"]
    for record in cross_repository_secret_records
)
client_workflow_readme = subprocess.run(
    ["git", "show", f"{scanned_commit}:modules/agent-factory/client-workflows/README.md"],
    cwd=REPOSITORY_ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout
assert "Drop-in GitHub Actions workflows for any repo" in client_workflow_readme
assert "reusable workflows must allow calls from your org" in client_workflow_readme
assert "Uses `secrets: inherit`" in client_workflow_readme
for record in cross_repository_secret_records:
    caller_source = subprocess.run(
        ["git", "show", f"{scanned_commit}:{record['file']}"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "uses: aws-innovate/adp/.github/workflows/" in caller_source
    assert "@main" in caller_source
    assert "secrets: inherit" in caller_source
assert "FOLLOWON-I" in disposition_data["proposed_followons"]

iam_records = [record for record in dispositions if record["group"] == "iam-wildcard-policy"]
assert len(iam_records) == 74
assert Counter((record["assessed_severity"], record["verdict"], record["owner"]) for record in iam_records) == {
    ("medium", "routed-existing-owner", "S12"): 27,
    ("medium", "routed-existing-owner", "S14"): 26,
    ("medium", "routed-existing-owner", "S17"): 3,
    ("none", "accepted-risk-required", "S20-reviewed"): 15,
    ("none", "not-currently-reachable", "S20-reviewed"): 1,
    ("low", "accepted-risk-documented", "S20-reviewed"): 2,
}
assert all("S14/S12/S17-overlap" not in record["owner"] for record in iam_records)
assert all("Individual verdicts require" not in record["rationale"] for record in iam_records)

routed_records = [record for record in dispositions if record["verdict"] == "routed-existing-owner"]
existing_owner_records = [
    record for record in dispositions if record["owner"] in {"S12", "S14", "S17", "S21"}
]
unowned_followon_records = [
    record for record in dispositions if record["owner"].startswith("FOLLOWON-")
]
assert len(routed_records) == 61
assert len(existing_owner_records) == 62
assert len(unowned_followon_records) == 334
ownership_evidence = json.loads(OWNERSHIP_EVIDENCE_PATH.read_text())
validate_bundled_ownership_evidence(ownership_evidence, dispositions, disposition_data, record_key)
if args.ownership_contracts:
    ownership_contract_bytes = args.ownership_contracts.read_bytes()
    ownership_contract_sha256 = args.ownership_contracts_sha256
else:
    ownership_contract_bytes = OWNERSHIP_CONTRACTS_PATH.read_bytes()
    ownership_contract_sha256 = OWNERSHIP_CONTRACTS_SHA256
assert hashlib.sha256(ownership_contract_bytes).hexdigest() == ownership_contract_sha256
ownership_contracts = json.loads(ownership_contract_bytes)
validate_ownership_contract_projection(
    ownership_contracts, existing_owner_records, unowned_followon_records, record_key, scanned_commit
)
if ownership_text is not None:
    validate_ownership_plan(
        ownership_text,
        existing_owner_records,
        unowned_followon_records,
        record_key,
    )

ecr_records = [record for record in dispositions if record["rule_id"] == "CKV_AWS_51"]
assert {record["result_index"] for record in ecr_records} == {106, 196, 231, 233, 235, 237}
assert Counter((record["verdict"], record["assessed_severity"], record["owner"]) for record in ecr_records) == {
    ("routed-existing-owner", "medium", "S21"): 4,
    ("routed-existing-owner", "medium", "S17"): 1,
    ("needs-followon", "medium", "FOLLOWON-G"): 1,
}

eks_version_records = [record for record in dispositions if record["rule_id"] == "CKV_AWS_339"]
assert {record["result_index"] for record in eks_version_records} == {59, 110, 239}
assert all(record["verdict"] == "false-positive-resolved-configuration" for record in eks_version_records)
assert all(record["assessed_severity"] == "none" for record in eks_version_records)

gateway_xml_records = [
    record
    for record in dispositions
    if record["tool"] == "semgrep" and record["result_index"] in {10316, 10317, 10318}
]
assert len(gateway_xml_records) == 3
assert all(record["verdict"] == "false-positive-constrained-context" for record in gateway_xml_records)
assert all(record["assessed_severity"] == "none" and record["owner"] == "S20-reviewed" for record in gateway_xml_records)

assert not any(record["owner"] == "FOLLOWON-D" for record in dispositions)
assert Counter(record["owner"] for record in dispositions if record["owner"].startswith("FOLLOWON-D-")) == {
    "FOLLOWON-D-IMDSV2": 1,
    "FOLLOWON-D-DEFAULT-SG": 3,
    "FOLLOWON-D-SECRET-ROTATION": 15,
}

backend_records = [record for record in dispositions if record["group"] == "terraform-backend"]
assert len(backend_records) == 14
assert Counter((record["verdict"], record["assessed_severity"], record["owner"]) for record in backend_records) == {
    ("false-positive-partial-backend", "none", "S20-reviewed"): 12,
    ("accepted-risk-low", "low", "S20-reviewed"): 1,
    ("needs-followon", "medium", "FOLLOWON-H"): 1,
}
agent_context_backend = next(record for record in backend_records if record["result_index"] == 0)
assert agent_context_backend["file"] == "modules/agent-context/terraform/backend.tf"
assert "deploy.sh" in agent_context_backend["rationale"]
backend_by_index = {record["result_index"]: record for record in backend_records}
backend_evidence_paths = {
    102: ".github/workflows/cyber-windows-image-build.yml",
    125: "modules/domain-apps/superplane/infra/workspaces/scripts/prepare_workspace_plan.py",
    179: "modules/research/gbrain/scripts/deploy.sh",
    250: "platform/scripts/release/bootstrap.py",
}
for result_index, evidence_path in backend_evidence_paths.items():
    assert evidence_path in backend_by_index[result_index]["rationale"]
runner_vpc_module = next(record for record in backend_records if record["result_index"] == 76)
assert runner_vpc_module["rule_id"] == "CKV_TF_1"
assert runner_vpc_module["file"] == "modules/agent-factory/runner-infra/infrastructure/vpc.tf"

agent_context_deploy = subprocess.run(
    ["git", "show", f"{scanned_commit}:modules/agent-context/deploy.sh"],
    cwd=REPOSITORY_ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout
assert agent_context_deploy.count("terraform init -upgrade") == 2
assert "-backend-config" not in agent_context_deploy


def scanned_source(path):
    return subprocess.run(
        ["git", "show", f"{scanned_commit}:{path}"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


codebuild_records = [record for record in dispositions if record["rule_id"] == "CKV_AWS_316"]
codebuild_indices_by_file = {
    "modules/agent-context/terraform/modules/images-build/main.tf": {8, 9, 10, 11, 12},
    "modules/research/gbrain/terraform/modules/build/main.tf": {183},
    "platform/infra/modules/codebuild/main.tf": {220, 222, 223, 224, 225, 226, 227, 228, 229, 230},
}
assert len(codebuild_records) == 16
for source_file, result_indices in codebuild_indices_by_file.items():
    package_records = [record for record in codebuild_records if record["file"] == source_file]
    assert {record["result_index"] for record in package_records} == result_indices
    assert all(
        record["verdict"] == "accepted-risk-required"
        and record["assessed_severity"] == "low"
        and record["owner"] == "S20-reviewed"
        for record in package_records
    )
assert all("4 CodeBuild projects" not in record["rationale"] for record in codebuild_records)

agent_context_projects = {
    "ingestion",
    "codegraph-context",
    "litellm-proxy",
    "deepwiki",
    "context-mcp",
}
platform_projects = {
    "gateway-build",
    "chat-agent",
    "agent-gateway",
    "arc-runner",
    "cyber-worker",
    "agent-runtime",
    "pyjwt-layer",
    "psycopg2-layer",
    "grype-scan",
    "syft-scan",
}
agent_context_codebuild = scanned_source(
    "modules/agent-context/terraform/modules/images-build/main.tf"
)
agent_context_project_block = agent_context_codebuild.split("images = {", 1)[1].split("\n  }\n}", 1)[0]
assert set(re.findall(r'^\s+"([^"]+)"\s+=\s+\{', agent_context_project_block, re.MULTILINE)) == agent_context_projects
assert "service_role = var.codebuild_service_role_arn" in agent_context_codebuild
assert "privileged_mode             = true" in agent_context_codebuild
agent_context_root = scanned_source("modules/agent-context/terraform/main.tf")
assert (
    "codebuild_service_role_arn = data.terraform_remote_state.platform.outputs.codebuild_role_arn"
    in agent_context_root
)

platform_codebuild = scanned_source("platform/infra/modules/codebuild/main.tf")
platform_project_block = platform_codebuild.split("projects = {", 1)[1].split("\n  }\n}", 1)[0]
assert set(re.findall(r'^\s+"([^"]+)"\s+=\s+\{', platform_project_block, re.MULTILINE)) == platform_projects
assert 'policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"' in platform_codebuild
assert "service_role  = aws_iam_role.codebuild.arn" in platform_codebuild
assert "privileged_mode             = true" in platform_codebuild
for required_capability in (
    "ECR push",
    "S3 read",
    "CloudWatch Logs",
    "EKS describe",
    "Secrets Manager read",
):
    assert required_capability in platform_codebuild
assert "codebuild_security_scan_upload" in platform_codebuild
platform_codebuild_outputs = scanned_source("platform/infra/modules/codebuild/outputs.tf")
assert "value       = aws_iam_role.codebuild.arn" in platform_codebuild_outputs

agent_context_rationales = [
    record["rationale"]
    for record in codebuild_records
    if record["file"] == "modules/agent-context/terraform/modules/images-build/main.tf"
]
assert all("five agent-context projects" in rationale for rationale in agent_context_rationales)
assert all("all 15 consumers" in rationale for rationale in agent_context_rationales)
platform_rationales = [
    record["rationale"]
    for record in codebuild_records
    if record["file"] == "platform/infra/modules/codebuild/main.tf"
]
assert all("ten platform projects" in rationale for rationale in platform_rationales)
assert all("all 15 consumers" in rationale for rationale in platform_rationales)

gbrain_codebuild = scanned_source("modules/research/gbrain/terraform/modules/build/main.tf")
assert 'resource "aws_codebuild_project" "gbrain_build"' in gbrain_codebuild
assert "service_role = aws_iam_role.codebuild.arn" in gbrain_codebuild
assert "privileged_mode             = true" in gbrain_codebuild
for scoped_policy in ("codebuild_ecr", "codebuild_logs", "codebuild_s3_source"):
    assert f'resource "aws_iam_role_policy" "{scoped_policy}"' in gbrain_codebuild
gbrain_record = next(record for record in codebuild_records if record["result_index"] == 183)
assert "does not use the platform AdministratorAccess role" in gbrain_record["rationale"]

administrator_record = next(
    record for record in dispositions if record["rule_id"] == "CKV_AWS_274" and record["result_index"] == 219
)
assert administrator_record["owner"] == "S14"
assert administrator_record["verdict"] == "needs-followon"
assert "all 15 consumers" in administrator_record["rationale"]
assert "not only ECR and logs" in administrator_record["rationale"]

cyber_image_workflow = scanned_source(backend_evidence_paths[102])
assert "TF_DIR: modules/domain-apps/cyber/image-builder" in cyber_image_workflow
assert cyber_image_workflow.count('-backend-config="dynamodb_table=adp-terraform-locks"') == 2

superplane_prepare = scanned_source(backend_evidence_paths[125])
superplane_workspace_readme = scanned_source(
    "modules/domain-apps/superplane/infra/workspaces/README.md"
)
assert 'parser.add_argument("--" + name, required=True)' in superplane_prepare
assert '"dynamodb_table": args.lock_table' in superplane_prepare
assert '"-backend-config=" + backend_file.name' in superplane_prepare
assert "--lock-table adp-terraform-locks" in superplane_workspace_readme

gbrain_deploy = scanned_source(backend_evidence_paths[179])
assert 'TF_DIR="${MODULE_DIR}/terraform"' in gbrain_deploy
assert '-backend-config="dynamodb_table=adp-terraform-locks"' in gbrain_deploy

release_bootstrap = scanned_source(backend_evidence_paths[250])
assert "directory = ROOT / 'platform/release-infra'" in release_bootstrap
assert "'-backend-config=dynamodb_table=adp-terraform-locks'" in release_bootstrap
assert "cwd=directory" in release_bootstrap

runner_vpc_source = subprocess.run(
    ["git", "show", f"{scanned_commit}:{runner_vpc_module['file']}"],
    cwd=REPOSITORY_ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout
assert 'source  = "terraform-aws-modules/vpc/aws"' in runner_vpc_source
assert 'version = "~> 5.0"' in runner_vpc_source

readme = (HERE / "README.md").read_text()
table_rows = {}
for line in readme.splitlines():
    normalized = line.replace("**", "")
    if not normalized.startswith("| `"):
        continue
    cells = [cell.strip() for cell in normalized.strip("|").split("|")]
    if len(cells) != 7:
        continue
    table_rows[cells[0].strip("`")] = cells

group_counts = Counter(record["group"] for record in dispositions)
group_files = {}
group_records = {}
for record in dispositions:
    group_files.setdefault(record["group"], set()).add(record["file"])
    group_records.setdefault(record["group"], []).append(record)


def summarize(records, field):
    counts = Counter(record[field] for record in records)
    if len(counts) == 1:
        return next(iter(counts))
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return "; ".join(f"{count} {value}" for value, count in ordered)


for group, count in group_counts.items():
    assert int(table_rows[group][1]) == count
    assert int(table_rows[group][2]) == len(group_files[group])
    assert table_rows[group][3] == summarize(group_records[group], "assessed_severity")
    assert table_rows[group][4] == summarize(group_records[group], "verdict")
    assert table_rows[group][5] == summarize(group_records[group], "owner")

for severity, count in disposition_data["severity_counts"].items():
    assert f"| {severity} | {count} |" in readme

reconciliation = disposition_data["reconciliation"]
assert reconciliation == {
    "source_semgrep": 234,
    "source_checkov": 626,
    "source_total": 860,
    "dispositioned_total": 860,
    "unique_keys": 860,
    "unaccounted": 0,
}

print("S20 validation passed: 860/860 source records, 0 missing, 0 extra")
print(f"Normalized source integrity passed: topic revision {TOPIC_SOURCE_REVISION}, sha256 {SOURCE_EXPORT_SHA256}")
if canonical_source:
    print(f"Canonical source provenance passed: {canonical_source}")
else:
    print("Canonical source provenance not run; use --require-canonical for the attested provenance check")
print(
    "Hash-pinned ownership contracts passed: 62 existing-owner records and "
    "334 unowned follow-on records resolved across S01-S21"
)
if ownership_text is not None:
    print(f"Optional source-plan semantic comparison passed: {ownership_source}")
elif args.require_ownership_plan:
    print(f"Ownership requirement satisfied by {ownership_source}")
else:
    print("Optional verbatim source-plan comparison not run; hash-pinned contract validation passed")
print("IAM triage passed: 56 routed records, 18 required/inert/documented records")
print("Workflow triage passed: 41 reachable shell follow-ons, 13 constrained records")
print("Workflow secret triage passed: 8 cross-repository follow-ons, 1 same-repository accepted risk")
print(
    "Review repairs passed: CodeBuild role mapping, workflow boundaries, backend locking, "
    "ECR integrity, EKS versions, gateway XML, and split infra follow-ons"
)
