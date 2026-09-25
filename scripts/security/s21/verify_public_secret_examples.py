"""Verify public example/delimiter dispositions without displaying candidate values."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path


class CodeValues(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_code = False
        self.current = []
        self.values = set()

    def handle_starttag(self, tag, attrs):
        if tag == "code":
            self.in_code = True
            self.current = []

    def handle_data(self, data):
        if self.in_code:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "code" and self.in_code:
            self.values.add("".join(self.current))
            self.in_code = False


def verify(source, scan_path, audit_path, receipt_path, public_document):
    receipt = json.loads(receipt_path.read_text())
    records = receipt["verified_records"]
    assert records and len(records) == receipt["verified_delta"]
    assert len({r["selector"] for r in records}) == len(records)
    document = public_document.read_bytes()
    assert hashlib.sha256(document).hexdigest() == receipt["public_document"]["sha256"]
    parser = CodeValues()
    parser.feed(document.decode())
    assert parser.values, "No public code examples"
    scan = json.loads(scan_path.read_text())["results"]
    audit = json.loads(audit_path.read_text())["results"]
    candidates = {}
    for group in audit:
        candidate_hash = hashlib.sha1(group["secrets"].encode()).hexdigest()
        for line in group["lines"]:
            candidates[group["filename"], int(line), candidate_hash] = group["secrets"]
    frozen = {}
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
        if record["file"] not in frozen:
            frozen[record["file"]] = subprocess.check_output(
                ["git", "show", f"{receipt['source_revision']}:{record['file']}"],
                cwd=source,
                stderr=subprocess.DEVNULL,
            )
        text = frozen[record["file"]].decode()
        assert candidate in text.splitlines()[record["line"] - 1]
        if record["kind"] == "aws_public_example":
            assert candidate in parser.values, (
                "Not a complete official documentation example"
            )
        elif record["kind"] == "public_pem_delimiter":
            assert re.fullmatch(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY", candidate)
            literals = [
                node
                for node in ast.walk(ast.parse(text))
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.lineno <= record["line"] <= node.end_lineno
                and node.value.strip().strip("-").strip() == candidate
            ]
            assert literals, "No complete delimiter-only Python literal"
            assert record["literal_lines"] and set(record["literal_lines"]) == {
                n.lineno for n in literals
            }
        else:
            raise AssertionError("Unsupported public-evidence kind")
    print(
        f"Verified {len(records)} original public-example/delimiter selectors; no values emitted"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "scan", "audit", "receipt", "public-document"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    verify(args.source, args.scan, args.audit, args.receipt, args.public_document)
