"""Verify identifier/reference findings without resolving or displaying resources."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path


def arn_service(candidate):
    match = re.fullmatch(
        r"arn:aws(?:-cn|-us-gov)?:(secretsmanager|kms):[a-z]{2}-[a-z]+-[0-9]:[0-9]+:(.+)",
        candidate,
    )
    assert match, "Not a complete supported reference shape"
    service, resource = match.groups()
    assert service == "secretsmanager", "No reviewed consumer proof for this service"
    pattern = (
        r"secret:[A-Za-z0-9/_+=.@*-]+"
        if service == "secretsmanager"
        else r"key/[A-Za-z0-9-]+"
    )
    assert re.fullmatch(pattern, resource), "Invalid resource identifier shape"
    # Short numeric test-account placeholders remain references, not valid live
    # ARN claims. No API lookup, authentication or SecretString retrieval occurs.
    return service


def reference_field(field):
    return isinstance(field, str) and (
        field == "SecretId" or field.lower().endswith("_arn")
    )


def context_proofs(text, candidate, line):
    tree = ast.parse(text)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }

    def scope(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return node
        return tree

    def direct(node):
        parent = parents[node]
        if isinstance(parent, ast.keyword) and reference_field(parent.arg):
            return {"kind": "keyword", "field": parent.arg, "line": node.lineno}
        if isinstance(parent, ast.Dict):
            for key, value in zip(parent.keys, parent.values):
                if (
                    value is node
                    and isinstance(key, ast.Constant)
                    and reference_field(key.value)
                ):
                    return {
                        "kind": "dictionary",
                        "field": key.value,
                        "line": node.lineno,
                    }
        if isinstance(parent, ast.Assign):
            for target in parent.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and reference_field(target.slice.value)
                ):
                    return {
                        "kind": "mapping_assignment",
                        "field": target.slice.value,
                        "line": node.lineno,
                    }
        if isinstance(parent, ast.arguments):
            args = [*parent.posonlyargs, *parent.args]
            for argument, default in zip(
                args[-len(parent.defaults) :], parent.defaults
            ):
                if default is node and reference_field(argument.arg):
                    return {
                        "kind": "parameter_default",
                        "field": argument.arg,
                        "line": node.lineno,
                    }
        return None

    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and node.value == candidate
        and node.lineno <= line <= node.end_lineno
    ]
    assert matches, "No exact literal at original line"
    proofs = []
    for node in matches:
        proof = direct(node)
        if proof:
            proofs.append(proof)
            continue
        parent = parents[node]
        if not (
            isinstance(parent, ast.Assign)
            and len(parent.targets) == 1
            and isinstance(parent.targets[0], ast.Name)
        ):
            continue
        name = parent.targets[0].id
        binding_scope = scope(parent)
        for use in ast.walk(tree):
            if not (
                isinstance(use, ast.Name)
                and isinstance(use.ctx, ast.Load)
                and use.id == name
            ):
                continue
            use_scope = scope(use)
            if binding_scope is not tree and use_scope is not binding_scope:
                continue
            if binding_scope is tree and use_scope is not tree:
                shadowed = any(
                    isinstance(n, ast.Name)
                    and isinstance(n.ctx, ast.Store)
                    and n.id == name
                    for n in ast.walk(use_scope)
                )
                shadowed |= any(
                    isinstance(n, ast.arg) and n.arg == name
                    for n in ast.walk(use_scope)
                )
                if shadowed:
                    continue
            proof = direct(use)
            if proof:
                proofs.append(dict(proof, variable=name, definition_line=node.lineno))
    assert proofs, "No bound resource-reference field or API argument"
    return proofs


def verify(source, scan_path, audit_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    records = receipt["verified_records"]
    assert records and len(records) == receipt["verified_delta"]
    assert len({r["selector"] for r in records}) == len(records)
    scan = json.loads(scan_path.read_text())["results"]
    audit = json.loads(audit_path.read_text())["results"]
    candidates = {}
    for group in audit:
        digest = hashlib.sha1(group["secrets"].encode()).hexdigest()
        for line in group["lines"]:
            candidates[group["filename"], int(line), digest] = group["secrets"]
    blobs = {}

    def blob(path):
        if path not in blobs:
            blobs[path] = subprocess.check_output(
                ["git", "show", f"{receipt['source_revision']}:{path}"],
                cwd=source,
                stderr=subprocess.DEVNULL,
            )
        return blobs[path]

    assert receipt["consumer_evidence"], "Missing identifier-consumer evidence"
    for consumer in receipt["consumer_evidence"]:
        tree = ast.parse(blob(consumer["file"]))
        matches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_secret_value"
            and any(k.arg == "SecretId" for k in node.keywords)
        ]
        assert matches and consumer["line"] in {node.lineno for node in matches}, (
            "Missing SecretId API consumer"
        )
    for record in records:
        originals = [
            r
            for r in scan[record["file"]]
            if r["line_number"] == record["line"]
            and r["type"] == record["detector"]
            and r["hashed_secret"].startswith(record["candidate_hash_prefix"])
        ]
        assert len(originals) == 1, "Ambiguous original selector"
        candidate = candidates[
            record["file"], record["line"], originals[0]["hashed_secret"]
        ]
        assert (
            hashlib.sha1(candidate.encode()).hexdigest()
            == originals[0]["hashed_secret"]
        )
        assert arn_service(candidate) == record["service"]
        text = blob(record["file"]).decode()
        assert candidate in text.splitlines()[record["line"] - 1]
        assert record["context_evidence"], "Missing reference field evidence"
        if record["file"].endswith(".py"):
            actual = context_proofs(text, candidate, record["line"])
            assert all(proof in actual for proof in record["context_evidence"])
        else:
            document = json.loads(text)
            for proof in record["context_evidence"]:
                path = proof["json_path"]
                assert path and path[-1] == "SecretId"
                value = document
                for key in path:
                    value = value[key]
                assert value == candidate
    print(
        f"Verified {len(records)} original resource-reference selectors; no values emitted or resources queried"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "scan", "audit", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    verify(args.source, args.scan, args.audit, args.receipt)
