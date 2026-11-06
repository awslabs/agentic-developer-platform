# ruff: noqa: F811 -- shared fixture injection.
"""Source qualification is read-only and refuses changed owner, image or policy."""

import copy
from datetime import datetime, timedelta, timezone
import json

import pytest

from installation import bootstrap_recovery as owner
from workspace_provisioning.tests.test_lifecycle_policy import policy
from .test_provider_authority import setup as setup


@pytest.mark.parametrize(
    "source_image",
    [
        "sha256:45349b74b297f367863ce368932b31d9a35c90ba728b8316db6a4bac94e9a8b0",
        "sha256:f0870380df7e74dabcd61e704b85e69cbabe6e2e821114c262d73c7498ebc2d4",
        "sha256:dbbf2eb4b51c61e387e6e4d13a2b141625ade8fa265d1d293fa6fcf0b968f5f1",
    ],
)
@pytest.mark.parametrize("history_mode", [False, "stable", "drift", "refused"])
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "worker-active",
        "dispatch-active",
        "source-image",
        "source-hash",
        "source-uid",
        "policy-mutable",
        "owner",
    ],
)
def test_bootstrap_source_collector_requires_exact_provenance_without_writes(
    setup, monkeypatch, fault, source_image, history_mode
):
    setup.run(check_only=False)
    before = setup.state.writes
    boundary = owner.source_contract()
    image, qualified = source_image, boundary.QUALIFIED_SOURCES[source_image]
    doc = setup.doc
    authority = doc["authority"]
    original = {
        "status": "installed-and-verified",
        "installation_id": authority["installation_id"],
        "environment": {
            "namespace": "superplane-demo",
            "cluster": doc["management_cluster"],
            "account_id": authority["account_id"],
        },
    }
    descriptor = {k: authority[k] for k in ("org_id", "workspace_id")}
    descriptor.update(
        operation_id="original",
        plan_digest="a" * 64,
        source_key="original-key",
        source_fence=1,
        source_principal="invocation#1",
        source_pod_name="worker",
        source_pod_uid="uid",
        workload_binding="uid",
        source_acquired_at=(
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat(),
    )
    selected = policy()
    selected["runtime"]["management_public_access"] = {
        "vpc_id": "vpc-0123456789abcdef0",
        "nat_gateway_ids": ["nat-0123456789abcdef0"],
    }
    selected["runtime"]["workspace_variables"].update(
        cluster_endpoint_public_access=True,
        cluster_endpoint_public_access_cidrs=["52.22.137.37/32"],
    )
    worker = {
        "image": "registry/worker@" + image,
        "env": [
            {
                "name": "SUPERPLANE_LIFECYCLE_POLICY_FILE",
                "value": "/run/policy/lifecycle.json",
            }
        ],
        "volumeMounts": [
            {"name": "policy", "mountPath": "/run/policy", "readOnly": True}
        ],
    }
    pod = {
        "metadata": {"creationTimestamp": "2026-01-02T00:00:00Z"},
        "spec": {
            "containers": [worker],
            "volumes": [{"name": "policy", "configMap": {"name": "policy"}}],
        },
    }
    objects = {
        "scaledjob": {
            "metadata": {"annotations": {"autoscaling.keda.sh/paused": "true"}}
        },
        "deployment": {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "superplane-api",
                                "env": [
                                    {
                                        "name": "SUPERPLANE_OPERATION_DISPATCH_ENABLED",
                                        "value": "false",
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        },
        "pods": {"items": []},
        "configmap": {
            "immutable": True,
            "metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"},
            "data": {
                "lifecycle.json": json.dumps(
                    {"version": 1, "tenants": {authority["org_id"]: selected}}
                )
            },
        },
    }
    hashes = copy.deepcopy(qualified["hashes"])
    if fault == "worker-active":
        objects["pods"]["items"] = [{"status": {"phase": "Running"}}]
    elif fault == "dispatch-active":
        objects["deployment"]["spec"]["template"]["spec"]["containers"][0]["env"][0][
            "value"
        ] = "true"
    elif fault == "source-image":
        worker["image"] = "other@sha256:" + "0" * 64
    elif fault == "source-hash":
        hashes["workspace_provisioning/runtime.py"] = "0" * 64
    elif fault == "source-uid":
        descriptor["source_pod_uid"] = "replaced"
    elif fault == "policy-mutable":
        objects["configmap"]["immutable"] = False
    elif fault == "owner":
        setup.state.selected_role_id = "another-role"
    monkeypatch.setattr(owner, "installed_config", lambda *args: setup.config)
    source_calls = []

    def read_source(*args, **kwargs):
        source_calls.append(kwargs)
        assert kwargs["historical_bootstrap"] is bool(history_mode)
        if history_mode:
            assert reads == 1
        return (
            pod,
            {"creation_event_id": "create", "terminal_event_ids": ["done"]},
        )

    monkeypatch.setattr(owner, "source_pod", read_source)
    options = {}
    original_before = copy.deepcopy(original)
    if history_mode:
        from types import SimpleNamespace
        from installation import bootstrap_history

        installer = SimpleNamespace(
            env=original.pop("environment"), receipt={}, save=lambda: None
        )
        reads = 0

        def current_context(*args, **kwargs):
            nonlocal reads
            reads += 1
            assert args[2] is original and "environment" not in original
            if history_mode == "refused":
                raise ValueError("historical context is not verified")
            return {
                "current": "changed"
                if history_mode == "drift" and reads > 1
                else "checked"
            }

        monkeypatch.setattr(bootstrap_history, "qualify", current_context)
        options = {
            "historical_archives": {"exact-archives": True},
            "installer": installer,
        }

    def run():
        return owner.collect(
            doc,
            original,
            descriptor,
            setup.session,
            read_kube=lambda *args: json.dumps(objects[args[3]]),
            read_sources=lambda *args: hashes,
            **options,
        )

    if fault or history_mode in {"drift", "refused"}:
        with pytest.raises(ValueError):
            run()
    else:
        evidence = run()
        assert evidence["source_image"] == worker["image"]
        assert evidence["evidence_kind"] == "bootstrap-journal-barrier-v1"
        assert "bootstrap_mutations_started" not in evidence
        if history_mode:
            assert reads == 2 and installer.receipt["bootstrap_historical_context"] == {
                "current": "checked"
            }
    if history_mode:
        assert "environment" not in original
        assert {**original, "environment": installer.env} == original_before
    if history_mode == "refused":
        assert source_calls == []
    assert setup.state.writes == before


@pytest.mark.parametrize(
    "fault", [None, "live", "guard", "source", "changed", "absence", "unrevoked"]
)
def test_revoked_owner_collection_requires_fresh_expiry_and_positive_absence(
    monkeypatch, fault
):
    from installation import bootstrap_revoked as revoked
    from types import SimpleNamespace

    now = datetime.now(timezone.utc)
    descriptor = {
        "operation_id": "operation",
        "org_id": "org",
        "workspace_id": "workspace",
        "source_principal": "worker#1",
        "source_key": "key",
        "source_fence": 12,
    }
    expiry = {
        **descriptor,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "earliest_owner_evidence_at": (now - timedelta(minutes=1)).isoformat(),
    }
    journals = [
        {
            "generation": "a" * 64,
            "revoked": True,
            "plan_json": '{"grants":[]}',
            "cluster_arn": "arn:aws:eks:us-west-2:123456789012:cluster/workspace",
        }
    ]
    snapshot = {
        "journals": journals,
        "revoked_bootstrap": {"journals": [{"generation": "a" * 64}]},
    }
    evidence = {**descriptor, "source_image": "qualified-image"}
    monkeypatch.setattr(revoked, "collect", lambda *a, **k: copy.deepcopy(evidence))
    monkeypatch.setattr(
        revoked,
        "source_contract",
        lambda: SimpleNamespace(
            qualified_source=lambda _: {"registrar_prefix": fault != "source"}
        ),
    )
    monkeypatch.setattr(
        revoked,
        "revoked_contract",
        lambda: SimpleNamespace(
            KIND="bootstrap-pre-namespace-revoked-v1", validate_revoked=lambda _: None
        ),
    )
    calls = []

    def absent(*a, **k):
        calls.append(k)
        if fault == "absence":
            raise ValueError("EKS AccessDenied is not absence")

    monkeypatch.setattr(revoked, "verify_absence", absent)
    if fault == "live":
        expiry["issued_sessions_expired"] = False
    elif fault == "guard":
        expiry["earliest_owner_evidence_at"] = (now + timedelta(minutes=1)).isoformat()
    elif fault == "unrevoked":
        journals[0]["revoked"] = False
    reads = 0

    def read_snapshot():
        nonlocal reads
        reads += 1
        value = copy.deepcopy(snapshot)
        if reads > 1 and fault == "changed":
            value["journals"][0]["generation"] = "b" * 64
        return value

    def run():
        return revoked.collect_revoked(
            {"authority": {"region": "us-west-2"}},
            {},
            descriptor,
            SimpleNamespace(client=lambda *a, **k: object()),
            read_snapshot=read_snapshot,
            read_expiry=lambda: expiry,
        )

    if fault:
        with pytest.raises(ValueError):
            run()
    else:
        result = run()
        assert result["revoked_bootstrap"] == snapshot["revoked_bootstrap"]
        assert len(calls) == 1
