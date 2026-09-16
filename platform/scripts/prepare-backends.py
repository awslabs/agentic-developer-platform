#!/usr/bin/env python3
"""Bind deployment backend files to the selected account on installs and updates."""
import re
import sys
from pathlib import Path


def prepare(directory, account):
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("Expected a twelve-digit AWS account ID")
    for path in Path(directory).rglob("*.tfvars"):
        text = path.read_text()
        updated = text.replace("ACCOUNT_ID", account)
        if path.name.endswith("backend.tfvars"):
            updated = re.sub(r'(\bbucket\s*=\s*"adp-terraform-state-)[0-9]{12}(\")',
                             lambda m: m[1] + account + m[2], updated)
        if updated != text:
            path.write_text(updated)


if __name__ == "__main__":
    prepare(sys.argv[1], sys.argv[2])
