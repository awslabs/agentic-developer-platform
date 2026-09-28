import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from region_version_policy import SUPPORTED_REGIONS, createable_versions


def test_every_supported_region_and_minor_has_a_matching_immutable_image_release():
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "node-image-pins.json").read_text()
    )
    assert policy["ami_type"] == "AL2023_x86_64_STANDARD"
    assert set(policy["releases"]) == set(SUPPORTED_REGIONS)
    for releases in policy["releases"].values():
        assert set(releases) == set(createable_versions())
        for minor, release in releases.items():
            assert re.fullmatch(re.escape(minor) + r"\.[0-9]+-[0-9]{8}", release)
