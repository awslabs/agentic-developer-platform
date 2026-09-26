"""Review exact import-only diagnostics without accepting execution boundaries."""

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path

from verify_secret_git_objects import git, require

EXECUTION_RULES = {"B602", "B603", "B604", "B605", "B606", "B607"}


def import_context(text, line):
    tree = ast.parse(text)
    nodes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno == line
    ]
    require(len(nodes) == 1, "Exact import statement missing or ambiguous")
    node = nodes[0]
    if isinstance(node, ast.Import):
        names = [a for a in node.names if a.name == "subprocess"]
        require(len(names) == 1, "Not the subprocess standard-library import")
        bindings = [names[0].asname or names[0].name]
        kind = "import"
    else:
        require(
            node.level == 0 and node.module == "subprocess",
            "Not an absolute subprocess import",
        )
        require(all(a.name != "*" for a in node.names), "Wildcard import not reviewed")
        bindings = [a.asname or a.name for a in node.names]
        kind = "from_import"
    return {
        "kind": kind,
        "bound_names": bindings,
        "start_line": node.lineno,
        "end_line": node.end_lineno,
    }


def original_result(sarif, selector):
    match = re.fullmatch(r"bandit\|bandit-results.sarif\|run=(\d+)\|ri=(\d+)", selector)
    require(match, "Invalid original selector")
    return sarif["runs"][int(match[1])]["results"][int(match[2])]


def verify(source, scan_path, inventory_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    inventory = json.loads(inventory_path.read_text())
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
    raw_scan = scan_path.read_bytes()
    require(
        hashlib.sha256(raw_scan).hexdigest() == receipt["original_sarif_sha256"],
        "Original scan artifact mismatch",
    )
    sarif = json.loads(raw_scan)
    require(
        len(inventory["selectors"]) == inventory["original_count"] == 1470,
        "Original inventory population mismatch",
    )
    by_selector = {r["selector"]: r for r in inventory["selectors"]}
    require(len(by_selector) == 1470, "Duplicate inventory identity")
    blobs = {}
    for record in records:
        original = original_result(sarif, record["selector"])
        location = original["locations"][0]["physicalLocation"]
        require(
            original["ruleId"] == record["rule"] == "B404"
            and original["properties"]["issue_severity"] == record["severity"]
            and original["level"] == record["sarif_level"]
            and location["artifactLocation"]["uri"] == record["file"]
            and location["region"]["startLine"] == record["line"],
            "Exact original import identity/severity mismatch",
        )
        if record["file"] not in blobs:
            blobs[record["file"]] = git(source, "show", revision + ":" + record["file"])
        require(
            import_context(blobs[record["file"]], record["line"])
            == record["import_evidence"],
            "Import context mismatch",
        )
        execution_selectors = record["execution_selectors"]
        require(
            execution_selectors
            and len(set(execution_selectors)) == len(execution_selectors),
            "Missing or duplicate separately owned execution selectors",
        )
        expected = {
            r["selector"]
            for r in inventory["selectors"]
            if r["files"] == [record["file"]] and r["rule"] in EXECUTION_RULES
        }
        require(
            set(execution_selectors) == expected, "Execution coverage links changed"
        )
        for selector in execution_selectors:
            execution = by_selector[selector]
            frozen = original_result(sarif, selector)
            physical = frozen["locations"][0]["physicalLocation"]
            require(
                frozen["ruleId"] == execution["rule"]
                and execution["rule"] in EXECUTION_RULES
                and physical["artifactLocation"]["uri"] == record["file"]
                and physical["region"]["startLine"] == execution["line"]
                and frozen["properties"]["issue_severity"] == execution["severity"]
                and execution["owner_issue"] == 6108,
                "Execution observation identity/owner mismatch",
            )
            require(
                execution["disposition"] == record["execution_dispositions"][selector],
                "Execution disposition must not inherit import review",
            )
    print(
        f"Verified {len(records)} original import-only observations; execution boundaries remain separately owned"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "scan", "inventory", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        verify(args.source, args.scan, args.inventory, args.receipt)
    except Exception:  # noqa: BLE001 - source/scan errors may contain private data
        parser.exit(1, "Verification failed; private source details withheld\n")
