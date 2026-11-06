"""Fresh retry evidence refuses any surviving disposable cleanup principal."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from installation.bootstrap_revoked import acquisition_contract, verify_cleanup_absence


@pytest.mark.parametrize(
    "fault", [None, "role-present", "entry-present", "denied", "timeout"]
)
def test_cleanup_identity_must_be_positively_absent(fault):
    iam, eks = Mock(), Mock()
    iam.get_role.side_effect = ClientError(
        {"Error": {"Code": "NoSuchEntity"}}, "GetRole"
    )
    eks.describe_access_entry.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeAccessEntry"
    )
    if fault == "role-present":
        iam.get_role.side_effect = None
    elif fault == "entry-present":
        eks.describe_access_entry.side_effect = None
    elif fault == "denied":
        eks.describe_access_entry.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "DescribeAccessEntry"
        )
    elif fault == "timeout":
        eks.describe_access_entry.side_effect = TimeoutError()
    session = SimpleNamespace(
        client=lambda service, **_: iam if service == "iam" else eks
    )
    row = {
        "generation": "a" * 64,
        "cluster_arn": "arn:aws:eks:us-west-2:123456789012:cluster/original",
    }
    if fault:
        with pytest.raises((ValueError, TimeoutError)):
            verify_cleanup_absence(session, row)
    else:
        verify_cleanup_absence(session, row)
        iam.get_role.assert_called_once_with(
            RoleName="sp-acquisition-cleanup-" + "a" * 24
        )
        eks.describe_access_entry.assert_called_once_with(
            clusterName="original",
            principalArn="arn:aws:iam::123456789012:role/sp-acquisition-cleanup-"
            + "a" * 24,
        )


@pytest.mark.parametrize("length", [14, 17, 18, 24, 25])
@pytest.mark.parametrize("qualified_cleanup", [False, True])
def test_retirement_grants_require_the_additional_source_qualification(
    length, qualified_cleanup
):
    source = {"acquisition_prefix": True, "hashes": {}}
    if qualified_cleanup:
        source.update(cleanup_acquisition_prefix=True)
        source["hashes"][
            "workspace_bootstrap/superplane_bootstrap/retirement_fence.py"
        ] = "a" * 64
    identity = {"acquisition_prefix": [{}] * length}
    validate = acquisition_contract().validate_source_prefix
    if length > 17 and not qualified_cleanup:
        with pytest.raises(ValueError):
            validate(source, identity)
    else:
        validate(source, identity)


@pytest.mark.parametrize(
    "fault,additional_attempts",
    [(None, 1), (None, 10)]
    + [
        (fault, 10)
        for fault in (
            "actor",
            "count",
            "limit",
            "over-allowance",
            "zero",
            "bool",
            "reextension",
            "blank",
            "partial",
            "retained-only",
            "changed-expiry",
            "changed-ceiling",
            "cleanup-present",
        )
    ],
)
def test_owner_allowance_requires_fresh_full_cleanup_and_actual_authorization(
    monkeypatch, fault, additional_attempts
):
    import copy
    from datetime import datetime, timedelta, timezone

    from installation import bootstrap_revoked as owner

    contract = acquisition_contract()
    generation = "a" * 64
    descriptor = dict(
        operation_id="op",
        org_id="org",
        workspace_id="ws",
        source_principal="worker#1",
        source_key="original",
        source_fence=20,
    )
    extension = dict(
        authorized_by="human",
        authorization="Authorize up to ten additional attempts after cleanup",
        previous_limit=10,
        recorded_attempts=10,
        additional_attempts=additional_attempts,
    )
    descriptor["attempt_extension"] = extension
    namespace = {
        "key": "workspace-namespace",
        "kind": "kubernetes",
        "actor": "registrar",
        "lifetime": "resource",
        "generation": generation,
        "cluster_arn": "original-cluster",
        "body": {"kind": "Namespace", "apiVersion": "v1"},
    }
    prefix = [
        {"key": key, "generation": generation}
        for key in (*contract.PREFIX, *contract.CLEANUP_PREFIX)
    ]
    prefix[2] = namespace
    journal = {k: descriptor[k] for k in ("operation_id", "org_id", "workspace_id")}
    journal.update(
        generation=generation,
        claim="b" * 64,
        plan_sha256="c" * 64,
        cluster_arn="original-cluster",
        revoked_progress_sha256="d" * 64,
        acquisition_prefix=prefix,
        retained_namespace={
            "grant": namespace,
            "progress": {"phase": "adopted", "identity": {"uid": "retained"}},
        },
    )
    snapshot = {
        "revoked_bootstrap": {
            "journals": [journal],
            "current_generation": generation,
            "predecessor": None,
        },
        "journals": [
            {
                "generation": generation,
                "revoked": True,
                "plan_json": "{}",
                "cluster_arn": "original-cluster",
            }
        ],
    }
    expiry = {
        **descriptor,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "earliest_owner_evidence_at": (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat(),
        "attempts": 10,
        "max_attempts": 10,
    }
    evidence = {
        **{
            k: descriptor[k]
            for k in ("operation_id", "org_id", "workspace_id", "source_fence")
        },
        "source_image": "qualified@sha256:77695ebc2e5d141181908fa44020b88b0bdb6e1ff44f64cc0b23f1ee00fe6890",
    }
    monkeypatch.setattr(
        owner, "collect", lambda *args, **kwargs: copy.deepcopy(evidence)
    )
    monkeypatch.setattr(owner, "verify_absence", lambda *args, **kwargs: None)

    def cleanup_absence(*args):
        if fault == "cleanup-present":
            raise ValueError("cleanup principal still present")

    monkeypatch.setattr(owner, "verify_cleanup_absence", cleanup_absence)
    if fault == "actor":
        extension["authorized_by"] = "other"
    elif fault == "count":
        expiry["attempts"] = 9
    elif fault == "limit":
        expiry["max_attempts"] = 11
    elif fault == "over-allowance":
        extension["additional_attempts"] = 11
    elif fault == "zero":
        extension["additional_attempts"] = 0
    elif fault == "bool":
        extension["additional_attempts"] = True
    elif fault == "reextension":
        extension["previous_limit"] = extension["recorded_attempts"] = 11
        extension["additional_attempts"] = 1
        expiry["attempts"] = expiry["max_attempts"] = 11
    elif fault == "blank":
        extension["authorization"] = " "
    elif fault == "partial":
        prefix.pop()
    elif fault == "retained-only":
        snapshot["revoked_bootstrap"]["current_generation"] = None
    reads = 0

    def read_expiry():
        nonlocal reads
        reads += 1
        value = copy.deepcopy(expiry)
        if fault == "changed-expiry" and reads > 1:
            value["attempts"] = 11
        if fault == "changed-ceiling" and reads > 1:
            value["max_attempts"] = 20
        return value

    def collect():
        return owner.collect_revoked(
            {"authority": {"subject": "human", "region": "us-west-2"}},
            {},
            descriptor,
            SimpleNamespace(client=lambda *args, **kwargs: object()),
            acquisition=True,
            read_snapshot=lambda: copy.deepcopy(snapshot),
            read_expiry=read_expiry,
        )

    if fault:
        with pytest.raises(ValueError):
            collect()
    else:
        result = collect()
        assert result["attempt_extension"] == extension
        assert result["revoked_bootstrap"] == snapshot["revoked_bootstrap"]
        assert reads == 2
