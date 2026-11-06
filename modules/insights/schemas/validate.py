"""Validate version-selected lineage records; v1 remains the default."""

import argparse
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = ROOT / "lineage" / "v1"
FIXTURE_DIR = ROOT / "fixtures" / "v1"
RECORD_TYPES = ("story", "task", "commit_pr", "deployment", "test_run")
SCHEMA_TYPES = (*RECORD_TYPES, "quarantine")
V2_RECORD_TYPES = (
    "story_revision",
    "acceptance_criterion",
    "coverage_definition",
    "test_definition",
    "coverage_mapping",
    "test_run",
    "run_test",
    "test_result",
    "acceptance_assessment",
)


def schema_directory(version):
    if version not in {"v1", "v2"}:
        raise ValueError(f"unsupported lineage version: {version}")
    return SCHEMA_DIR if version == "v1" else ROOT / "lineage" / "v2"


def load_schema(record_type, version="v1"):
    if record_type not in (SCHEMA_TYPES if version == "v1" else V2_RECORD_TYPES):
        raise ValueError(f"unknown lineage record type: {record_type}")
    return json.loads(
        (schema_directory(version) / f"{record_type}.schema.json").read_text()
    )


def validate_record(record, record_type, version="v1"):
    directory = schema_directory(version)
    registry = Registry().with_resources(
        (path.name, Resource.from_contents(json.loads(path.read_text())))
        for path in directory.glob("*.schema.json")
    )
    schema = load_schema(record_type, version)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(
        schema, registry=registry, format_checker=FormatChecker()
    )
    errors = list(validator.iter_errors(record))
    if version == "v2" and not errors:
        from jsonschema import ValidationError

        def check_scope(value):
            if isinstance(value, dict):
                if "record_id" in value and any(
                    value.get(k) != record[k] for k in ("tenant_id", "project_id")
                ):
                    errors.append(
                        ValidationError(
                            "record reference must stay within the record's tenant and project"
                        )
                    )
                for child in value.values():
                    check_scope(child)
            elif isinstance(value, list):
                for child in value:
                    check_scope(child)

        check_scope(record)
        if record.get("started_at") and record.get("ended_at"):
            from datetime import datetime

            if datetime.fromisoformat(record["ended_at"]) < datetime.fromisoformat(
                record["started_at"]
            ):
                errors.append(ValidationError("ended_at must not precede started_at"))
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files", nargs="*", type=Path, help="JSON records (default: five v1 fixtures)"
    )
    parser.add_argument("--version", choices=("v1", "v2"), default="v1")
    parser.add_argument(
        "--schema",
        choices=sorted(set(SCHEMA_TYPES + V2_RECORD_TYPES)),
        help="record type for supplied files",
    )
    args = parser.parse_args(argv)
    if args.files and not args.schema:
        parser.error("--schema is required when supplying files")
    if not args.files and args.schema:
        parser.error("supply at least one file with --schema")

    fixture_directory = (
        FIXTURE_DIR if args.version == "v1" else ROOT / "fixtures" / "v2"
    )
    records = (
        ((args.schema, path) for path in args.files)
        if args.files
        else (
            (record_type, fixture_directory / f"{record_type}.json")
            for record_type in (
                RECORD_TYPES if args.version == "v1" else V2_RECORD_TYPES
            )
        )
    )
    failures = 0
    for record_type, path in records:
        try:
            errors = validate_record(
                json.loads(path.read_text()), record_type, args.version
            )
        except (OSError, ValueError) as error:
            print(f"{path}: {error}", file=sys.stderr)
            failures += 1
            continue
        for error in errors:
            print(
                f"{path}: {'.'.join(map(str, error.absolute_path)) or record_type}: {error.message}",
                file=sys.stderr,
            )
            failures += 1
        if not errors:
            print(f"{path}: valid")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
