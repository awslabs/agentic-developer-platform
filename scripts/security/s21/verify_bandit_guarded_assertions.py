"""Verify exact Bandit assertions dominated by non-removable validation guards."""

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path

from verify_secret_git_objects import git, require

HELPER_FILE = "modules/domain-apps/superplane/superplane_acceptance/cli_delivery.py"


def normalized(node):
    return ast.dump(node, include_attributes=False)


def guarded_assertion(text, line):
    tree = ast.parse(text)
    imports = [
        n
        for n in tree.body
        if isinstance(n, ast.ImportFrom)
        and n.level == 1
        and n.module == "cli_delivery"
        and any(a.name == "require" and a.asname is None for a in n.names)
    ]
    require(len(imports) == 1, "Explicit guard import missing")
    for node in ast.walk(tree):
        require(
            not (
                isinstance(node, ast.Name)
                and node.id == "require"
                and isinstance(node.ctx, ast.Store)
            ),
            "Guard helper rebound",
        )
        require(
            not (isinstance(node, ast.arg) and node.arg == "require"),
            "Guard helper shadowed",
        )
        require(
            not (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == "require"
            ),
            "Guard helper redefined",
        )
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node is not imports[0]:
            require(
                not any(
                    (a.asname or a.name.split(".")[0]) == "require" for a in node.names
                ),
                "Conflicting guard import",
            )
    assertions = [
        n for n in ast.walk(tree) if isinstance(n, ast.Assert) and n.lineno == line
    ]
    require(len(assertions) == 1, "Exact original assertion missing")
    assertion = assertions[0]
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    parent = parents[assertion]
    statements = next(
        (
            value
            for _, value in ast.iter_fields(parent)
            if isinstance(value, list) and assertion in value
        ),
        None,
    )
    require(
        statements is not None and statements.index(assertion) > 0,
        "No directly preceding guard",
    )
    previous = statements[statements.index(assertion) - 1]
    require(
        isinstance(previous, ast.Expr) and isinstance(previous.value, ast.Call),
        "Assertion not immediately dominated by guard",
    )
    call = previous.value
    require(
        isinstance(call.func, ast.Name)
        and call.func.id == "require"
        and len(call.args) == 2
        and not call.keywords,
        "Not the reviewed guard call",
    )
    predicate = call.args[0]
    conjuncts = (
        predicate.values
        if isinstance(predicate, ast.BoolOp) and isinstance(predicate.op, ast.And)
        else [predicate]
    )
    require(
        normalized(conjuncts[0]) == normalized(assertion.test),
        "Guard does not enforce assertion predicate",
    )
    owner = parent
    while not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        require(owner in parents, "Assertion is not inside a reviewed function")
        owner = parents[owner]
    return {
        "guard_line": previous.lineno,
        "function": owner.name,
        "predicate_ast_sha256": hashlib.sha256(
            normalized(assertion.test).encode()
        ).hexdigest(),
    }


def verify_helper(text):
    tree = ast.parse(text)
    helpers = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "require"
    ]
    require(len(helpers) == 1, "Guard helper missing")
    helper = helpers[0]
    require(
        [a.arg for a in helper.args.args] == ["condition", "message"],
        "Guard helper arguments changed",
    )
    expected = ast.parse("if not condition:\n    raise EvidenceError(message)\n").body
    require(
        [normalized(n) for n in helper.body] == [normalized(n) for n in expected],
        "Helper is not an explicit non-removable failure",
    )
    errors = [
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "EvidenceError"
    ]
    require(
        len(errors) == 1
        and len(errors[0].bases) == 1
        and isinstance(errors[0].bases[0], ast.Name)
        and errors[0].bases[0].id == "RuntimeError",
        "Guard exception definition mismatch",
    )


def verify(source, scan_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    records = receipt["reviewed_records"]
    require(
        records and len(records) == receipt["reviewed_delta"], "Receipt count mismatch"
    )
    require(
        len({r["selector"] for r in records}) == len(records),
        "Duplicate original selector",
    )
    revision = receipt["source_revision"]
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Invalid source revision")
    sarif = json.loads(scan_path.read_text())
    require(
        hashlib.sha256(scan_path.read_bytes()).hexdigest()
        == receipt["original_sarif_sha256"],
        "Original scan artifact mismatch",
    )
    blobs = {}

    def blob(path):
        if path not in blobs:
            blobs[path] = git(source, "show", revision + ":" + path)
        return blobs[path]

    verify_helper(blob(HELPER_FILE))
    for record in records:
        match = re.fullmatch(
            r"bandit\|bandit-results.sarif\|run=(\d+)\|ri=(\d+)", record["selector"]
        )
        require(match, "Invalid original selector")
        original = sarif["runs"][int(match[1])]["results"][int(match[2])]
        location = original["locations"][0]["physicalLocation"]
        require(
            original["ruleId"] == record["rule"] == "B101"
            and original["properties"]["issue_severity"] == record["severity"]
            and original["level"] == record["sarif_level"]
            and location["artifactLocation"]["uri"] == record["file"]
            and location["region"]["startLine"] == record["line"],
            "Exact original identity/severity mismatch",
        )
        proof = guarded_assertion(blob(record["file"]), record["line"])
        require(proof == record["guard_evidence"], "Guard context proof mismatch")
    print(
        f"Verified {len(records)} exact original assertions with explicit preceding guards"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "scan", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        verify(args.source, args.scan, args.receipt)
    except Exception:  # noqa: BLE001 - source/scan errors may contain private data
        parser.exit(1, "Verification failed; private source details withheld\n")
