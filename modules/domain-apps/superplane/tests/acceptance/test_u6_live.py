"""U6 live acceptance. Run explicitly with the inputs documented in README.md."""

import json
import os
from pathlib import Path

import pytest

from superplane_acceptance.cli_delivery import GitHub, settings, verify


@pytest.mark.superplane_live
def test_cli_only_merge_reaches_served_artifact():
    config = settings(dict(os.environ))
    evidence = Path(config["evidence_file"])
    assert not evidence.exists(), (
        "Choose a new evidence path; do not overwrite a previous acceptance result"
    )
    report = verify(config, GitHub())
    assert report["evidence_kind"] == "live", (
        "Fixture transports cannot create live acceptance evidence"
    )
    with evidence.open("x") as target:
        target.write(json.dumps(report, indent=2) + "\n")
