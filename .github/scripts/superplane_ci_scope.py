"""Keep persona metadata PRs on the lightweight Superplane registration lane."""

import os
import re
import subprocess
from pathlib import Path

PERSONA_METADATA = frozenset(
    {
        "docs/agent-catalogue.md",
        "modules/agent-factory/webhook-ingress/lambda/common/personas.py",
        "modules/agent-factory/webhook-ingress/lambda/common/tests/test_persona_prompt_files.py",
        "modules/agent-factory/webhook-ingress/lambda/common/tests/test_persona_catalogue_parity.py",
    }
)


def persona_only(paths: list[str]) -> bool:
    # Empty/unknown changes must not suppress full coverage. Renames are supplied
    # as deletion + addition so moving a metadata file into runtime code runs full CI.
    return bool(paths) and set(paths) <= PERSONA_METADATA


def main() -> None:
    lightweight = False
    if os.environ["CI_EVENT"] == "pull_request":
        base, head = os.environ["PR_BASE_SHA"], os.environ["PR_HEAD_SHA"]
        if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
            raise ValueError("Expected full PR commit SHAs")
        changed = (
            subprocess.check_output(
                [
                    "git",
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "-z",
                    f"{base}...{head}",
                    "--",
                ]
            )
            .decode()
            .split("\0")
        )
        lightweight = persona_only([path for path in changed if path])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"persona_only={str(lightweight).lower()}\n")
    print(
        "Persona registration checks only" if lightweight else "Full Superplane checks"
    )


if __name__ == "__main__":
    main()
