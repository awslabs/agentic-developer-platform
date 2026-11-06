"""Produce a private Identity summary only after independent image/readback checks."""

import os
import tempfile
from pathlib import Path

from deploy import write_private
from foundation_release import FoundationCheck


def main():
    environment = {**os.environ, "ADP_QUALIFICATION_STAGE": "identity"}
    with tempfile.TemporaryDirectory(prefix="adp-identity-release-") as name:
        report = FoundationCheck(environment).qualify(Path(name))
    destination = Path(environment["ADP_TOOLS_ADAPTER_RELEASE_DIR"])
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(destination / "atlassian-stage-summary.json", report)


if __name__ == "__main__":
    main()
