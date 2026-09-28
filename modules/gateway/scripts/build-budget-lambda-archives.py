#!/usr/bin/env python3
"""Build the same source manifests as budget-lambda's Terraform archives."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def manifest(module: Path, name: str) -> dict[str, Path]:
    shared = sorted((module / "lambda/shared").glob("*.py"))
    policy = module / "pricing_policy"
    policy_files = sorted(policy.rglob("*.py")) + sorted((policy / "snapshots").glob("*.json"))
    if not (shared and (policy / "__init__.py").is_file()):
        raise AssertionError()
    if not (list((policy / "snapshots").glob("*.json"))):
        raise AssertionError("Missing pricing snapshots")
    if not ((module / "lambda" / name / "handler.py").is_file()):
        raise AssertionError("Missing Lambda handler")
    entries = [(path, path.name) for path in sorted((module / "lambda" / name).glob("*.py"))]
    entries.extend((path, path.name) for path in shared)
    entries.extend((path, f"pricing_policy/{path.relative_to(policy)}") for path in policy_files)
    if not (len({entry for _, entry in entries}) == len(entries)):
        raise AssertionError("Duplicate archive entries")
    return {entry: path for path, entry in entries}


def build(module: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name in ("pricing-refresh", "budget-usage-tracker"):
        with ZipFile(output / f"{name}.zip", "w", ZIP_DEFLATED) as archive:
            for entry, path in sorted(manifest(module, name).items()):
                archive.write(path, entry)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(Path(__file__).resolve().parents[1], args.output)
