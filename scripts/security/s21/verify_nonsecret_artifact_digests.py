"""Verify reviewed digest candidates against private originals without emitting values."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path


def verify_context(record, candidate, source_bytes):
    """Require semantic checksum context as well as byte identity."""
    for evidence in record["context_evidence"]:
        kind = evidence.get("kind", "json_checksum")
        if kind in {"json_checksum", "json_module_digest", "yaml_sha256"}:
            if kind == "yaml_sha256":
                import yaml

                document = yaml.safe_load(source_bytes)
            else:
                document = json.loads(source_bytes)
            path = evidence["json_path"]
            assert path, "Empty context path"
            value = document
            for key in path:
                value = value[key]
            assert value == candidate, "Context does not identify candidate"
            if kind == "json_module_digest":
                assert isinstance(path[-1], str)
                assert any(
                    artifact.endswith("/" + path[-1].replace(".", "/") + ".py")
                    for artifact in record["matching_artifacts"]
                ), "Module does not map to artifact"
            elif kind == "yaml_sha256":
                assert path[-1] == "sha256", "Not a dependency checksum field"
            else:
                explicit = any(
                    isinstance(k, str)
                    and ("sha256" in k.lower() or k.lower() == "file_hashes")
                    for k in path
                )
                linked = isinstance(path[-1], str) and any(
                    artifact == path[-1] or artifact.endswith("/" + path[-1])
                    for artifact in record["matching_artifacts"]
                )
                assert explicit or linked, "No decisive checksum context"
            continue
        tree = ast.parse(source_bytes)
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        constants = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and node.value == candidate
            and node.lineno == record["line"] == evidence["line"]
        ]
        assert len(constants) == 1, "Ambiguous Python candidate context"
        node = constants[0]
        parent = parents[node]
        if kind == "python_keyword_sha256":
            assert isinstance(parent, ast.keyword) and parent.arg == "sha256"
        elif kind == "python_digest_assignment":
            assert isinstance(parent, ast.Assign) and len(parent.targets) == 1
            assert isinstance(parent.targets[0], ast.Name)
            name = parent.targets[0].id
            assert name == evidence["variable"] and "SHA" in name
            usage = set()
            for context in ast.walk(tree):
                if (
                    isinstance(context, ast.Compare)
                    and any(
                        isinstance(c, ast.Name) and c.id == name
                        for c in ast.walk(context)
                    )
                    and any(
                        (isinstance(c, ast.Attribute) and c.attr == "sha256")
                        or (
                            isinstance(c, ast.Constant)
                            and isinstance(c.value, str)
                            and "sha256" in c.value
                        )
                        for c in ast.walk(context)
                    )
                ):
                    usage.add(context.lineno)
                if isinstance(context, ast.Dict):
                    for key, value in zip(context.keys, context.values):
                        if (
                            isinstance(key, ast.Constant)
                            and isinstance(key.value, str)
                            and "sha256" in key.value
                            and isinstance(value, ast.Name)
                            and value.id == name
                        ):
                            usage.add(context.lineno)
            assert (
                evidence["checksum_usage_lines"]
                and set(evidence["checksum_usage_lines"]) <= usage
            )
        elif kind == "python_artifact_dictionary":
            assert isinstance(parent, ast.Dict)
            key = ast.literal_eval(parent.keys[parent.values.index(node)])
            path = key[-1] if isinstance(key, tuple) else key
            assert path == evidence["artifact_key"]
            assert any(
                artifact == path or artifact.endswith("/" + path)
                for artifact in record["matching_artifacts"]
            )
        elif kind == "python_seed_source_digest":
            assert isinstance(parent, ast.Tuple) and parent.elts.index(node) == 13
            collection = parents[parent]
            assignment = parents[collection]
            assert isinstance(assignment, ast.Assign)
            assert (
                isinstance(assignment.targets[0], ast.Name)
                and assignment.targets[0].id == "SEED_ROWS"
            )
            mappings = [
                context
                for context in ast.walk(tree)
                if isinstance(context, ast.Dict)
                and any(
                    isinstance(key, ast.Constant)
                    and key.value == "source_content_sha256"
                    and isinstance(value, ast.Subscript)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "row"
                    and isinstance(value.slice, ast.Constant)
                    and value.slice.value == 13
                    for key, value in zip(context.keys, context.values)
                )
            ]
            assert mappings, "Tuple not mapped to source checksum column"
            assert any(
                isinstance(context, ast.For)
                and isinstance(context.target, ast.Name)
                and context.target.id == "row"
                and isinstance(context.iter, ast.Name)
                and context.iter.id == "SEED_ROWS"
                and any(mapping in list(ast.walk(context)) for mapping in mappings)
                for context in ast.walk(tree)
            ), "Seed checksum mapping is not used"
        else:
            raise AssertionError("Unsupported checksum evidence kind")


def verify(source, scan_path, audit_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    assert revision == receipt["source_revision"], "Wrong frozen source revision"
    scan = json.loads(scan_path.read_text())["results"]
    audit = json.loads(audit_path.read_text())["results"]
    audited = {}
    for group in audit:
        candidate = group["secrets"]
        candidate_sha1 = hashlib.sha1(candidate.encode()).hexdigest()
        for line in group["lines"]:
            audited[(group["filename"], int(line), candidate_sha1)] = candidate
    records = receipt["verified_records"]
    assert records, "Empty verification receipt"
    selectors = [record["selector"] for record in records]
    assert len(selectors) == len(set(selectors)), "Duplicate original selectors"
    assert len(records) == receipt["verified_delta"], "Receipt count mismatch"
    for record in records:
        assert record.get("matching_artifacts"), "Missing artifact evidence"
        assert record.get("context_evidence"), "Missing checksum context evidence"
    frozen = {}

    def frozen_bytes(path, source_revision=revision):
        key = (source_revision, path)
        if key not in frozen:
            # Read committed blobs, never mutable checkout files. Missing or
            # untracked evidence refuses verification rather than falling back.
            frozen[key] = subprocess.check_output(
                ["git", "show", f"{source_revision}:{path}"],
                cwd=source,
                stderr=subprocess.DEVNULL,
            )
        return frozen[key]

    digests = {}
    checked_ancestors = set()
    for record in receipt["verified_records"]:
        # Join private full candidate hashes, not prefix/path heuristics.
        originals = [
            r
            for r in scan[record["file"]]
            if r["line_number"] == record["line"]
            and r["type"] == record["detector"]
            and r["hashed_secret"].startswith(record["candidate_hash_prefix"])
        ]
        assert len(originals) == 1, "Ambiguous original scan selector"
        original = originals[0]
        candidate = audited[(record["file"], record["line"], original["hashed_secret"])]
        assert hashlib.sha1(candidate.encode()).hexdigest() == original["hashed_secret"]
        lines = frozen_bytes(record["file"]).decode().splitlines()
        assert candidate in lines[record["line"] - 1], (
            "Candidate missing at exact frozen line"
        )
        historical = record.get("historical_artifacts", [])
        historical_by_path = {entry["historical_path"]: entry for entry in historical}
        assert len(historical_by_path) == len(historical), (
            "Duplicate historical artifact proof"
        )
        if historical:
            assert set(historical_by_path) == set(record["matching_artifacts"]), (
                "Incomplete historical provenance"
            )
        for artifact in record["matching_artifacts"]:
            source_revision = revision
            if artifact in historical_by_path:
                proof = historical_by_path[artifact]
                source_revision = proof["source_revision"]
                assert re.fullmatch(r"[0-9a-f]{40}", source_revision), (
                    "Invalid historical revision"
                )
                assert re.fullmatch(r"[0-9a-f]{40}", proof["blob_oid"]), (
                    "Invalid historical blob identity"
                )
                if source_revision not in checked_ancestors:
                    result = subprocess.run(
                        [
                            "git",
                            "merge-base",
                            "--is-ancestor",
                            source_revision,
                            revision,
                        ],
                        cwd=source,
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    assert result.returncode == 0, (
                        "Historical revision is not an ancestor of frozen source"
                    )
                    checked_ancestors.add(source_revision)
                blob_oid = subprocess.check_output(
                    ["git", "rev-parse", f"{source_revision}:{artifact}"],
                    cwd=source,
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                assert blob_oid == proof["blob_oid"], (
                    "Historical commit:path does not resolve to recorded blob"
                )
            key = (source_revision, artifact)
            if key not in digests:
                digests[key] = hashlib.sha256(
                    frozen_bytes(artifact, source_revision)
                ).hexdigest()
            assert candidate == digests[key], "Candidate is not the artifact SHA256"
        verify_context(record, candidate, frozen_bytes(record["file"]))
    print(
        f"Verified {len(receipt['verified_records'])} original selectors; no candidate values emitted"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--scan", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    verify(args.source, args.scan, args.audit, args.receipt)
