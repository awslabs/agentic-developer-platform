"""Historical image recovery cannot adopt objects or force unrelated conflicts."""

import copy
import json
from types import SimpleNamespace

import pytest
import yaml

from installation.config import LABEL, Refusal, digest
from installation.deployment_recovery import KEY, validate
from installation.runner import Installer


@pytest.fixture
def recovery(tmp_path, environment, release):
    installer = Installer(environment, release, tmp_path)
    desired = next(d for d in installer.docs if d["kind"] == "Deployment")
    old = copy.deepcopy(desired)
    old["metadata"].update(
        uid="01234567-89ab-cdef-0123-456789abcdef", resourceVersion="10"
    )
    container = old["spec"]["template"]["spec"]["containers"][0]
    container["image"] = "example.test/previous@sha256:" + "a" * 64
    old["metadata"]["managedFields"] = [
        {
            "manager": "kubectl-patch",
            "operation": "Update",
            "fieldsV1": {
                "f:spec": {
                    "f:template": {
                        "f:spec": {
                            "f:containers": {
                                'k:{"name":"' + container["name"] + '"}': {
                                    "f:image": {}
                                }
                            }
                        }
                    }
                }
            },
        }
    ]
    entry = {
        "name": desired["metadata"]["name"],
        "namespace": desired["metadata"]["namespace"],
        "uid": old["metadata"]["uid"],
        "spec_sha256": digest(old["spec"]),
        "images": {container["name"]: container["image"]},
    }
    installer.env[KEY] = [entry]
    installer.receipt.update(
        stage="rollout",
        completed=["migration", "bootstrap"],
        remote_lock={"etag": "retained"},
    )
    installer.verify_deployment_identity = lambda: None
    return installer, desired, old


class Server:
    def __init__(self, current, race=None):
        self.current = copy.deepcopy(current)
        self.race = race
        self.calls = []
        self.writes = 0

    def call(self, args, **kwargs):
        self.calls.append(args)
        if "get" in args:
            result = copy.deepcopy(self.current)
            if "--show-managed-fields" in args and self.race == "read":
                result["metadata"]["resourceVersion"] = "11"
        else:
            desired = yaml.safe_load(kwargs["data"])
            assert "--server-side" in args
            dry = "--dry-run=server" in args
            if dry:
                assert "--force-conflicts" not in args
                old = self.current["spec"]["template"]["spec"]["containers"][0]
                assert (
                    desired["spec"]["template"]["spec"]["containers"][0]["image"]
                    == old["image"]
                )
                if self.race == "other-conflict":
                    raise Refusal("Non-image field conflicts")
                if self.race == "write":
                    self.current["metadata"]["resourceVersion"] = "11"
                    self.current["metadata"]["labels"][LABEL] = "foreign"
                result = desired
            else:
                assert desired["metadata"]["uid"] == self.current["metadata"]["uid"]
                if (
                    desired["metadata"]["resourceVersion"]
                    != self.current["metadata"]["resourceVersion"]
                ):
                    raise Refusal("Resource version conflict")
                self.writes += 1
                self.current = desired
                result = desired
        return SimpleNamespace(stdout=json.dumps(result), returncode=0, stderr="")


def test_fenced_recovery_applies_after_non_image_conflicts_are_excluded(recovery):
    installer, desired, old = recovery
    validate(installer.env)
    server = Server(old)
    installer.commands = server
    installer.apply([desired])
    assert server.writes == 1
    assert "--force-conflicts" in server.calls[-1]
    assert server.current["spec"] == desired["spec"]
    assert installer.receipt[KEY][0]["resource_version"] == "10"
    assert installer.receipt["objects"][0]["uid"] == old["metadata"]["uid"]
    assert "resourceVersion" not in desired["metadata"]
    installer.apply([desired])
    assert "--force-conflicts" not in server.calls[-1]


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "uid",
        "spec",
        "image",
        "manager",
        "co-owner",
        "migration",
        "bootstrap",
        "lock",
        "stage",
    ],
)
def test_recovery_refuses_unreviewed_state_before_writes(recovery, change):
    installer, desired, old = recovery
    if change == "owner":
        old["metadata"]["labels"][LABEL] = "foreign"
    elif change == "uid":
        old["metadata"]["uid"] = "replacement"
    elif change == "spec":
        old["spec"]["replicas"] += 1
    elif change == "image":
        old["spec"]["template"]["spec"]["containers"][0]["image"] += "changed"
    elif change == "manager":
        old["metadata"]["managedFields"][0]["manager"] = "other"
    elif change == "co-owner":
        old["metadata"]["managedFields"].append(
            copy.deepcopy(old["metadata"]["managedFields"][0])
        )
    elif change in {"migration", "bootstrap"}:
        installer.receipt["completed"].remove(change)
    elif change == "lock":
        installer.receipt.pop("remote_lock")
    else:
        installer.receipt["stage"] = "foundations"
    server = Server(old)
    installer.commands = server
    with pytest.raises(Refusal):
        installer.apply([desired])
    assert server.writes == 0
    assert not any("--force-conflicts" in call for call in server.calls)


@pytest.mark.parametrize("race", ["read", "write", "other-conflict"])
def test_recovery_fences_races_and_refuses_unrelated_conflicts(recovery, race):
    installer, desired, old = recovery
    server = Server(old, race)
    installer.commands = server
    with pytest.raises(Refusal):
        installer.apply([desired])
    assert server.writes == 0
    assert installer.receipt["objects"] == []


@pytest.mark.parametrize(
    "change",
    ["namespace", "name", "duplicate", "extra", "tag", "container", "empty", "digest"],
)
def test_recovery_input_is_exact_and_bounded(recovery, change):
    installer, _, _ = recovery
    entry = installer.env[KEY][0]
    if change in {"namespace", "name"}:
        entry[change] = "foreign"
    elif change == "duplicate":
        installer.env[KEY].append(copy.deepcopy(entry))
    elif change == "extra":
        entry["force_all"] = True
    elif change == "tag":
        entry["images"] = {"superplane-api": "example.test/image:latest"}
    elif change == "container":
        entry["images"]["other"] = next(iter(entry["images"].values()))
    elif change == "empty":
        installer.env[KEY] = []
    else:
        entry["spec_sha256"] = "bad"
    with pytest.raises(Refusal):
        validate(installer.env)
