"""The owner needs a genuine denial, complete history and explicit AWS absence."""

import copy
from datetime import datetime, timedelta, timezone
import pytest
from botocore.exceptions import ClientError
from installation.bootstrap_denial import verify_events, verify_absence


@pytest.fixture
def denial():
    now = datetime.now(timezone.utc)
    identity = {
        "operation_id": "operation-original",
        "org_id": "org",
        "workspace_id": "workspace",
        "cluster_arn": "arn:aws:eks:us-west-2:123456789012:cluster/fixture",
        "generation": "a" * 64,
        "first_grant": {
            "principal_arn": "arn:aws:iam::123456789012:role/registrar",
            "groups": ["temporary"],
            "username": "temporary:{{SessionName}}",
            "client_token": "b" * 64,
        },
    }
    authority = {
        "account_id": "123456789012",
        "region": "us-west-2",
        "role_arn": "arn:aws:iam::123456789012:role/provider",
    }
    event = {
        "eventTime": now.isoformat(),
        "eventSource": "eks.amazonaws.com",
        "eventName": "CreateAccessEntry",
        "readOnly": False,
        "errorCode": "AccessDenied",
        "eventID": "denied-event",
        "requestID": "request",
        "recipientAccountId": authority["account_id"],
        "awsRegion": authority["region"],
        "userIdentity": {
            "arn": "arn:aws:sts::123456789012:assumed-role/provider/adp-superplane-operation-operatio",
            "sessionContext": {"sessionIssuer": {"arn": authority["role_arn"]}},
        },
        "requestParameters": {
            "name": "fixture",
            "principalArn": identity["first_grant"]["principal_arn"],
            "type": "STANDARD",
            "kubernetesGroups": ["temporary"],
            "username": "temporary:{{SessionName}}",
            "clientRequestToken": "b" * 64,
            "tags": {
                "superplane-generation": "a" * 64,
                "OrgId": "org",
                "WorkspaceId": "workspace",
            },
        },
        "responseElements": {"message": "denied"},
    }
    return identity, authority, event, now


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "duplicate",
        "success",
        "ambiguous",
        "other-mutation",
        "token",
        "tags",
        "cluster",
        "principal",
        "region",
        "account",
        "issuer",
        "session",
        "response",
        "late",
    ],
)
def test_exact_denial_and_no_other_mutation_required(denial, fault):
    identity, authority, event, now = denial
    events = [event]
    if fault == "missing":
        events = []
    elif fault == "duplicate":
        events.append(copy.deepcopy(event))
    elif fault == "success":
        event.pop("errorCode")
    elif fault == "ambiguous":
        event["errorCode"] = "InternalFailure"
    elif fault == "other-mutation":
        events.append({**event, "eventName": "AssociateAccessPolicy"})
    elif fault in {"token", "tags", "cluster", "principal"}:
        event["requestParameters"][
            {
                "token": "clientRequestToken",
                "tags": "tags",
                "cluster": "name",
                "principal": "principalArn",
            }[fault]
        ] = "different"
    elif fault in {"region", "account"}:
        event[{"region": "awsRegion", "account": "recipientAccountId"}[fault]] = (
            "different"
        )
    elif fault == "issuer":
        event["userIdentity"]["sessionContext"]["sessionIssuer"]["arn"] = "different"
    elif fault == "session":
        event["userIdentity"]["arn"] = "different"
    elif fault == "response":
        event["responseElements"]["accessEntry"] = {"arn": "created"}
    elif fault == "late":
        event["eventTime"] = (now + timedelta(hours=1)).isoformat()
    if fault:
        with pytest.raises(ValueError):
            verify_events(
                events,
                identity,
                authority,
                acquired_at=now - timedelta(minutes=1),
                stopped_at=now + timedelta(minutes=1),
            )
    else:
        assert (
            verify_events(
                events,
                identity,
                authority,
                acquired_at=now - timedelta(minutes=1),
                stopped_at=now + timedelta(minutes=1),
            )
            == event
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "exists",
        "denied",
        "timeout",
        "group",
        "incomplete-list",
        "incomplete-entry",
        "actor-missing",
    ],
)
def test_absence_requires_every_actor_not_found_and_complete_group_inventory(fault):
    specs = [
        {
            "kind": "eks-entry",
            "actor": actor,
            "principal_arn": actor,
            "groups": ["temporary-" + actor],
        }
        for actor in ("registrar", "installer", "supervisor")
    ]
    if fault == "actor-missing":
        specs.pop()

    class Eks:
        def describe_access_entry(self, *, clusterName, principalArn):
            assert clusterName == "fixture"
            if principalArn != "node":
                if fault == "exists":
                    return {"accessEntry": {}}
                if fault == "timeout":
                    raise TimeoutError()
                raise ClientError(
                    {
                        "Error": {
                            "Code": "AccessDenied"
                            if fault == "denied"
                            else "ResourceNotFoundException"
                        }
                    },
                    "DescribeAccessEntry",
                )
            if fault == "incomplete-entry":
                return {}
            return {
                "accessEntry": {
                    "principalArn": "node",
                    "kubernetesGroups": ["temporary-registrar"]
                    if fault == "group"
                    else [],
                }
            }

        def list_access_entries(self, **kwargs):
            return {} if fault == "incomplete-list" else {"accessEntries": ["node"]}

    if fault:
        with pytest.raises(Exception):
            verify_absence(Eks(), {"grants": specs}, cluster_name="fixture")
    else:
        assert verify_absence(Eks(), {"grants": specs}, cluster_name="fixture") == [
            {"principalArn": "node", "kubernetesGroups": []}
        ]
