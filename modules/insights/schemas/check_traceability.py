"""Check every lineage v1 schema property against its designated design section."""

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = ROOT / "lineage" / "v1"
SOURCE_DOCUMENT = ROOT.parents[2] / "docs/engineering-insights/lineage-schema.md"
SCHEMA_SOURCES = {
    "envelope": ("3.0", None),
    "story": ("3.3", None),
    "task": ("3.4", None),
    "commit_pr": ("3.6", None),
    "deployment": ("3.7", None),
    "test_run": ("3.8", "Test Run"),
    "quarantine": ("6.1", None),
}


def source_fields(document, record_type, *, sources=None):
    section, entity = (sources or SCHEMA_SOURCES)[record_type]
    current_section = None
    current_entity = None
    fields = {}
    for line_number, line in enumerate(document.splitlines(), start=1):
        if line.startswith("#"):
            heading = re.match(r"^#+ (\d+(?:\.\d+)*)\s", line)
            current_section = heading.group(1) if heading else None
            current_entity = None
        if current_section != section:
            continue
        if record_type == "quarantine":
            if "Quarantine missing required evidence with a reason." in line:
                fields["reason"] = line_number
            continue
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < (3 if entity else 2):
            continue
        if entity:
            current_entity = cells[0] or current_entity
            if current_entity != entity:
                continue
        row_fields = re.findall(r"`([a-z][a-z0-9_]*)`", cells[1 if entity else 0])
        if record_type == "deployment" and "incident_id" in row_fields:
            continue
        fields.update((field, line_number) for field in row_fields)
    return fields


def schema_properties(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}/{key}"
            if key == "properties":
                for field in value:
                    yield field, f"{child_path}/{field}"
            yield from schema_properties(value, child_path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from schema_properties(value, f"{path}/{index}")


def main():
    try:
        document = SOURCE_DOCUMENT.read_text()
        schemas = {
            path.name.removesuffix(".schema.json")
            for path in SCHEMA_DIR.glob("*.schema.json")
        }
        failures = 0
        for record_type in sorted(schemas | SCHEMA_SOURCES.keys()):
            if record_type not in SCHEMA_SOURCES:
                print(f"{record_type}: no designated source section", file=sys.stderr)
                failures += 1
                continue
            schema = json.loads((SCHEMA_DIR / f"{record_type}.schema.json").read_text())
            fields = source_fields(document, record_type)
            section = SCHEMA_SOURCES[record_type][0]
            for field, path in schema_properties(schema):
                if field not in fields:
                    print(
                        f"{record_type}{path}: no source field in section {section}",
                        file=sys.stderr,
                    )
                    failures += 1
                else:
                    print(
                        f"{record_type}{path} -> {SOURCE_DOCUMENT}:{fields[field]} (section {section})"
                    )
        return 1 if failures else 0
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
