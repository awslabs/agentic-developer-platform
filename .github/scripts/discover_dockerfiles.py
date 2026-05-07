#!/usr/bin/env python3
"""Discover Dockerfiles in the repo and emit a JSON matrix for GitHub Actions."""

import json
import os


def discover_dockerfiles(repo_root: str = ".") -> list[str]:
    """Find all Dockerfiles, excluding node_modules and hidden directories."""
    exclude_dirs = {"node_modules", ".git", ".github"}
    dockerfiles = []

    for root, dirs, files in os.walk(repo_root):
        # Prune excluded directories
        dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith(".")]

        if "Dockerfile" in files:
            path = os.path.join(root, "Dockerfile")
            # Normalize to relative path with ./
            rel = os.path.relpath(path, repo_root)
            dockerfiles.append(f"./{rel}")

    return sorted(dockerfiles)


def main() -> None:
    repo_root = os.environ.get("GITHUB_WORKSPACE", ".")
    dockerfiles = discover_dockerfiles(repo_root)

    if not dockerfiles:
        # Emit empty matrix — jobs will be skipped
        print(f"matrix={json.dumps([])}")
        return

    print(f"matrix={json.dumps(dockerfiles)}")


if __name__ == "__main__":
    main()
