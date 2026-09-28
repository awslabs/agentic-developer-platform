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
OWNERSHIP_CONTRACTS_SHA256 = (
    "c89a617572317c1a6ee8774d2736123eedb87bcded9d7f977411ade15610f3aa"
)
OWNERSHIP_IDENTITY_COMMIT = "4683b3ea479c7609a08f99677d9625a5e99895d9"
OWNERSHIP_IDENTITY_PATH = "doc/ai-dlc-engine/ai-dlc-topology-preview.json"
OWNERSHIP_IDENTITY_SHA256 = (
    "8cb7ce37479ab6140a41d9640bea034f2598eb7d97c74f2f752dbaedf8388b47"
)
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
SOURCE_EXPORT_SHA256 = (
    "bbac1c39fe13bc971062e36e8ddbc1ceebc966f769e0cc5ea2701ce066b45a8b"
)
TOPIC_DISPOSITIONS_SHA256 = (
    "c4180d9b7990a0e099e7a46982f8ef02f5401d1dbb17088c177cfd73e474ebb1"
)
CANONICAL_ATTESTATION_COMMIT = "cf8f0c936a050405ad4481b4c6c8c9d48ab81061"
CANONICAL_ATTESTATION_PATH = "data/code-review/review-20260921-pr-5702.md"
CANONICAL_ATTESTATION_SHA256 = (
    "314699db041d806ba3b385091540c560e1607fa2ec02890a4a5640908ddb7fe9"
)

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
            1,
            2,
            3,
            22,
            23,
            24,
            28,
            29,
            30,
            84,
            85,
            86,
            126,
            127,
            128,
            136,
            137,
            138,
            142,
            143,
            144,
            211,
            212,
            213,
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
            45,
            46,
            47,
            48,
            49,
            50,
            51,
            52,
            53,
            54,
            55,
            56,
            57,
            63,
            64,
            65,
            66,
            67,
            68,
            69,
            70,
            71,
            72,
            73,
            74,
            75,
        },
        "rules": {
            "CKV_AWS_286",
            "CKV_AWS_287",
            "CKV_AWS_288",
            "CKV_AWS_289",
            "CKV_AWS_290",
            "CKV_AWS_355",
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
    owner_pattern = re.compile(
        rf"(?<![a-z0-9]){owner.lower()}(?![a-z0-9])", re.IGNORECASE
    )
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
        leading_cells = [
            cell.strip() for cell in line.strip().strip("|").split("|")[:2]
        ]
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
        if not (
            {record["result_index"] for record in domain_records}
            == requirement["result_indices"]
        ):
            raise AssertionError()
        if not (
            {record["owner"] for record in domain_records} == {requirement["owner"]}
        ):
            raise AssertionError()
        if not (
            {record["rule_id"] for record in domain_records} <= requirement["rules"]
        ):
            raise AssertionError()
        if not ({record["file"] for record in domain_records} == requirement["files"]):
            raise AssertionError()
        current_keys = {record_key(record) for record in domain_records}
        if not (domain_record_keys.isdisjoint(current_keys)):
            raise AssertionError(f"routed domain overlap: {domain}")
        domain_record_keys.update(current_keys)
    if not (
        domain_record_keys == {record_key(record) for record in existing_owner_records}
    ):
        raise AssertionError()


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
    keys = sorted(
        (record["tool"], record["artifact"], record["result_index"])
        for record in records
    )
    payload = "".join(
        f"{json.dumps(key, separators=(',', ':'))}\n" for key in keys
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_record_summary(summary, records):
    """Require evidence counts, keys, and paths to describe the same records."""
    if not (summary["record_count"] == len(records)):
        raise AssertionError()
    if not (summary["record_keys_sha256"] == record_key_digest(records)):
        raise AssertionError()
    if not (summary["files"] == sorted({record["file"] for record in records})):
        raise AssertionError()


def validate_bundled_ownership_evidence(
    evidence, dispositions, disposition_data, record_key
):
    """Validate the self-contained owner and unowned-follow-on disposition map."""
    if not (evidence["schema_version"] == 2):
        raise AssertionError()
    if not (evidence["work_package"] == "S20"):
        raise AssertionError()
    if not (evidence["issue"] == 5619):
        raise AssertionError()
    if not (
        evidence["source_plan"]
        == {
            "commit": OWNERSHIP_PLAN_COMMIT,
            "path": OWNERSHIP_PLAN_PATH,
        }
    ):
        raise AssertionError()
    if not (
        evidence["contract_projection"]
        == {
            "path": str(OWNERSHIP_CONTRACTS_PATH.relative_to(REPOSITORY_ROOT)),
            "sha256": OWNERSHIP_CONTRACTS_SHA256,
        }
    ):
        raise AssertionError()
    if not (
        evidence["record_key_digest"]
        == (
            "SHA-256 of sorted compact JSON [tool,artifact,result_index] tuples, "
            "one tuple per line"
        )
    ):
        raise AssertionError()
    if not (
        evidence["review_method"]
        == [
            "Compare every open record source location and required remediation to each "
            "S01-S19 work-package contract.",
            "Assign an existing owner only where the record behavior is within that named "
            "contract; do not infer ownership from a broad parent directory.",
            "Classify a follow-on as unowned only after all S01-S19 contracts were considered; "
            "retain every source file and record-key digest for review.",
        ]
    ):
        raise AssertionError()

    identity = evidence["identity_attestation"]
    if not (
        identity
        == {
            "commit": OWNERSHIP_IDENTITY_COMMIT,
            "path": OWNERSHIP_IDENTITY_PATH,
            "sha256": OWNERSHIP_IDENTITY_SHA256,
            "scope": "issue numbers and exact work-package titles only",
        }
    ):
        raise AssertionError()
    topology_bytes = git_blob(OWNERSHIP_IDENTITY_COMMIT, OWNERSHIP_IDENTITY_PATH)
    if not (hashlib.sha256(topology_bytes).hexdigest() == OWNERSHIP_IDENTITY_SHA256):
        raise AssertionError()
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
    if not (set(topology_packages) == expected_work_packages):
        raise AssertionError()
    evidence_packages = {entry["id"]: entry for entry in evidence["work_packages"]}
    if not (len(evidence_packages) == len(evidence["work_packages"]) == 21):
        raise AssertionError()
    if not (set(evidence_packages) == expected_work_packages):
        raise AssertionError()
    existing_owners = {"S12", "S14", "S17", "S21"}
    for owner, package in evidence_packages.items():
        if not (
            {field: package[field] for field in ("issue", "title")}
            == topology_packages[owner]
        ):
            raise AssertionError()
        expected_role = (
            "existing-owner"
            if owner in existing_owners
            else "triage-owner"
            if owner == "S20"
            else "reviewed-no-assignment"
        )
        if not (package["inventory_role"] == expected_role):
            raise AssertionError()

    compared_owners = [f"S{number:02d}" for number in range(1, 20)]
    if not (evidence["compared_existing_work_packages"] == compared_owners):
        raise AssertionError()
    mappings = {entry["owner"]: entry for entry in evidence["existing_owner_mappings"]}
    if not (len(mappings) == len(evidence["existing_owner_mappings"])):
        raise AssertionError()
    if not (set(mappings) == existing_owners):
        raise AssertionError()
    existing_owner_records = [
        record for record in dispositions if record["owner"] in existing_owners
    ]
    for owner, mapping in mappings.items():
        if not (mapping["classification"] == "existing-work-package"):
            raise AssertionError()
        if not (mapping["decision_basis"] == OWNERSHIP_DECISION_BASES[owner]):
            raise AssertionError()
        if not (
            {field: mapping[field] for field in ("issue", "title")}
            == topology_packages[owner]
        ):
            raise AssertionError()
        validate_record_summary(
            mapping,
            [record for record in existing_owner_records if record["owner"] == owner],
        )

    followon_definitions = disposition_data["proposed_followons"]
    followons = {entry["followon"]: entry for entry in evidence["unowned_followons"]}
    if not (len(followons) == len(evidence["unowned_followons"])):
        raise AssertionError()
    if not (set(followons) == set(followon_definitions)):
        raise AssertionError()
    unowned_followon_records = [
        record for record in dispositions if record["owner"].startswith("FOLLOWON-")
    ]
    for followon, entry in followons.items():
        if not (entry["classification"] == "validated-unowned"):
            raise AssertionError()
        if not (entry["overlapping_work_packages"] == []):
            raise AssertionError()
        if not (
            entry["additional_scope_files"]
            == sorted(UNOWNED_FOLLOWON_SCOPE_FILES.get(followon, set()))
        ):
            raise AssertionError()
        matching_records = [
            record for record in unowned_followon_records if record["owner"] == followon
        ]
        if not (
            all(record["verdict"] == "needs-followon" for record in matching_records)
        ):
            raise AssertionError()
        validate_record_summary(entry, matching_records)

    open_records = [
        record
        for record in dispositions
        if record["verdict"] in {"needs-followon", "routed-existing-owner"}
    ]
    if not (
        {record_key(record) for record in open_records}
        == {
            record_key(record)
            for record in [*existing_owner_records, *unowned_followon_records]
        }
    ):
        raise AssertionError()
    partition = evidence["open_record_partition"]
    if not (
        partition
        == {
            "record_count": len(open_records),
            "record_keys_sha256": record_key_digest(open_records),
            "existing_owner_record_count": len(existing_owner_records),
            "unowned_followon_record_count": len(unowned_followon_records),
        }
    ):
        raise AssertionError()
    if not (len(open_records) == 396):
        raise AssertionError()
    if not (len(existing_owner_records) == 62):
        raise AssertionError()
    if not (len(unowned_followon_records) == 334):
        raise AssertionError()
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
    if not (len(contracts) == 21):
        raise AssertionError()
    for contract in contracts:
        owner = contract["id"]
        context = ownership_context(plan_text, owner)
        if not (context):
            raise AssertionError(f"ownership plan lacks definition for {owner}")
        title = contract["title"].split(f"{owner}: ", 1)[1]
        heading = (
            f"### [{owner} / #{contract['issue']}]"
            f"(https://github.com/aws-e/adp/issues/{contract['issue']}) — {title}"
        )
        if heading not in context:
            raise AssertionError(f"ownership plan identity/title mismatch for {owner}")
    # These are exact scope statements in the canonical plan. The reviewed
    # projection supplies the more specific per-record remediation decisions.
    required_scopes = {
        "S12": "Own worker ScaledJob IAM, not CI runner IAM (S14) or cyber IAM (S17).",
        "S14": "modules/agent-factory/infra/modules/runner-iam/main.tf",
        "S17": "modules/domain-apps/cyber/infra/worker-irsa.tf and cape-host.tf",
        "S21": "Integrate fixes, reconcile AWS Security Agent results and verify the complete run",
    }
    for owner, quote in required_scopes.items():
        if quote not in ownership_context(plan_text, owner):
            raise AssertionError(f"ownership scope missing for {owner}")
    validate_existing_owner_domains(routed_records, record_key)


def selector_matches(record, selector):
    """Return whether one contract selector owns the record's finding domain."""
    return (
        record["group"] == selector["group"]
        and record["rule_id"] in selector["rule_ids"]
        and any(
            scope_matches(record["file"], scope) for scope in selector["path_scopes"]
        )
    )


def validate_ownership_contract_projection(
    projection, existing_records, followon_records, record_key, scanned_revision
):
    """Validate every open record against the hash-pinned contract projection."""
    if not (projection["schema_version"] == 1):
        raise AssertionError()
    if not (
        projection["source_plan"]
        == {
            "commit": OWNERSHIP_PLAN_COMMIT,
            "path": OWNERSHIP_PLAN_PATH,
        }
    ):
        raise AssertionError()
    if not (
        projection["projection_scope"]
        == (
            "Exact work-package identities and bounded contract/path selectors used to resolve "
            "every open S20 record"
        )
    ):
        raise AssertionError()
    if not (
        projection["identity_attestation"]
        == {
            "commit": OWNERSHIP_IDENTITY_COMMIT,
            "path": OWNERSHIP_IDENTITY_PATH,
            "sha256": OWNERSHIP_IDENTITY_SHA256,
        }
    ):
        raise AssertionError()

    topology_bytes = git_blob(OWNERSHIP_IDENTITY_COMMIT, OWNERSHIP_IDENTITY_PATH)
    if not (hashlib.sha256(topology_bytes).hexdigest() == OWNERSHIP_IDENTITY_SHA256):
        raise AssertionError()
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
    if not (len(contracts) == len(projection["contracts"]) == 21):
        raise AssertionError()
    if not (set(contracts) == set(topology_packages) == expected_packages):
        raise AssertionError()

    selector_hits = Counter()
    for owner, contract in contracts.items():
        if not (
            {field: contract[field] for field in ("issue", "title")}
            == topology_packages[owner]
        ):
            raise AssertionError()
        if not (contract["contract_domains"]):
            raise AssertionError()
        if not (contract["path_scopes"] == sorted(set(contract["path_scopes"]))):
            raise AssertionError()
        if not (
            all(
                scope and not scope.startswith("/") for scope in contract["path_scopes"]
            )
        ):
            raise AssertionError()
        for scope in contract["path_scopes"]:
            if owner == "S20":
                continue
            if not (
                subprocess.run(
                    ["git", "cat-file", "-e", f"{scanned_revision}:{scope}"],
                    cwd=REPOSITORY_ROOT,
                    check=False,
                    capture_output=True,
                ).returncode
                == 0
            ):
                raise AssertionError(
                    f"contract scope absent at scanned revision: {owner} {scope}"
                )
        for selector_index, selector in enumerate(contract["s20_record_selectors"]):
            if not (set(selector) == {"group", "rule_ids", "path_scopes"}):
                raise AssertionError()
            if not (selector["rule_ids"] == sorted(set(selector["rule_ids"]))):
                raise AssertionError()
            if not (selector["path_scopes"] == sorted(set(selector["path_scopes"]))):
                raise AssertionError()
            if not (
                all(
                    any(
                        scope_matches(scope, contract_scope)
                        for contract_scope in contract["path_scopes"]
                    )
                    for scope in selector["path_scopes"]
                )
            ):
                raise AssertionError()
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
        if not (matches == {record["owner"]}):
            raise AssertionError(
                f"contract projection resolves {record_key(record)} to {sorted(matches)}, "
                f"not {record['owner']}"
            )
    for record in followon_records:
        matches = matching_contracts(record)
        if not (not matches):
            raise AssertionError(
                f"unowned {record_key(record)} overlaps contract selectors {sorted(matches)}"
            )

    expected_selectors = {
        (owner, selector_index)
        for owner, contract in contracts.items()
        for selector_index, _ in enumerate(contract["s20_record_selectors"])
    }
    if not (set(selector_hits) == expected_selectors):
        raise AssertionError()


def validated_attestation():
    """Return the hash- and ancestry-checked independent review attestation."""
    attestation_bytes = git_blob(
        CANONICAL_ATTESTATION_COMMIT, CANONICAL_ATTESTATION_PATH
    )
    if not (
        hashlib.sha256(attestation_bytes).hexdigest() == CANONICAL_ATTESTATION_SHA256
    ):
        raise AssertionError()
    attestation_parent = subprocess.run(
        ["git", "rev-parse", f"{CANONICAL_ATTESTATION_COMMIT}^"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not (attestation_parent == TOPIC_SOURCE_REVISION):
        raise AssertionError()
    return " ".join(attestation_bytes.decode("utf-8").split())


def validate_canonical_attestation(source_by_key, record_key):
    """Validate the local chain from canonical review to retained source fields."""
    topic_bytes = git_blob(
        TOPIC_SOURCE_REVISION,
        "docs/security/runs/2026-09-21/triage/s20-dispositions.json",
    )
    if not (hashlib.sha256(topic_bytes).hexdigest() == TOPIC_DISPOSITIONS_SHA256):
        raise AssertionError()
    topic_records = json.loads(topic_bytes)["dispositions"]
    topic_by_key = {record_key(record): record for record in topic_records}
    if not (len(topic_records) == len(topic_by_key) == 860):
        raise AssertionError()
    for source_key, source_record in source_by_key.items():
        if not (
            all(
                topic_by_key[source_key][field] == value
                for field, value in source_record.items()
            )
        ):
            raise AssertionError()

    attestation = validated_attestation()
    required_statements = (
        f"Verified head: `{TOPIC_SOURCE_REVISION}`",
        f"pinned commit `{CANONICAL_COMMIT}` (`{CANONICAL_PATH}`)",
        "`semgrep_unrated_error_findings` = 234, `checkov_unrated_findings` = 626, source keys = 860",
        "860 keys, 0 duplicates, 0 missing, 0 extra vs. source",
        "The 13 `suppressed_explicit_findings` records have zero key-overlap with the 860",
    )
    for statement in required_statements:
        if statement not in attestation:
            raise AssertionError()


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
    parser.error(
        "--ownership-contracts requires a trusted --ownership-contracts-sha256"
    )
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

if not (hashlib.sha256(source_bytes).hexdigest() == SOURCE_EXPORT_SHA256):
    raise AssertionError()
if not (source_data["provenance"] == disposition_data["generated_from"]):
    raise AssertionError()
if not (source_data["record_count"] == 860):
    raise AssertionError()
if not (source_data["tool_counts"] == {"checkov": 626, "semgrep": 234}):
    raise AssertionError()


def record_key(record):
    return record["tool"], record["artifact"], record["result_index"]


source_by_key = {record_key(record): record for record in source_records}
disposition_by_key = {record_key(record): record for record in dispositions}

if not (len(source_records) == len(source_by_key) == 860):
    raise AssertionError()
if not (len(dispositions) == len(disposition_by_key) == 860):
    raise AssertionError()
if not (
    Counter(record["tool"] for record in source_records)
    == {"semgrep": 234, "checkov": 626}
):
    raise AssertionError()
if not (source_by_key.keys() == disposition_by_key.keys()):
    raise AssertionError()

topic_bytes = git_blob(
    TOPIC_SOURCE_REVISION,
    "docs/security/runs/2026-09-21/triage/s20-dispositions.json",
)
if not (hashlib.sha256(topic_bytes).hexdigest() == TOPIC_DISPOSITIONS_SHA256):
    raise AssertionError()
topic_dispositions = json.loads(topic_bytes)["dispositions"]
topic_by_key = {record_key(record): record for record in topic_dispositions}
if not (topic_by_key.keys() == source_by_key.keys()):
    raise AssertionError()

for source_key, source_record in source_by_key.items():
    disposition = disposition_by_key[source_key]
    if not (all(disposition[field] == value for field, value in source_record.items())):
        raise AssertionError()
    if not (
        all(
            topic_by_key[source_key][field] == value
            for field, value in source_record.items()
        )
    ):
        raise AssertionError()
    if not (
        disposition["verdict"]
        and disposition["assessed_severity"]
        and disposition["owner"]
        and disposition["rationale"]
    ):
        raise AssertionError()


def project_canonical_record(tool, record):
    projected = {"tool": tool}
    for field in (
        "artifact",
        "result_index",
        "run_id",
        "rule_id",
        "check_name",
        "file",
        "line",
        "end_line",
    ):
        if field in record:
            projected[field] = record[field]
    # The canonical findings export stores the location as one nested entry
    # and Checkov's check name in message. Reconcile those fields too; dropping
    # them would miss a changed source location or check description.
    if "locations" in record:
        if not (len(record["locations"]) == 1):
            raise AssertionError()
        for field in ("file", "line", "end_line"):
            value = record["locations"][0][field]
            if not (field not in projected or projected[field] == value):
                raise AssertionError()
            projected[field] = value
    if tool == "checkov" and "message" in record:
        if not (
            "check_name" not in projected
            or projected["check_name"] == record["message"]
        ):
            raise AssertionError()
        projected["check_name"] = record["message"]
    return projected


canonical_bytes = None
canonical_source = None
if args.canonical_findings:
    canonical_bytes = args.canonical_findings.read_bytes()
    if not (hashlib.sha256(canonical_bytes).hexdigest() == args.canonical_sha256):
        raise AssertionError()
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
        *(
            project_canonical_record("semgrep", record)
            for record in canonical["semgrep_unrated_error_findings"]
        ),
        *(
            project_canonical_record("checkov", record)
            for record in canonical["checkov_unrated_findings"]
        ),
    ]
    canonical_by_key = {record_key(record): record for record in canonical_records}
    if not (len(canonical_records) == len(canonical_by_key) == 860):
        raise AssertionError()
    if not (canonical_by_key == source_by_key):
        raise AssertionError()
    suppressed_records = [
        project_canonical_record(record.get("tool", "semgrep"), record)
        for record in canonical["suppressed_explicit_findings"]
    ]
    suppressed_keys = {record_key(record) for record in suppressed_records}
    if not (len(suppressed_records) == len(suppressed_keys) == 13):
        raise AssertionError()
    if not (suppressed_keys.isdisjoint(canonical_by_key)):
        raise AssertionError()
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
    if not (hashlib.sha256(ownership_bytes).hexdigest() == args.ownership_sha256):
        raise AssertionError()
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
    if not (
        dict(Counter(record[field] for record in dispositions))
        == disposition_data[metadata_key]
    ):
        raise AssertionError()

workflow_records = [
    record
    for record in dispositions
    if record["group"] == "workflow-shell-interpolation"
]
if not (len(workflow_records) == 54):
    raise AssertionError()
if not (
    Counter(record["verdict"] for record in workflow_records)
    == {
        "needs-followon": 41,
        "false-positive-constrained-context": 11,
        "false-positive-callsite-controlled": 2,
    }
):
    raise AssertionError()
if not (
    all("All 53 interpolate" not in record["rationale"] for record in workflow_records)
):
    raise AssertionError()

scanned_commit = disposition_data["generated_from"]["scanned_commit"]
deploy_eks_records = [
    record for record in workflow_records if record["result_index"] in {50, 51}
]
if not (len(deploy_eks_records) == 2):
    raise AssertionError()
if not (
    all(
        record["verdict"] == "needs-followon"
        and record["assessed_severity"] == "medium"
        and record["owner"] == "FOLLOWON-F"
        and "cross-repository" in record["rationale"]
        for record in deploy_eks_records
    )
):
    raise AssertionError()
deploy_eks_source = subprocess.run(
    ["git", "show", f"{scanned_commit}:.github/workflows/_deploy-eks.yml"],
    cwd=REPOSITORY_ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout
if "workflow_call:" not in deploy_eks_source:
    raise AssertionError()
if "runs-on: arc-runner-org" not in deploy_eks_source:
    raise AssertionError()
if (
    "aws eks update-kubeconfig --name ${{ inputs.cluster_name }}"
    not in deploy_eks_source
):
    raise AssertionError()
if "kubectl apply -f . -n ${{ inputs.namespace }}" not in deploy_eks_source:
    raise AssertionError()
if (
    "deployment/${{ inputs.module }} -n ${{ inputs.namespace }}"
    not in deploy_eks_source
):
    raise AssertionError()

secret_inherit_records = [
    record for record in dispositions if record["group"] == "workflow-secrets-inherit"
]
if not (len(secret_inherit_records) == 9):
    raise AssertionError()
same_repository_secret_record = next(
    record for record in secret_inherit_records if record["result_index"] == 343
)
if not (
    same_repository_secret_record["file"]
    == ".github/workflows/nightly-cli-regression.yml"
    and same_repository_secret_record["verdict"] == "accepted-risk-low"
    and same_repository_secret_record["assessed_severity"] == "low"
    and same_repository_secret_record["owner"] == "S20-reviewed"
    and "same repository" in same_repository_secret_record["rationale"]
):
    raise AssertionError()
cross_repository_secret_records = [
    record
    for record in secret_inherit_records
    if record["result_index"] in set(range(2368, 2376))
]
if not (len(cross_repository_secret_records) == 8):
    raise AssertionError()
if not (
    all(
        record["verdict"] == "needs-followon"
        and record["assessed_severity"] == "medium"
        and record["owner"] == "FOLLOWON-I"
        and "mutable branch ref" in record["rationale"]
        for record in cross_repository_secret_records
    )
):
    raise AssertionError()
client_workflow_readme = subprocess.run(
    [
        "git",
        "show",
        f"{scanned_commit}:modules/agent-factory/client-workflows/README.md",
    ],
    cwd=REPOSITORY_ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout
if "Drop-in GitHub Actions workflows for any repo" not in client_workflow_readme:
    raise AssertionError()
if "reusable workflows must allow calls from your org" not in client_workflow_readme:
    raise AssertionError()
if "Uses `secrets: inherit`" not in client_workflow_readme:
    raise AssertionError()
for record in cross_repository_secret_records:
    caller_source = subprocess.run(
        ["git", "show", f"{scanned_commit}:{record['file']}"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "uses: aws-innovate/adp/.github/workflows/" not in caller_source:
        raise AssertionError()
    if "@main" not in caller_source:
        raise AssertionError()
    if "secrets: inherit" not in caller_source:
        raise AssertionError()
if "FOLLOWON-I" not in disposition_data["proposed_followons"]:
    raise AssertionError()

iam_records = [
    record for record in dispositions if record["group"] == "iam-wildcard-policy"
]
if not (len(iam_records) == 74):
    raise AssertionError()
if not (
    Counter(
        (record["assessed_severity"], record["verdict"], record["owner"])
        for record in iam_records
    )
    == {
        ("medium", "routed-existing-owner", "S12"): 27,
        ("medium", "routed-existing-owner", "S14"): 26,
        ("medium", "routed-existing-owner", "S17"): 3,
        ("none", "accepted-risk-required", "S20-reviewed"): 15,
        ("none", "not-currently-reachable", "S20-reviewed"): 1,
        ("low", "accepted-risk-documented", "S20-reviewed"): 2,
    }
):
    raise AssertionError()
if not (all("S14/S12/S17-overlap" not in record["owner"] for record in iam_records)):
    raise AssertionError()
if not (
    all(
        "Individual verdicts require" not in record["rationale"]
        for record in iam_records
    )
):
    raise AssertionError()

routed_records = [
    record for record in dispositions if record["verdict"] == "routed-existing-owner"
]
existing_owner_records = [
    record for record in dispositions if record["owner"] in {"S12", "S14", "S17", "S21"}
]
unowned_followon_records = [
    record for record in dispositions if record["owner"].startswith("FOLLOWON-")
]
if not (len(routed_records) == 61):
    raise AssertionError()
if not (len(existing_owner_records) == 62):
    raise AssertionError()
if not (len(unowned_followon_records) == 334):
    raise AssertionError()
ownership_evidence = json.loads(OWNERSHIP_EVIDENCE_PATH.read_text())
validate_bundled_ownership_evidence(
    ownership_evidence, dispositions, disposition_data, record_key
)
if args.ownership_contracts:
    ownership_contract_bytes = args.ownership_contracts.read_bytes()
    ownership_contract_sha256 = args.ownership_contracts_sha256
else:
    ownership_contract_bytes = OWNERSHIP_CONTRACTS_PATH.read_bytes()
    ownership_contract_sha256 = OWNERSHIP_CONTRACTS_SHA256
if not (
    hashlib.sha256(ownership_contract_bytes).hexdigest() == ownership_contract_sha256
):
    raise AssertionError()
ownership_contracts = json.loads(ownership_contract_bytes)
validate_ownership_contract_projection(
    ownership_contracts,
    existing_owner_records,
    unowned_followon_records,
    record_key,
    scanned_commit,
)
if ownership_text is not None:
    validate_ownership_plan(
        ownership_text,
        existing_owner_records,
        unowned_followon_records,
        record_key,
    )

ecr_records = [record for record in dispositions if record["rule_id"] == "CKV_AWS_51"]
if not (
    {record["result_index"] for record in ecr_records} == {106, 196, 231, 233, 235, 237}
):
    raise AssertionError()
if not (
    Counter(
        (record["verdict"], record["assessed_severity"], record["owner"])
        for record in ecr_records
    )
    == {
        ("routed-existing-owner", "medium", "S21"): 4,
        ("routed-existing-owner", "medium", "S17"): 1,
        ("needs-followon", "medium", "FOLLOWON-G"): 1,
    }
):
    raise AssertionError()

eks_version_records = [
    record for record in dispositions if record["rule_id"] == "CKV_AWS_339"
]
if not ({record["result_index"] for record in eks_version_records} == {59, 110, 239}):
    raise AssertionError()
if not (
    all(
        record["verdict"] == "false-positive-resolved-configuration"
        for record in eks_version_records
    )
):
    raise AssertionError()
if not (all(record["assessed_severity"] == "none" for record in eks_version_records)):
    raise AssertionError()

gateway_xml_records = [
    record
    for record in dispositions
    if record["tool"] == "semgrep" and record["result_index"] in {10316, 10317, 10318}
]
if not (len(gateway_xml_records) == 3):
    raise AssertionError()
if not (
    all(
        record["verdict"] == "false-positive-constrained-context"
        for record in gateway_xml_records
    )
):
    raise AssertionError()
if not (
    all(
        record["assessed_severity"] == "none" and record["owner"] == "S20-reviewed"
        for record in gateway_xml_records
    )
):
    raise AssertionError()

if not (not any(record["owner"] == "FOLLOWON-D" for record in dispositions)):
    raise AssertionError()
if not (
    Counter(
        record["owner"]
        for record in dispositions
        if record["owner"].startswith("FOLLOWON-D-")
    )
    == {
        "FOLLOWON-D-IMDSV2": 1,
        "FOLLOWON-D-DEFAULT-SG": 3,
        "FOLLOWON-D-SECRET-ROTATION": 15,
    }
):
    raise AssertionError()

backend_records = [
    record for record in dispositions if record["group"] == "terraform-backend"
]
if not (len(backend_records) == 14):
    raise AssertionError()
if not (
    Counter(
        (record["verdict"], record["assessed_severity"], record["owner"])
        for record in backend_records
    )
    == {
        ("false-positive-partial-backend", "none", "S20-reviewed"): 12,
        ("accepted-risk-low", "low", "S20-reviewed"): 1,
        ("needs-followon", "medium", "FOLLOWON-H"): 1,
    }
):
    raise AssertionError()
agent_context_backend = next(
    record for record in backend_records if record["result_index"] == 0
)
if not (agent_context_backend["file"] == "modules/agent-context/terraform/backend.tf"):
    raise AssertionError()
if "deploy.sh" not in agent_context_backend["rationale"]:
    raise AssertionError()
backend_by_index = {record["result_index"]: record for record in backend_records}
backend_evidence_paths = {
    102: ".github/workflows/cyber-windows-image-build.yml",
    125: "modules/domain-apps/superplane/infra/workspaces/scripts/prepare_workspace_plan.py",
    179: "modules/research/gbrain/scripts/deploy.sh",
    250: "platform/scripts/release/bootstrap.py",
}
for result_index, evidence_path in backend_evidence_paths.items():
    if evidence_path not in backend_by_index[result_index]["rationale"]:
        raise AssertionError()
runner_vpc_module = next(
    record for record in backend_records if record["result_index"] == 76
)
if not (runner_vpc_module["rule_id"] == "CKV_TF_1"):
    raise AssertionError()
if not (
    runner_vpc_module["file"]
    == "modules/agent-factory/runner-infra/infrastructure/vpc.tf"
):
    raise AssertionError()

agent_context_deploy = subprocess.run(
    ["git", "show", f"{scanned_commit}:modules/agent-context/deploy.sh"],
    cwd=REPOSITORY_ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout
if not (agent_context_deploy.count("terraform init -upgrade") == 2):
    raise AssertionError()
if not ("-backend-config" not in agent_context_deploy):
    raise AssertionError()


def scanned_source(path):
    return subprocess.run(
        ["git", "show", f"{scanned_commit}:{path}"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


codebuild_records = [
    record for record in dispositions if record["rule_id"] == "CKV_AWS_316"
]
codebuild_indices_by_file = {
    "modules/agent-context/terraform/modules/images-build/main.tf": {8, 9, 10, 11, 12},
    "modules/research/gbrain/terraform/modules/build/main.tf": {183},
    "platform/infra/modules/codebuild/main.tf": {
        220,
        222,
        223,
        224,
        225,
        226,
        227,
        228,
        229,
        230,
    },
}
if not (len(codebuild_records) == 16):
    raise AssertionError()
for source_file, result_indices in codebuild_indices_by_file.items():
    package_records = [
        record for record in codebuild_records if record["file"] == source_file
    ]
    if not ({record["result_index"] for record in package_records} == result_indices):
        raise AssertionError()
    if not (
        all(
            record["verdict"] == "accepted-risk-required"
            and record["assessed_severity"] == "low"
            and record["owner"] == "S20-reviewed"
            for record in package_records
        )
    ):
        raise AssertionError()
if not (
    all(
        "4 CodeBuild projects" not in record["rationale"]
        for record in codebuild_records
    )
):
    raise AssertionError()

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
agent_context_project_block = agent_context_codebuild.split("images = {", 1)[1].split(
    "\n  }\n}", 1
)[0]
if not (
    set(
        re.findall(r'^\s+"([^"]+)"\s+=\s+\{', agent_context_project_block, re.MULTILINE)
    )
    == agent_context_projects
):
    raise AssertionError()
if "service_role = var.codebuild_service_role_arn" not in agent_context_codebuild:
    raise AssertionError()
if "privileged_mode             = true" not in agent_context_codebuild:
    raise AssertionError()
agent_context_root = scanned_source("modules/agent-context/terraform/main.tf")
if (
    "codebuild_service_role_arn = data.terraform_remote_state.platform.outputs.codebuild_role_arn"
    not in agent_context_root
):
    raise AssertionError()

platform_codebuild = scanned_source("platform/infra/modules/codebuild/main.tf")
platform_project_block = platform_codebuild.split("projects = {", 1)[1].split(
    "\n  }\n}", 1
)[0]
if not (
    set(re.findall(r'^\s+"([^"]+)"\s+=\s+\{', platform_project_block, re.MULTILINE))
    == platform_projects
):
    raise AssertionError()
if (
    'policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"'
    not in platform_codebuild
):
    raise AssertionError()
if "service_role  = aws_iam_role.codebuild.arn" not in platform_codebuild:
    raise AssertionError()
if "privileged_mode             = true" not in platform_codebuild:
    raise AssertionError()
for required_capability in (
    "ECR push",
    "S3 read",
    "CloudWatch Logs",
    "EKS describe",
    "Secrets Manager read",
):
    if required_capability not in platform_codebuild:
        raise AssertionError()
if "codebuild_security_scan_upload" not in platform_codebuild:
    raise AssertionError()
platform_codebuild_outputs = scanned_source(
    "platform/infra/modules/codebuild/outputs.tf"
)
if "value       = aws_iam_role.codebuild.arn" not in platform_codebuild_outputs:
    raise AssertionError()

agent_context_rationales = [
    record["rationale"]
    for record in codebuild_records
    if record["file"] == "modules/agent-context/terraform/modules/images-build/main.tf"
]
if not (
    all(
        "five agent-context projects" in rationale
        for rationale in agent_context_rationales
    )
):
    raise AssertionError()
if not (all("all 15 consumers" in rationale for rationale in agent_context_rationales)):
    raise AssertionError()
platform_rationales = [
    record["rationale"]
    for record in codebuild_records
    if record["file"] == "platform/infra/modules/codebuild/main.tf"
]
if not (all("ten platform projects" in rationale for rationale in platform_rationales)):
    raise AssertionError()
if not (all("all 15 consumers" in rationale for rationale in platform_rationales)):
    raise AssertionError()

gbrain_codebuild = scanned_source(
    "modules/research/gbrain/terraform/modules/build/main.tf"
)
if 'resource "aws_codebuild_project" "gbrain_build"' not in gbrain_codebuild:
    raise AssertionError()
if "service_role = aws_iam_role.codebuild.arn" not in gbrain_codebuild:
    raise AssertionError()
if "privileged_mode             = true" not in gbrain_codebuild:
    raise AssertionError()
for scoped_policy in ("codebuild_ecr", "codebuild_logs", "codebuild_s3_source"):
    if f'resource "aws_iam_role_policy" "{scoped_policy}"' not in gbrain_codebuild:
        raise AssertionError()
gbrain_record = next(
    record for record in codebuild_records if record["result_index"] == 183
)
if (
    "does not use the platform AdministratorAccess role"
    not in gbrain_record["rationale"]
):
    raise AssertionError()

administrator_record = next(
    record
    for record in dispositions
    if record["rule_id"] == "CKV_AWS_274" and record["result_index"] == 219
)
if not (administrator_record["owner"] == "S14"):
    raise AssertionError()
if not (administrator_record["verdict"] == "needs-followon"):
    raise AssertionError()
if "all 15 consumers" not in administrator_record["rationale"]:
    raise AssertionError()
if "not only ECR and logs" not in administrator_record["rationale"]:
    raise AssertionError()

cyber_image_workflow = scanned_source(backend_evidence_paths[102])
if "TF_DIR: modules/domain-apps/cyber/image-builder" not in cyber_image_workflow:
    raise AssertionError()
if not (
    cyber_image_workflow.count('-backend-config="dynamodb_table=adp-terraform-locks"')
    == 2
):
    raise AssertionError()

superplane_prepare = scanned_source(backend_evidence_paths[125])
superplane_workspace_readme = scanned_source(
    "modules/domain-apps/superplane/infra/workspaces/README.md"
)
if 'parser.add_argument("--" + name, required=True)' not in superplane_prepare:
    raise AssertionError()
if '"dynamodb_table": args.lock_table' not in superplane_prepare:
    raise AssertionError()
if '"-backend-config=" + backend_file.name' not in superplane_prepare:
    raise AssertionError()
if "--lock-table adp-terraform-locks" not in superplane_workspace_readme:
    raise AssertionError()

gbrain_deploy = scanned_source(backend_evidence_paths[179])
if 'TF_DIR="${MODULE_DIR}/terraform"' not in gbrain_deploy:
    raise AssertionError()
if '-backend-config="dynamodb_table=adp-terraform-locks"' not in gbrain_deploy:
    raise AssertionError()

release_bootstrap = scanned_source(backend_evidence_paths[250])
if "directory = ROOT / 'platform/release-infra'" not in release_bootstrap:
    raise AssertionError()
if "'-backend-config=dynamodb_table=adp-terraform-locks'" not in release_bootstrap:
    raise AssertionError()
if "cwd=directory" not in release_bootstrap:
    raise AssertionError()

runner_vpc_source = subprocess.run(
    ["git", "show", f"{scanned_commit}:{runner_vpc_module['file']}"],
    cwd=REPOSITORY_ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout
if 'source  = "terraform-aws-modules/vpc/aws"' not in runner_vpc_source:
    raise AssertionError()
if 'version = "~> 5.0"' not in runner_vpc_source:
    raise AssertionError()

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
    if not (int(table_rows[group][1]) == count):
        raise AssertionError()
    if not (int(table_rows[group][2]) == len(group_files[group])):
        raise AssertionError()
    if not (
        table_rows[group][3] == summarize(group_records[group], "assessed_severity")
    ):
        raise AssertionError()
    if not (table_rows[group][4] == summarize(group_records[group], "verdict")):
        raise AssertionError()
    if not (table_rows[group][5] == summarize(group_records[group], "owner")):
        raise AssertionError()

for severity, count in disposition_data["severity_counts"].items():
    if f"| {severity} | {count} |" not in readme:
        raise AssertionError()

reconciliation = disposition_data["reconciliation"]
if not (
    reconciliation
    == {
        "source_semgrep": 234,
        "source_checkov": 626,
        "source_total": 860,
        "dispositioned_total": 860,
        "unique_keys": 860,
        "unaccounted": 0,
    }
):
    raise AssertionError()

print("S20 validation passed: 860/860 source records, 0 missing, 0 extra")
print(
    f"Normalized source integrity passed: topic revision {TOPIC_SOURCE_REVISION}, sha256 {SOURCE_EXPORT_SHA256}"
)
if canonical_source:
    print(f"Canonical source provenance passed: {canonical_source}")
else:
    print(
        "Canonical source provenance not run; use --require-canonical for the attested provenance check"
    )
print(
    "Hash-pinned ownership contracts passed: 62 existing-owner records and "
    "334 unowned follow-on records resolved across S01-S21"
)
if ownership_text is not None:
    print(f"Optional source-plan semantic comparison passed: {ownership_source}")
elif args.require_ownership_plan:
    print(f"Ownership requirement satisfied by {ownership_source}")
else:
    print(
        "Optional verbatim source-plan comparison not run; hash-pinned contract validation passed"
    )
print("IAM triage passed: 56 routed records, 18 required/inert/documented records")
print("Workflow triage passed: 41 reachable shell follow-ons, 13 constrained records")
print(
    "Workflow secret triage passed: 8 cross-repository follow-ons, 1 same-repository accepted risk"
)
print(
    "Review repairs passed: CodeBuild role mapping, workflow boundaries, backend locking, "
    "ECR integrity, EKS versions, gateway XML, and split infra follow-ons"
)
