"""Check v2 schema fields against their versioned lineage design sections."""

import json
import sys

from check_traceability import ROOT, SOURCE_DOCUMENT, schema_properties, source_fields
from validate import V2_RECORD_TYPES

SCHEMA_DIR = ROOT / "lineage" / "v2"
SOURCES = {"envelope": ("11.0", None), "references": ("11.1", None)}
SOURCES.update(
    {name: (f"11.{index}", None) for index, name in enumerate(V2_RECORD_TYPES, start=2)}
)


def main():
    try:
        document = SOURCE_DOCUMENT.read_text()
        schemas = {
            path.name.removesuffix(".schema.json")
            for path in SCHEMA_DIR.glob("*.schema.json")
        }
        failures = 0
        for name in sorted(schemas | SOURCES.keys()):
            if name not in SOURCES:
                print(f"{name}: no designated v2 source section", file=sys.stderr)
                failures += 1
                continue
            schema = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())
            fields = source_fields(document, name, sources=SOURCES)
            if name == "envelope":
                fields.update(source_fields(document, "envelope"))
            elif name != "references":
                # Only the discriminator is repeated in the concrete record schemas.
                fields["record_type"] = source_fields(
                    document, "envelope", sources=SOURCES
                )["record_type"]
            for field, path in schema_properties(schema):
                if field not in fields:
                    print(
                        f"{name}{path}: no source field in section {SOURCES[name][0]}",
                        file=sys.stderr,
                    )
                    failures += 1
                else:
                    print(f"{name}{path} -> {SOURCE_DOCUMENT}:{fields[field]}")
        return 1 if failures else 0
    except (OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
