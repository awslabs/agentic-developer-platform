"""Select implemented Tools packages without allowing their removal to skip CI."""

import argparse
import os
import re
import subprocess
from pathlib import Path


def packages(root: Path, base: str | None) -> dict[str, bool]:
    if base and not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("A full immutable base commit is required")
    result = {}
    for name in ("facade", "interceptor", "adapters"):
        relative = f"modules/tools/{name}"
        directory = root / relative
        existed = False
        if base:
            listing = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", base, "--", relative],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            existed = bool(listing.stdout.strip())
        if not directory.exists():
            if existed:
                raise ValueError(
                    f"Existing {name} package was removed; its checks cannot be skipped"
                )
            result[name] = False
            continue
        required = [directory / "pyproject.toml", directory / "tests"]
        if name in {"facade", "adapters"}:
            required.append(directory / "Dockerfile")
        if name == "adapters":
            required += [
                directory / "uv.lock",
                directory / "tool_adapters/server.py",
                directory / "scripts/image_smoke.py",
                directory / "scripts/emit_image_evidence.py",
            ]
        missing = [
            str(path.relative_to(root)) for path in required if not path.exists()
        ]
        if missing:
            raise ValueError("Incomplete Tools package: " + ", ".join(missing))
        result[name] = True
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="")
    args = parser.parse_args()
    selected = packages(Path.cwd(), args.base or None)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for name, present in selected.items():
            output.write(f"{name}={str(present).lower()}\n")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write("Tools package checks\n\n")
        for name, present in selected.items():
            summary.write(
                f"- {name}: {'lint and tests required' if present else 'not implemented yet; workflow contracts only'}\n"
            )
        if not selected["facade"]:
            summary.write(
                "\nFaçade image build and deployment acceptance remain pending the façade implementation.\n"
            )


if __name__ == "__main__":
    main()
