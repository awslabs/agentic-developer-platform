"""An independent reader release cannot alter another deployment or permissions."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from installation.config import Refusal, digest
from installation.dedicated_reader_runtime import KIND, NAME, reviewed, target


def test_saved_reader_plan_allows_only_image_change():
    before = {
        "kind": "Deployment",
        "metadata": {"name": NAME, "uid": "owned"},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "serviceAccountName": NAME,
                    "containers": [
                        {"name": NAME, "image": "repo@sha256:old", "env": []}
                    ],
                }
            },
        },
    }
    after = target(before, "repo@sha256:new")
    assert (
        before["spec"]["template"]["spec"]["containers"][0]["image"]
        == "repo@sha256:old"
    )
    value = {
        "kind": KIND,
        "installation_id": "owner",
        "release_id": "release",
        "before": before,
        "after": after,
        "image": "repo@sha256:new",
    }
    installer = SimpleNamespace(owner="owner", release="release")
    reviewed(installer, value, digest(value))
    drifted = deepcopy(value)
    drifted["after"]["spec"]["template"]["spec"]["serviceAccountName"] = "administrator"
    with pytest.raises(Refusal):
        reviewed(installer, drifted, digest(drifted))
    with pytest.raises(Refusal):
        reviewed(installer, value, "wrong-digest")
    foreign = deepcopy(before)
    foreign["metadata"]["name"] = "superplane-api"
    with pytest.raises(Refusal):
        target(foreign, "repo@sha256:new")
