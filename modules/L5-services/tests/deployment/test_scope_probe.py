"""SCN-03 synthetic refusals; live IAM and provider calls are separate evidence."""

import copy
import json
from datetime import UTC, datetime

import pytest
from l5_services.installed_release_probe import ReleaseProofError
from l5_services.scope_probe import (
    CALL_SOURCES,
    NETWORK_ACTIONS,
    QUEUE_ACTIONS,
    SOURCES,
    SURFACES,
    ScopeFixture,
    sha256_document,
    verify_effective_scope,
)

ACCOUNT = "000000000000"
QUEUE = f"arn:aws:sqs:us-east-1:{ACCOUNT}:adp-dev-l5-work"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/reviewed-deployment"
GATEWAY = f"arn:aws:iam::{ACCOUNT}:role/adp-dev-gateway"
NOW = datetime(2026, 10, 26, 12, 2, tzinfo=UTC)


def policies():
    roles = {}
    for label, actions in QUEUE_ACTIONS.items():
        name = f"adp-dev-l5-{label}"
        queue_statement = {
            "Effect": "Allow",
            "Action": sorted(actions),
            "Resource": QUEUE,
        }
        statements = [queue_statement]
        role = {
            "role_arn": GATEWAY
            if label == "gateway"
            else f"arn:aws:iam::{ACCOUNT}:role/{name}"
        }
        if label != "gateway":
            function_arn = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{name}"
            statements = [
                {
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/{name}:*",
                },
                queue_statement,
                {"Effect": "Allow", "Action": list(NETWORK_ACTIONS), "Resource": "*"},
                {
                    "Effect": "Deny",
                    "Action": list(NETWORK_ACTIONS),
                    "Resource": "*",
                    "Condition": {
                        "ArnEquals": {"lambda:SourceFunctionArn": function_arn}
                    },
                },
            ]
            role.update(
                function_arn=function_arn,
                managed_policies=[],
                boundary={
                    "arn": f"arn:aws:iam::{ACCOUNT}:policy/reviewed-{label}",
                    "version_id": "v1",
                    "document": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": sorted(actions),
                                "Resource": QUEUE,
                            },
                            {
                                "Effect": "Allow",
                                "Action": list(NETWORK_ACTIONS),
                                "Resource": "*",
                            },
                        ],
                    },
                },
            )
        role["policy"] = {"Version": "2012-10-17", "Statement": statements}
        roles[label] = role
    return roles


def fixture_data():
    roles = policies()
    return {
        "account_id": ACCOUNT,
        "role_arn": ROLE,
        "gateway_role_arn": GATEWAY,
        "queue_arn": QUEUE,
        "allowed_tenant": "tenant-owned",
        "foreign_tenant": "tenant-foreign",
        "allowed_product": "product-owned",
        "foreign_product": "product-foreign",
        "allowed_grant": "grant-owned",
        "foreign_grant": "grant-denied",
        "probe_id": "probe-owned",
        "principals": {
            "api": "subject-owned",
            "source": ROLE,
            "artifact": ROLE,
            "provider": ROLE,
            "queue": f"arn:aws:iam::{ACCOUNT}:role/adp-dev-l5-worker",
        },
        "targets": {
            surface: {
                scope: {
                    "operation": "sqs:ReceiveMessage"
                    if surface == "queue"
                    else "ReadFixture",
                    "resource": (
                        QUEUE
                        if scope == "allowed"
                        else f"arn:aws:sqs:us-east-1:{ACCOUNT}:foreign-work"
                    )
                    if surface == "queue"
                    else f"fixture/{surface}/{scope}",
                }
                for scope in ("allowed", "foreign")
            }
            for surface in SURFACES
        },
        "policy_sha256": {
            label: sha256_document(role["policy"]) for label, role in roles.items()
        },
        "boundary_sha256": {
            label: sha256_document(roles[label]["boundary"]["document"])
            for label in ("worker", "repair")
        },
        "owned_resources": ["resource-owned"],
        "deadline_seconds": 120,
        "max_attempts": 2,
        "max_spend_usd": "0.25",
    }


def fixture(tmp_path, data=None):
    path = tmp_path / "private-scope-fixture.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "effective_scope": fixture_data() if data is None else data,
            }
        )
    )
    return ScopeFixture.from_manifest(path)


def observations():
    spec = fixture_data()
    found = {
        name: {
            "source": source,
            "evidence_ref": f"private/{name}",
            "observed_at": "2026-10-26T12:01:00+00:00",
        }
        for name, source in SOURCES.items()
    }
    found["identity"].update(account_id=ACCOUNT, role_arn=ROLE)
    found["grants"].update(
        allowed={
            "tenant": "tenant-owned",
            "product": "product-owned",
            "grant": "grant-owned",
            "current": True,
        },
        foreign={
            "tenant": "tenant-foreign",
            "product": "product-foreign",
            "grant": "grant-denied",
            "current": False,
        },
    )
    found["policies"]["roles"] = policies()
    found["calls"]["results"] = [
        {
            "surface": surface,
            "source": CALL_SOURCES[surface],
            "scope": scope,
            "principal": spec["principals"][surface],
            "tenant": spec[f"{scope}_tenant"],
            "product": spec[f"{scope}_product"],
            **spec["targets"][surface][scope],
            "result": "allowed" if scope == "allowed" else "denied",
            "disclosed_resources": []
            if scope == "foreign"
            else [spec["targets"][surface]["allowed"]["resource"]],
            "http_status": 200
            if scope == "allowed"
            else 403
            if surface == "api"
            else None,
            "error_code": "AccessDenied"
            if scope == "foreign" and surface != "api"
            else None,
            "observed_at": "2026-10-26T12:00:45+00:00",
            "evidence_ref": f"private/{surface}/{scope}",
        }
        for surface in SURFACES
        for scope in ("allowed", "foreign")
    ]
    found["provider_absence"].update(
        probe_id="probe-owned",
        tenant="tenant-foreign",
        product="product-foreign",
        native_assets=[],
    )
    found["database_absence"].update(
        probe_id="probe-owned",
        tenant="tenant-foreign",
        product="product-foreign",
        database_rows=[],
    )
    found["budget"].update(
        started_at="2026-10-26T12:00:00+00:00",
        spent_usd="0.05",
        attempts=[
            {
                "observed_at": "2026-10-26T12:00:30+00:00",
                "evidence_ref": "private/attempt-one",
            }
        ],
    )
    found["cleanup"].update(
        observed_at="2026-10-26T12:01:20+00:00",
        resources=[{"id": "resource-owned", "state": "absent"}],
    )
    return found


def verify(tmp_path, observed, data=None):
    verify_effective_scope(fixture(tmp_path, data), observed, now=NOW)


def test_independent_scope_and_cleanup_observations_are_bounded(tmp_path):
    verify(tmp_path, observations())
    found = observations()
    found["cleanup"]["resources"] = [
        {
            "id": "resource-owned",
            "state": "retained",
            "reason": "reviewed drain pending",
        }
    ]
    verify(tmp_path, found)


@pytest.mark.parametrize(
    ("part", "field", "value", "reason"),
    [
        ("identity", "role_arn", "wrong-role", "scope_target_changed"),
        ("grants", "foreign", {"current": True}, "scope_grants_changed"),
        (
            "provider_absence",
            "native_assets",
            ["unexpected-native-effect"],
            "scope_foreign_effect_found",
        ),
        (
            "database_absence",
            "database_rows",
            ["unexpected-db-effect"],
            "scope_foreign_effect_found",
        ),
        (
            "provider_absence",
            "probe_id",
            "different-probe",
            "scope_foreign_effect_found",
        ),
        ("budget", "spent_usd", "0.26", "scope_spend_exceeded"),
        ("budget", "attempts", [], "scope_attempts_invalid"),
        ("cleanup", "resources", [], "scope_cleanup_incomplete"),
        (
            "cleanup",
            "resources",
            [{"id": "resource-owned", "state": "retained"}],
            "scope_cleanup_unverified",
        ),
    ],
)
def test_unattributed_denial_or_cleanup_cannot_pass(
    tmp_path, part, field, value, reason
):
    found = observations()
    found[part][field] = value
    with pytest.raises(ReleaseProofError, match=f"^{reason}$"):
        verify(tmp_path, found)


@pytest.mark.parametrize(
    ("surface", "field", "value", "reason"),
    [
        ("api", "http_status", 200, "scope_foreign_disclosed"),
        (
            "source",
            "disclosed_resources",
            ["foreign-source"],
            "scope_foreign_disclosed",
        ),
        ("source", "error_code", None, "scope_foreign_permitted"),
        ("artifact", "result", "allowed", "scope_foreign_permitted"),
        ("provider", "product", "product-owned", "scope_call_product_changed"),
        ("source", "source", "self-attested", "scope_call_target_changed"),
        ("queue", "principal", "foreign-principal", "scope_call_target_changed"),
        (
            "provider",
            "resource",
            "fixture/provider/allowed",
            "scope_call_target_changed",
        ),
    ],
)
def test_wrong_product_or_native_denial_mismatch_is_refused(
    tmp_path, surface, field, value, reason
):
    found = observations()
    call = next(
        item
        for item in found["calls"]["results"]
        if item["surface"] == surface and item["scope"] == "foreign"
    )
    call[field] = value
    with pytest.raises(ReleaseProofError, match=f"^{reason}$"):
        verify(tmp_path, found)


def test_replayed_or_old_readbacks_are_not_effective_proof(tmp_path):
    found = observations()
    found["calls"]["results"][0]["evidence_ref"] = found["identity"]["evidence_ref"]
    with pytest.raises(ReleaseProofError, match="scope_call_reference_invalid"):
        verify(tmp_path, found)
    found = observations()
    found["policies"]["observed_at"] = "2026-10-26T12:00:00+00:00"
    with pytest.raises(ReleaseProofError, match="scope_deadline_exceeded"):
        verify(tmp_path, found)
    found = observations()
    found["budget"]["started_at"] = "2026-10-26T11:55:00+00:00"
    with pytest.raises(ReleaseProofError, match="scope_deadline_exceeded"):
        verify(tmp_path, found)


@pytest.mark.parametrize("label", ("gateway", "worker", "repair"))
def test_permissive_policy_is_rejected_even_if_fixture_re_pins_its_digest(
    tmp_path, label
):
    found = observations()
    data = fixture_data()
    policy = found["policies"]["roles"][label]["policy"]
    policy["Statement"].append(
        {"Effect": "Allow", "Action": ["sqs:*"], "Resource": "*"}
    )
    data["policy_sha256"][label] = sha256_document(policy)
    with pytest.raises(ReleaseProofError, match="scope_policy_expanded"):
        verify(tmp_path, found, data)


def test_boundary_expansion_is_rejected_even_if_fixture_re_pins_its_digest(tmp_path):
    found = observations()
    data = fixture_data()
    boundary = found["policies"]["roles"]["worker"]["boundary"]["document"]
    boundary["Statement"].append(
        {"Effect": "Allow", "Action": ["sqs:*"], "Resource": QUEUE}
    )
    data["boundary_sha256"]["worker"] = sha256_document(boundary)
    with pytest.raises(ReleaseProofError, match="scope_boundary_expanded"):
        verify(tmp_path, found, data)


def test_scoped_extra_boundary_grant_is_rejected_even_when_re_pinned(tmp_path):
    found = observations()
    data = fixture_data()
    boundary = found["policies"]["roles"]["worker"]["boundary"]["document"]
    boundary["Statement"].append(
        {
            "Effect": "Allow",
            "Action": ["s3:DeleteBucket"],
            "Resource": "arn:aws:s3:::other-bucket",
        }
    )
    data["boundary_sha256"]["worker"] = sha256_document(boundary)
    with pytest.raises(ReleaseProofError, match="scope_boundary_expanded"):
        verify(tmp_path, found, data)


def test_duplicate_attempt_reference_is_not_independent_evidence(tmp_path):
    found = observations()
    found["budget"]["attempts"].append(copy.deepcopy(found["budget"]["attempts"][0]))
    with pytest.raises(ReleaseProofError, match="scope_attempt_reference_invalid"):
        verify(tmp_path, found)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("allowed_product", "product-foreign", "scope_fixture_grants_invalid"),
        (
            "queue_arn",
            "arn:aws:sqs:us-east-1:111111111111:foreign",
            "scope_fixture_queue_invalid",
        ),
        ("policy_sha256", {"worker": "latest"}, "scope_fixture_policy_invalid"),
        ("deadline_seconds", 3600, "scope_fixture_limits_invalid"),
        ("max_spend_usd", "0.00", "scope_fixture_limits_invalid"),
    ],
)
def test_unbounded_or_unpinned_scope_fixture_is_rejected(
    tmp_path, field, value, reason
):
    data = copy.deepcopy(fixture_data())
    data[field] = value
    with pytest.raises(ReleaseProofError, match=f"^{reason}$"):
        fixture(tmp_path, data)
