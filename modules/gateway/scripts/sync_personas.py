#!/usr/bin/env python3
"""Deterministic persona staging — sync _personas.py from the authoritative source.

Usage:
    python3 scripts/sync_personas.py          # Write the staged copy
    python3 scripts/sync_personas.py --check  # Exit 0 if current, 1 if stale

Reads the authoritative persona registry at
``modules/agent-factory/webhook-ingress/lambda/common/personas.py`` and writes
the staged copy at ``src/admin/persona_models/_personas.py``.

The ``--check`` mode compares the current staged copy against what the script
would generate.  It exits 0 if identical, 1 with a summary if stale.  Wire
this into CI/build to enforce that the staged copy stays in sync.

The parity test (``tests/admin/persona_models/test_persona_parity.py``) catches
any drift between the staged copy and the authoritative source, so running this
script is the deterministic way to update the staged copy after a source change.

This script replaces hand-editing _personas.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path


def _repo_root() -> Path:
    """Resolve the repo root.  This script lives at modules/gateway/scripts/."""
    return Path(__file__).resolve().parents[3]


def _load_source() -> object:
    """Load the authoritative personas module without polluting sys.modules."""
    source_path = _repo_root() / "modules" / "agent-factory" / "webhook-ingress" / "lambda" / "common" / "personas.py"
    if not source_path.exists():
        print(f"ERROR: Authoritative source not found at {source_path}", file=sys.stderr)
        sys.exit(1)

    spec = importlib.util.spec_from_file_location("authoritative_personas", source_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_harness_revisions() -> dict[str, str]:
    """Read exact SDK revisions from the owning runtime package manifests."""
    root = _repo_root()
    manifests = {
        "claude-agent-sdk": (
            root / "modules" / "agent-factory" / "agent" / "package.json",
            "@anthropic-ai/claude-agent-sdk",
        ),
        "codex-sdk": (
            root / "modules" / "agent-factory" / "codex-reviewer" / "package.json",
            "@openai/codex-sdk",
        ),
    }
    revisions: dict[str, str] = {}
    for compatibility_class, (manifest_path, dependency) in manifests.items():
        manifest = json.loads(manifest_path.read_text())
        revision = manifest.get("dependencies", {}).get(dependency)
        if not isinstance(revision, str) or not revision or revision[0] in "^~<>=*":
            raise ValueError(f"{manifest_path} must pin {dependency} to an exact revision; got {revision!r}")
        revisions[compatibility_class] = revision
    return revisions


def _format_dict(name: str, d: dict[str, str], type_hint: str) -> str:
    """Format a dict constant as Python source."""
    lines = [f"{name}: {type_hint} = {{"]
    for k, v in sorted(d.items()):
        lines.append(f'    "{k}": "{v}",')
    lines.append("}")
    return "\n".join(lines)


def _format_set(name: str, values: set[str], type_hint: str) -> str:
    """Format a set constant as deterministic Python source."""
    lines = [f"{name}: {type_hint} = {{"]
    for value in sorted(values):
        lines.append(f'    "{value}",')
    lines.append("}")
    return "\n".join(lines)


def _generate_output(module: object) -> tuple[str, int]:
    """Generate the staged copy content and return (content, persona_count)."""
    label_to_persona = dict(module.LABEL_TO_PERSONA)
    mention_to_persona = dict(module.MENTION_TO_PERSONA)
    automatic_personas = set(getattr(module, "AUTOMATIC_PERSONAS", set()))
    valid_personas = set(module.VALID_PERSONAS)
    persona_compatibility_class = dict(module.PERSONA_COMPATIBILITY_CLASS)
    task_classes = dict(getattr(module, "TASK_PERSONA_COMPATIBILITY_CLASS", {}))
    if any(not key.startswith("agent-task-") for key in task_classes) or set(task_classes) & valid_personas:
        raise ValueError("Task-only personas must not enter legacy dispatch")
    harness_revisions = _load_harness_revisions()

    # The staged copy recomputes VALID_PERSONAS from the three emitted mappings, so if
    # the source's own VALID_PERSONAS is not exactly that union the generated copy is
    # wrong by construction.  This must fail rather than warn: when AUTOMATIC_PERSONAS
    # was introduced (a persona category selected by platform events rather than by a
    # label or @-mention) the generator did not emit it, silently dropped a persona from
    # the staged copy, and exited 0 -- surfacing only as a confusing red parity test in
    # an unrelated PR's CI.  Failing here reports the drift at the point it is
    # introduced, consistent with the PERSONA_COMPATIBILITY_CLASS/harness checks below.
    derived = set(label_to_persona.values()) | set(mention_to_persona.values()) | automatic_personas
    if derived != valid_personas:
        raise ValueError(
            "Source VALID_PERSONAS is not the union of LABEL_TO_PERSONA, MENTION_TO_PERSONA "
            "and AUTOMATIC_PERSONAS, so the staged copy cannot reproduce it. If a new persona "
            "category was added to personas.py, teach this generator to emit it.\n"
            f"  Derived:     {sorted(derived)}\n"
            f"  Source only: {sorted(valid_personas - derived)}\n"
            f"  Derived only: {sorted(derived - valid_personas)}"
        )
    if set(persona_compatibility_class) != valid_personas:
        raise ValueError("PERSONA_COMPATIBILITY_CLASS keys must exactly match VALID_PERSONAS")
    missing_revisions = (set(persona_compatibility_class.values()) | set(task_classes.values())) - set(harness_revisions)
    if missing_revisions:
        raise ValueError(f"No exact harness revision is registered for compatibility classes: {sorted(missing_revisions)}")

    output = textwrap.dedent('''\
        """Staged copy of the authoritative persona registry — Issue #5420 (PMM-03).

        THIS FILE IS GENERATED by ``scripts/sync_personas.py``.
        DO NOT edit manually.

        Source: ``modules/agent-factory/webhook-ingress/lambda/common/personas.py``

        The parity test asserts this copy matches the source, so drift is a red
        test rather than a silent divergence.
        """

        from __future__ import annotations

    ''')

    output += _format_dict("LABEL_TO_PERSONA", label_to_persona, "dict[str, str]")
    output += "\n\n"
    output += _format_dict("MENTION_TO_PERSONA", mention_to_persona, "dict[str, str]")
    output += "\n\n"
    output += _format_set("AUTOMATIC_PERSONAS", automatic_personas, "set[str]")
    output += "\n\n"
    output += _format_dict(
        "PERSONA_COMPATIBILITY_CLASS",
        persona_compatibility_class,
        "dict[str, str]",
    )
    output += "\n\n"
    output += _format_dict("TASK_PERSONA_COMPATIBILITY_CLASS", task_classes, "dict[str, str]")
    output += "\n\n"
    output += _format_dict(
        "COMPATIBILITY_CLASS_HARNESS_CONTRACT_REVISION",
        harness_revisions,
        "dict[str, str]",
    )
    output += "\n\n"
    output += "# The canonical set of all valid personas — union of all mapping targets and automatic personas.\n"
    output += "VALID_PERSONAS: set[str] = set(MENTION_TO_PERSONA.values()) | set(LABEL_TO_PERSONA.values()) | AUTOMATIC_PERSONAS\n"

    return output, len(valid_personas)


def _target_path() -> Path:
    """Path to the staged copy."""
    return Path(__file__).resolve().parents[1] / "src" / "admin" / "persona_models" / "_personas.py"


def main() -> None:
    check_mode = "--check" in sys.argv

    module = _load_source()
    expected, persona_count = _generate_output(module)
    target = _target_path()

    if check_mode:
        if not target.exists():
            print(f"STALE: {target} does not exist. Run scripts/sync_personas.py to generate it.", file=sys.stderr)
            sys.exit(1)
        current = target.read_text()
        if current == expected:
            print(f"OK: {target} is up to date ({persona_count} personas)")
            sys.exit(0)
        else:
            print(f"STALE: {target} differs from authoritative source.", file=sys.stderr)
            print("  Run: python3 scripts/sync_personas.py", file=sys.stderr)
            sys.exit(1)

    target.write_text(expected)
    print(f"Wrote {target} ({persona_count} personas)")


if __name__ == "__main__":
    main()
