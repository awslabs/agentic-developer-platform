#!/usr/bin/env python3
"""Select domain build projects without adding optional apps to a base deploy."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def resolve(manifest_root: Path, state_addresses: str, explicit: str | None,
            superplane_enabled: bool) -> list[str]:
    available = {path.parent.parent.name for path in manifest_root.glob("*/codebuild/projects.json")}
    if explicit is None:
        # Retain existing projects in their current Terraform state until an
        # operator migrates or removes them deliberately.
        selected = {
            app for app in available
            if re.search(
                rf'module\.codebuild\.(?:aws_codebuild_project\.main|aws_iam_role(?:_policy)?\.project)\["{re.escape(app)}-',
                state_addresses,
            )
        }
    else:
        selected = set() if explicit == "none" else {part.strip() for part in explicit.split(",") if part.strip()}
        unknown = selected - available
        if unknown:
            raise ValueError(f"Unknown domain app build manifests: {', '.join(sorted(unknown))}")
    if superplane_enabled:
        if explicit is not None and "superplane" not in selected:
            raise ValueError("Superplane deployment requires its build projects")
        selected.add("superplane")
    return sorted(selected)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-root", required=True, type=Path)
    parser.add_argument("--explicit", required=True)
    parser.add_argument("--explicit-set", required=True)
    parser.add_argument("--superplane-enabled", required=True)
    parser.add_argument("--superplane-only", required=True)
    parser.add_argument("--skip-superplane", required=True)
    args = parser.parse_args()
    explicit = args.explicit if args.explicit_set else None
    superplane = args.superplane_only == "true" or (args.superplane_enabled == "true" and args.skip_superplane != "true")
    try:
        print(json.dumps(resolve(args.manifest_root, sys.stdin.read(), explicit, superplane)))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
