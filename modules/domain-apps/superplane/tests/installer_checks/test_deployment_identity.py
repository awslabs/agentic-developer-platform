"""Selected-role checks prevent same-account fallback and stale resume authority."""

import copy
import json
from types import SimpleNamespace

import pytest

from installation.config import Refusal, validate
from installation.runner import Installer

from .test_complete_command import ExternalTools


class IdentityTools(ExternalTools):
    def __init__(self, environment, release):
        super().__init__(environment, release)
        self.caller_override = {}
        self.role_override = {}
        self.denied = None

    def call(self, args, **kwargs):
        if self.denied in args:
            raise Refusal("identity unavailable")
        result = super().call(args, **kwargs)
        if "get-caller-identity" in args:
            value = json.loads(result.stdout) | self.caller_override
        elif "get-role" in args:
            value = json.loads(result.stdout)
            value["Role"].update(self.role_override)
        else:
            return result
        return SimpleNamespace(stdout=json.dumps(value), returncode=0, stderr="")


@pytest.fixture
def selected(tmp_path, environment, release):
    tools = IdentityTools(environment, release)
    installer = Installer(environment, release, tmp_path, tools)
    return installer, tools


@pytest.mark.parametrize(
    "caller,role",
    [
        ({"Account": "111111111111"}, {}),
        ({"Arn": "arn:aws:sts::879318057152:assumed-role/ambient-worker/fixture"}, {}),
        ({"Arn": "arn:aws:iam::879318057152:user/operator"}, {}),
        ({"UserId": "AROA" + "B" * 17 + ":fixture"}, {}),
        ({}, {"RoleId": "AROA" + "B" * 17}),
        ({}, {"Arn": "arn:aws:iam::879318057152:role/replaced-path/test-installer"}),
    ],
)
def test_no_phase_action_under_wrong_or_recreated_role(selected, caller, role):
    installer, tools = selected
    tools.caller_override, tools.role_override = caller, role
    actions = []
    with pytest.raises(Refusal):
        installer.phase("infrastructure", lambda: actions.append("mutation"))
    assert actions == []
    assert installer.receipt["status"] == "failed"


@pytest.mark.parametrize("denied", ["get-caller-identity", "get-role"])
def test_unavailable_identity_stops_before_mutation(selected, denied):
    installer, tools = selected
    tools.denied = denied
    with pytest.raises(Refusal, match="unavailable"):
        installer.aws("s3api", "put-object")
    assert not any("put-object" in args for args, _ in tools.calls)


def test_refresh_is_revalidated_and_never_uses_previous_success(selected):
    installer, tools = selected
    actions = []
    installer.phase("first", lambda: actions.append("first"))
    tools.caller_override = {"UserId": "AROA" + "B" * 17 + ":fixture"}
    with pytest.raises(Refusal, match="role identity"):
        installer.phase("after-refresh", lambda: actions.append("second"))
    assert actions == ["first"]
    assert sum("get-caller-identity" in args for args, _ in tools.calls) == 2


def test_resumed_receipt_cannot_supply_live_authority(selected, tmp_path):
    installer, tools = selected
    installer.phase("first", lambda: None)
    installer.receipt["status"] = "failed"
    previous = copy.deepcopy(installer.receipt)
    resumed = Installer(installer.env, installer.lock, tmp_path, tools)
    resumed.resume(previous)
    tools.role_override = {"RoleId": "AROA" + "B" * 17}
    with pytest.raises(Refusal, match="replaced"):
        resumed.phase(
            "rollout", lambda: pytest.fail("stale receipt authorized mutation")
        )


@pytest.mark.parametrize(
    "transport,args",
    [
        ("aws", ("s3api", "delete-object")),
        ("kube", ("delete", "namespace", "superplane")),
        ("kube", ("exec", "pod", "--", "mutating-command")),
    ],
)
def test_compensation_and_recovery_commands_recheck_identity(selected, transport, args):
    installer, tools = selected
    installer.verify_deployment_identity()
    tools.caller_override = {"Account": "111111111111"}
    before = len(tools.calls)
    with pytest.raises(Refusal, match="selected account"):
        getattr(installer, transport)(*args)
    assert len(tools.calls) == before + 1


def test_legacy_offline_plan_remains_available_but_live_work_requires_metadata(
    selected,
):
    installer, tools = selected
    installer.env.pop("deployment_identity")
    validate(installer.env, installer.lock)
    installer.plan()
    assert tools.calls == []
    with pytest.raises(Refusal, match="requires deployment_identity"):
        installer.phase("migration", lambda: pytest.fail("missing role accepted"))
    assert tools.calls == []


def test_evidence_contains_only_selected_metadata_and_observed_identity(selected):
    installer, _tools = selected
    installer.phase("rollout", lambda: None)
    evidence = installer.receipt["deployment_identity_verification"]
    assert evidence["connection_label"] == "selected-test-connection"
    assert evidence["expected_role_arn"].endswith("/deployment/test-installer")
    assert evidence["expected_role_id"] == "AROA" + "A" * 17
    assert evidence["stage"] == "rollout"
    assert "credential" not in json.dumps(evidence).lower()


@pytest.mark.parametrize(
    "field,value",
    [
        ("service", "gcp"),
        ("connection_label", ""),
        ("expected_role_arn", "arn:aws:iam::111111111111:role/test-installer"),
        ("expected_role_id", "test-installer"),
        ("unexpected", "value"),
    ],
)
def test_bad_identity_metadata_is_refused_offline(environment, release, field, value):
    environment["deployment_identity"][field] = value
    with pytest.raises(Refusal, match="deployment_identity"):
        validate(environment, release)


def test_refresh_to_same_role_with_new_session_is_accepted(selected):
    installer, tools = selected
    installer.phase("first", lambda: None)
    tools.caller_override = {
        "Arn": "arn:aws:sts::879318057152:assumed-role/test-installer/refreshed-session",
        "UserId": "AROA" + "A" * 17 + ":refreshed-session",
    }
    installer.phase("after-refresh", lambda: None)
    assert installer.receipt["completed"] == ["first", "after-refresh"]
    assert installer.receipt["deployment_identity_verification"][
        "assumed_role_arn"
    ].endswith("/refreshed-session")
