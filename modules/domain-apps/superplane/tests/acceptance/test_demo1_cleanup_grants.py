"""Synthetic broker/provider responses never contact AWS or create live receipts."""

import copy
import json
import subprocess
from datetime import timedelta

import pytest
import test_demo1_aws as aws_fixtures

from superplane_acceptance.demo1_cleanup_grants import observe_grants
from superplane_acceptance.demo1_evidence import EvidenceError
from workspace_provisioning.artifacts import digest

reader = aws_fixtures.reader
selection = aws_fixtures.selection


def grant_documents(selected):
    cluster = f"arn:aws:eks:{selected.region}:{selected.account}:cluster/example-owned"
    principal = f"arn:aws:iam::{selected.account}:role/ExampleInstaller"
    spec = {
        "key": "cleaner-entry",
        "kind": "eks-entry",
        "cluster_arn": cluster,
        "principal_arn": principal,
        "generation": "a" * 64,
        "groups": ["example-cleanup"],
        "username": "example-cleaner:{{SessionName}}",
    }
    identity = {
        "arn": cluster.replace(":cluster/", ":access-entry/")
        + "/role/example/ExampleInstaller/unique-id",
        "generation": spec["generation"],
        "groups": spec["groups"],
        "username": spec["username"],
    }
    return [{"spec": spec, "identity": identity}]


class GrantCli:
    def __init__(self, selected):
        self.selected = selected
        self.grants = grant_documents(selected)
        self.calls = []
        self.change = lambda operation, value: value

    def __call__(self, command, **options):
        assert command[:8] == [
            "adp-cred",
            "assume",
            "--service",
            "aws",
            "--label",
            "example-connection",
            "--exec",
            "aws",
        ]
        assert 0 < options["timeout"] <= 30
        service, operation = command[8:10]
        if service == "sts":
            assert operation == "get-caller-identity"
            result = {
                "Account": self.selected.account,
                "Arn": f"arn:aws:sts::{self.selected.account}:assumed-role/{self.selected.role}/example-session",
            }
        else:
            assert service == "eks"
            assert self.calls[-1][8:10] == ["sts", "get-caller-identity"]
            spec, identity = self.grants[0]["spec"], self.grants[0]["identity"]
            assert command[10:] == [
                "--cluster-name",
                "example-owned",
                "--principal-arn",
                spec["principal_arn"],
                "--region",
                self.selected.region,
                "--no-paginate",
                "--output",
                "json",
            ]
            if operation == "describe-access-entry":
                result = {
                    "accessEntry": {
                        "clusterName": "example-owned",
                        "principalArn": spec["principal_arn"],
                        "accessEntryArn": identity["arn"],
                        "type": "STANDARD",
                        "kubernetesGroups": identity["groups"],
                        "username": identity["username"],
                        "tags": {"superplane-generation": identity["generation"]},
                    }
                }
            else:
                assert operation == "list-associated-access-policies"
                result = {
                    "clusterName": "example-owned",
                    "principalArn": spec["principal_arn"],
                    "associatedAccessPolicies": [],
                }
        self.calls.append(command)
        result = self.change(operation, copy.deepcopy(result))
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")


def check(selected, executor):
    return observe_grants(
        reader(selected, executor), selected, executor.grants, digest(executor.grants)
    )


def test_observes_only_original_entry_and_empty_policy_set(selection):
    executor = GrantCli(selection)
    observed = check(selection, executor)
    assert observed["status"] == "OBSERVED"
    assert observed["associated_policy_count"] == 0
    assert [command[9] for command in executor.calls] == [
        "get-caller-identity",
        "describe-access-entry",
        "get-caller-identity",
        "list-associated-access-policies",
        "get-caller-identity",
        "describe-access-entry",
    ]
    assert selection.account not in json.dumps(observed)
    assert executor.grants[0]["identity"]["arn"] not in json.dumps(observed)


@pytest.mark.parametrize(
    "case",
    [
        "wrong-role",
        "wrong-account",
        "replacement",
        "generation",
        "groups",
        "username",
        "foreign-principal",
        "foreign-cluster",
        "entry-type",
        "missing-entry",
        "policies",
        "incomplete-policies",
        "missing-policies",
        "foreign-policy-principal",
        "foreign-policy-cluster",
        "replacement-after-policies",
    ],
)
def test_refuses_changed_or_incomplete_current_authority(selection, case):
    executor = GrantCli(selection)

    def changed(operation, value):
        if operation == "get-caller-identity":
            if case == "wrong-role":
                value["Arn"] = value["Arn"].replace(selection.role, "ForeignObserver")
            elif case == "wrong-account":
                value["Account"] = "000000000000"
        elif operation == "describe-access-entry":
            entry = value["accessEntry"]
            if case == "missing-entry":
                return {}
            if case == "replacement" or (
                case == "replacement-after-policies" and len(executor.calls) == 6
            ):
                entry["accessEntryArn"] += "-replacement"
            elif case == "generation":
                entry["tags"]["superplane-generation"] = "f" * 64
            elif case == "groups":
                entry["kubernetesGroups"].append("system:masters")
            elif case == "username":
                entry["username"] = "foreign-cleaner"
            elif case == "foreign-principal":
                entry["principalArn"] += "-foreign"
            elif case == "foreign-cluster":
                entry["clusterName"] = "foreign-cluster"
            elif case == "entry-type":
                entry["type"] = "EC2_LINUX"
        else:
            if case == "policies":
                value["associatedAccessPolicies"] = [{"policyArn": "foreign-policy"}]
            elif case == "incomplete-policies":
                value["nextToken"] = "more-results"
            elif case == "missing-policies":
                del value["associatedAccessPolicies"]
            elif case == "foreign-policy-principal":
                value["principalArn"] += "-foreign"
            elif case == "foreign-policy-cluster":
                value["clusterName"] = "foreign-cluster"
        return value

    executor.change = changed
    with pytest.raises(EvidenceError, match="cleanup grants"):
        check(selection, executor)
    if case in ("wrong-role", "wrong-account"):
        assert len(executor.calls) == 1


@pytest.mark.parametrize("failure", ["denied", "absent", "malformed", "timeout"])
def test_provider_failure_never_establishes_grant_authority(selection, failure):
    executor = GrantCli(selection)

    def failed(command, **options):
        if command[8] == "sts":
            return executor(command, **options)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1, output="private-provider-data")
        return subprocess.CompletedProcess(
            command,
            0 if failure == "malformed" else 254,
            "private-provider-data",
            "ResourceNotFoundException"
            if failure == "absent"
            else "AccessDeniedException",
        )

    with pytest.raises(EvidenceError, match="cleanup grants") as error:
        observe_grants(
            reader(selection, failed),
            selection,
            executor.grants,
            digest(executor.grants),
        )
    assert "private-provider-data" not in str(error.value)


@pytest.mark.parametrize(
    "case",
    [
        "digest",
        "target",
        "extra-grant",
        "foreign-cluster",
        "foreign-principal",
        "expired",
    ],
)
def test_scope_or_artifact_mismatch_refuses_before_provider_reads(selection, case):
    executor = GrantCli(selection)
    provider = reader(selection, executor)
    expected = digest(executor.grants)
    if case == "digest":
        expected = "f" * 64
    elif case == "target":
        provider.role_name = "ForeignObserver"
    elif case == "expired":
        provider.clock = lambda: selection.deadline + timedelta(seconds=1)
    else:
        if case == "extra-grant":
            executor.grants *= 2
        else:
            field = "cluster_arn" if case == "foreign-cluster" else "principal_arn"
            executor.grants[0]["spec"][field] = executor.grants[0]["spec"][
                field
            ].replace(selection.account, "000000000000")
        expected = digest(executor.grants)
    with pytest.raises(EvidenceError, match="cleanup grants"):
        observe_grants(provider, selection, executor.grants, expected)
    assert executor.calls == []
