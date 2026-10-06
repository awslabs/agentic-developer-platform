#!/usr/bin/env python3
"""Review exact-candidate SARIF inventories without claiming scanner absence is a fix.

Inputs and output may contain private scan data: keep them outside the public repo.
A verified evidence file is necessary, not sufficient, for a human disposition.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
ACTIVE = {"critical", "high"}
RESOLVED = {"fixed", "evidence-reviewed-not-applicable"}


def finding_key(row: dict) -> tuple:
    return (row["advisory"], row["severity"], tuple(row["paths"]), row.get("rule_help", "") if not row["paths"] else "")


def active_rows(inventory: dict) -> list[dict]:
    return [
        row for row in inventory["occurrences"]
        if row["severity"] in ACTIVE and not row["accepted_suppression"]
    ]


def validate_review(row: dict, review: dict, image_digest: str, evidence_dir: Path) -> None:
    status = review.get("status")
    if status not in RESOLVED:
        raise ValueError(f"invalid resolved disposition for {row['id']}")
    if (
        review.get("candidate_image") != image_digest
        or review.get("advisory") != row["advisory"]
        or review.get("paths") != row["paths"]
    ):
        raise ValueError(f"candidate/advisory/path mismatch for {row['id']}")
    for field in ("package", "installed_version"):
        if not isinstance(review.get(field), str) or not review[field].strip():
            raise ValueError(f"missing {field} for {row['id']}")
    installed_paths = review.get("installed_paths")
    if (
        not isinstance(installed_paths, list)
        or not installed_paths
        or any(not isinstance(path, str) or not path.strip() for path in installed_paths)
        or (row["paths"] and installed_paths != row["paths"])
    ):
        raise ValueError(f"missing or mismatched installed paths for {row['id']}")
    if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
        raise ValueError(f"missing reviewer for {row['id']}")
    if status == "fixed":
        for field in ("binary_sha256", "source_or_patch_sha256"):
            if not SHA.fullmatch(review.get(field, "")):
                raise ValueError(f"missing {field} for {row['id']}")
    elif not isinstance(review.get("reason"), str) or not review["reason"].strip():
        raise ValueError(f"missing non-applicability reason for {row['id']}")

    evidence = review.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(f"missing evidence for {row['id']}")
    for entry in evidence:
        if not isinstance(entry, dict) or not SHA.fullmatch(entry.get("sha256", "")):
            raise ValueError(f"invalid evidence hash for {row['id']}")
        path = (evidence_dir / entry["path"]).resolve()
        if not path.is_relative_to(evidence_dir.resolve()) or not path.is_file():
            raise ValueError(f"missing or out-of-tree evidence for {row['id']}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"evidence hash mismatch for {row['id']}")


def review_candidate(
    baseline: dict, candidate: dict, reviewed: dict, image_digest: str, evidence_dir: Path
) -> dict:
    if not DIGEST.fullmatch(image_digest):
        raise ValueError("candidate image must have an immutable SHA-256 digest")
    if not SHA.fullmatch(baseline.get("report_sha256", "")) or not SHA.fullmatch(
        candidate.get("report_sha256", "")
    ):
        raise ValueError("inventories must be bound to original SARIF hashes")
    original = active_rows(baseline)
    current = active_rows(candidate)
    original_ids = [row["id"] for row in original]
    if len(original_ids) != len(set(original_ids)):
        raise ValueError("duplicate baseline occurrence IDs")
    if not isinstance(reviewed, dict) or set(reviewed) - set(original_ids):
        raise ValueError("review contains unassigned occurrences")

    dispositions = []
    for row in original:
        review = reviewed.get(row["id"])
        if review is not None:
            validate_review(row, review, image_digest, evidence_dir)
        dispositions.append(
            {
                "id": row["id"],
                "advisory": row["advisory"],
                "severity": row["severity"],
                "paths": row["paths"],
                "status": review["status"] if review else "unresolved",
                "review": review,
            }
        )

    previously = Counter(map(finding_key, original))
    additions = []
    for row in current:
        key = finding_key(row)
        if previously[key]:
            previously[key] -= 1
        else:
            additions.append(row)
    return {
        "baseline_report_sha256": baseline["report_sha256"],
        "candidate_report_sha256": candidate["report_sha256"],
        "candidate_image": image_digest,
        "assigned_count": len(original),
        "accepted_suppressions": baseline["accepted_suppression_count"],
        "dispositions": dispositions,
        "new_critical_high": additions,
        "complete": not additions and all(
            row["status"] in RESOLVED for row in dispositions
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("reviewed", type=Path)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = review_candidate(
        json.loads(args.baseline.read_text()),
        json.loads(args.candidate.read_text()),
        json.loads(args.reviewed.read_text()),
        args.image_digest,
        args.reviewed.parent,
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if not result["complete"]:
        parser.exit(1, "candidate has unresolved records or new critical/high findings\n")


if __name__ == "__main__":
    main()
