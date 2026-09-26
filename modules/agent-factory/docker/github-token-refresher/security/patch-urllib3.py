"""Backport urllib3 1.26.18's CVE-2023-45803 redirect-body fix to AL2023.

Keep AL2023's other downstream security patches. Refuse changed source for review.
Upstream behavior: discard body and entity headers when 303 changes POST to GET.
"""

import hashlib
import json
from pathlib import Path

import urllib3.connectionpool
import urllib3.poolmanager

HEADERS = (
    "content-encoding",
    "content-language",
    "content-location",
    "content-type",
    "content-length",
    "digest",
    "last-modified",
)
SOURCES = [
    (
        urllib3.poolmanager,
        "1daa6214d2e117ecd3f3b5b3b93f19c92b5c040a99a44e76bd706fcb2e0a8cdf",
        8,
        'kw["body"] = None',
        'kw["headers"]',
    ),
    (
        urllib3.connectionpool,
        "b489f7f59f3cb30258da6aa7066f16d1c259a93229b7a5083ab45d6304d439f9",
        12,
        "body = None",
        "headers",
    ),
]
receipt = []
for module, expected, indent, body, headers in SOURCES:
    path = Path(module.__file__)
    source = path.read_text()
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f"Review changed vendor source before applying redirect fix: {path}")
    pad = " " * indent
    old = pad + "if response.status == 303:\n" + pad + '    method = "GET"\n'
    if source.count(old) != 1:
        raise RuntimeError(f"Ambiguous redirect patch target: {path}")
    new = old + pad + "    # CVE-2023-45803: upstream urllib3 1.26.18 behavior.\n"
    new += pad + "    " + body + "\n"
    # HTTPHeaderDict copy retains repeated non-entity fields and leaves the caller intact.
    new += pad + "    from ._collections import HTTPHeaderDict\n"
    new += pad + "    " + headers + " = HTTPHeaderDict(" + headers + " or {})\n"
    new += pad + "    for entity_header in " + repr(HEADERS) + ":\n"
    new += pad + "        " + headers + ".discard(entity_header)\n"
    path.write_text(source.replace(old, new))
    patched = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt.append({"path": str(path), "before_sha256": expected, "after_sha256": patched})
    print(path.name, patched)

output = Path("/usr/share/adp-security/urllib3-redirect.json")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(
    json.dumps({"cve": "CVE-2023-45803", "upstream": "urllib3 1.26.18", "files": receipt}, indent=2)
    + "\n"
)
