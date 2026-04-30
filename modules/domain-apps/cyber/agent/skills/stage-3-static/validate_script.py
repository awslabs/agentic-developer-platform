#!/usr/bin/env python3
"""Validate a Mode B script against the worker manifest.

Checks that all imports and subprocess calls reference tools actually available
in the worker image. Exit 0 = safe to upload. Nonzero = violation found.

Usage:
    python3 validate_script.py <script.py> <worker-manifest.json>
"""

import ast
import json
import sys
from pathlib import Path

# Standard library modules that are always available (subset relevant to analysis scripts).
STDLIB_ALLOWLIST = frozenset(
    [
        "abc",
        "base64",
        "binascii",
        "collections",
        "contextlib",
        "copy",
        "dataclasses",
        "datetime",
        "enum",
        "fnmatch",
        "functools",
        "glob",
        "gzip",
        "hashlib",
        "io",
        "itertools",
        "json",
        "logging",
        "math",
        "operator",
        "os",
        "pathlib",
        "re",
        "shutil",
        "struct",
        "subprocess",
        "sys",
        "tempfile",
        "textwrap",
        "time",
        "typing",
        "uuid",
        "zipfile",
        "zlib",
    ]
)

# Common aliases: package distribution name -> importable module name
# e.g. "yara-python" installs as "yara", "python-magic" installs as "magic"
DIST_TO_IMPORT = {
    "yara-python": "yara",
    "python-magic": "magic",
    "pyelftools": "elftools",
}


def load_manifest(manifest_path: str) -> dict:
    """Load and return the worker manifest."""
    with open(manifest_path) as f:
        return json.load(f)


def get_allowed_imports(manifest: dict) -> set[str]:
    """Build the set of allowed top-level import names from the manifest."""
    allowed = set(STDLIB_ALLOWLIST)

    for dist_name in manifest.get("python_packages", {}):
        # Add the distribution name itself (works for most packages)
        allowed.add(dist_name.replace("-", "_"))
        # Add known import aliases
        if dist_name in DIST_TO_IMPORT:
            allowed.add(DIST_TO_IMPORT[dist_name])

    return allowed


def get_allowed_binaries(manifest: dict) -> set[str]:
    """Build the set of allowed binary names from the manifest."""
    return set(manifest.get("system_binaries", {}).keys())


def find_similar(name: str, allowed: set[str]) -> str | None:
    """Find a similar name in the allowed set (simple edit-distance-1 or prefix match)."""
    for candidate in sorted(allowed):
        if candidate.startswith(name[:3]) or name.startswith(candidate[:3]):
            return candidate
    return None


def validate_script(script_path: str, manifest: dict) -> list[str]:
    """Validate a script against the manifest. Returns list of violation messages."""
    source = Path(script_path).read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"SyntaxError: {e}"]

    allowed_imports = get_allowed_imports(manifest)
    allowed_binaries = get_allowed_binaries(manifest)
    violations = []

    for node in ast.walk(tree):
        # Check import statements
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_module = alias.name.split(".")[0]
                if top_module not in allowed_imports:
                    suggestion = find_similar(top_module, allowed_imports)
                    msg = f"line {node.lineno}: 'import {alias.name}' — not in worker image"
                    if suggestion:
                        msg += f" (did you mean '{suggestion}'?)"
                    violations.append(msg)

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_module = node.module.split(".")[0]
                if top_module not in allowed_imports:
                    suggestion = find_similar(top_module, allowed_imports)
                    msg = f"line {node.lineno}: 'from {node.module} import ...' — not in worker image"
                    if suggestion:
                        msg += f" (did you mean '{suggestion}'?)"
                    violations.append(msg)

        # Check subprocess calls with literal command strings
        elif isinstance(node, ast.Call):
            func = node.func
            # Match subprocess.run([...]) or subprocess.Popen([...])
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
                and func.attr in ("run", "Popen", "call", "check_output", "check_call")
            ):
                # Look at the first positional argument
                if node.args:
                    first_arg = node.args[0]
                    # Handle list literal: subprocess.run(["tool", ...])
                    if isinstance(first_arg, ast.List) and first_arg.elts:
                        first_elt = first_arg.elts[0]
                        if isinstance(first_elt, ast.Constant) and isinstance(
                            first_elt.value, str
                        ):
                            binary_name = Path(first_elt.value).name
                            if (
                                binary_name not in allowed_binaries
                                and binary_name != "python3"
                                and binary_name != "python"
                            ):
                                msg = (
                                    f"line {node.lineno}: subprocess call to "
                                    f"'{binary_name}' — not in worker image"
                                )
                                suggestion = find_similar(binary_name, allowed_binaries)
                                if suggestion:
                                    msg += f" (did you mean '{suggestion}'?)"
                                violations.append(msg)
                    # Handle string literal: subprocess.run("tool ...")
                    elif isinstance(first_arg, ast.Constant) and isinstance(
                        first_arg.value, str
                    ):
                        binary_name = first_arg.value.split()[0]
                        binary_name = Path(binary_name).name
                        if (
                            binary_name not in allowed_binaries
                            and binary_name != "python3"
                            and binary_name != "python"
                        ):
                            msg = (
                                f"line {node.lineno}: subprocess call to "
                                f"'{binary_name}' — not in worker image"
                            )
                            violations.append(msg)

    return violations


def main() -> int:
    if len(sys.argv) != 3:
        print(
            f"Usage: {sys.argv[0]} <script.py> <worker-manifest.json>", file=sys.stderr
        )
        return 2

    script_path = sys.argv[1]
    manifest_path = sys.argv[2]

    if not Path(script_path).exists():
        print(f"Error: script not found: {script_path}", file=sys.stderr)
        return 2

    if not Path(manifest_path).exists():
        print(f"Error: manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    manifest = load_manifest(manifest_path)
    violations = validate_script(script_path, manifest)

    if violations:
        print(f"FAILED — {len(violations)} violation(s) found:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    print("OK — all imports and subprocess calls are in the worker manifest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
