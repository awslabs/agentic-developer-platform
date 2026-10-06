"""Exercise installer writes against Kubernetes identity/version preconditions."""

import copy
import json
from types import SimpleNamespace

import pytest
import yaml

from installation.config import LABEL, Refusal
from installation.runner import Installer


class RacingKubernetes:
    def __init__(self, desired, race):
        self.current = copy.deepcopy(desired) if race != "create" else None
        if self.current:
            self.current["metadata"].update(uid="original", resourceVersion="1")
        self.race = race
        self.foreign = None

    def call(self, args, **kwargs):
        if "get-caller-identity" in args:
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "Account": "879318057152",
                        "Arn": "arn:aws:sts::879318057152:assumed-role/test-installer/fixture",
                        "UserId": "AROA" + "A" * 17 + ":fixture",
                    }
                )
            )
        if "get-role" in args:
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "Role": {
                            "Arn": "arn:aws:iam::879318057152:role/deployment/test-installer",
                            "RoleId": "AROA" + "A" * 17,
                        }
                    }
                )
            )
        if "get" in args:
            value = self.current
        else:
            desired = next(yaml.safe_load_all(kwargs["data"]))
            self.foreign = copy.deepcopy(desired)
            self.foreign["metadata"].update(
                uid="original" if self.race == "relabel" else "replacement",
                resourceVersion="2",
                labels={LABEL: "another-installation"},
            )
            self.current = copy.deepcopy(self.foreign)
            if "create" in args:
                raise Refusal("Kubernetes AlreadyExists")
            assert "apply" in args
            meta = desired["metadata"]
            if any(
                field in meta and meta[field] != self.current["metadata"][field]
                for field in ("uid", "resourceVersion")
            ):
                raise Refusal("Kubernetes identity/version conflict")
            self.current = desired
            self.current["metadata"].update(uid="replacement", resourceVersion="3")
            value = self.current
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(value) if value else "", stderr=""
        )


@pytest.mark.parametrize("race", ["replace", "relabel", "create"])
def test_apply_cannot_adopt_an_object_changed_after_ownership_read(
    tmp_path, environment, release, race
):
    installer = Installer(environment, release, tmp_path)
    desired = installer.docs[0]
    tools = RacingKubernetes(desired, race)
    installer.commands = tools

    with pytest.raises(Refusal):
        installer.apply([desired])

    assert tools.current == tools.foreign
    assert installer.receipt["objects"] == []
    assert "uid" not in desired["metadata"]
