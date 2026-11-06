"""Synthetic receipts test the contract adapter, never real registration readiness."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gitlab_delivery", ROOT / "delivery_evidence.py")
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)
from registration_delivery import target_binding, check_reviewed_instance_plan, observe_registration_groups, observe_ca_root


def test_ca_probe_refuses_non_private_target_before_any_provider_call():
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"provider called: {name}")
    with pytest.raises(ValueError, match="private CA target"):
        observe_ca_root(Forbidden(), Forbidden(), {"approved_root_sha256": "a" * 64},
                        certificate_arn="unused", runner_url="http://example.invalid/gitlab",
                        root_file="unused", ca_secret_arn="unused", ca_version="")



def test_saved_instance_plan_refuses_unapproved_mutations_and_missing_gate():
    import hashlib
    plan = {"resource_changes": [{"type": "aws_instance", "change": {
        "actions": ["update"], "before": {"id": "i-1234567890abcdef0", "user_data": "old"},
        "after": {"id": "i-1234567890abcdef0", "user_data": "new"}}}]}
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    targets = {"reviewed_plan_sha256": digest}
    with pytest.raises(ValueError, match="ungated"):
        check_reviewed_instance_plan(plan, targets, "i-1234567890abcdef0")
    assert check_reviewed_instance_plan(plan, targets, "i-1234567890abcdef0", instance_gate=True)
    for actions, kind in ((["create"], "aws_instance"), (["update"], "aws_iam_role"),
                          (["delete", "create"], "aws_instance")):
        changed = json.loads(json.dumps(plan))
        changed["resource_changes"][0]["change"]["actions"] = actions
        changed["resource_changes"][0]["type"] = kind
        targets["reviewed_plan_sha256"] = hashlib.sha256(json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with pytest.raises(ValueError):
            check_reviewed_instance_plan(changed, targets, "i-1234567890abcdef0", instance_gate=True)
    with pytest.raises(ValueError, match="reviewed plan"):
        check_reviewed_instance_plan(plan, targets, "i-1234567890abcdef0", instance_gate=True)


@pytest.mark.asyncio
async def test_group_readbacks_fail_closed_before_claiming_isolation():
    class Admin:
        owner_id = 4
        async def request(self, method, endpoint, **kwargs):
            if endpoint == "groups/12":
                import hashlib
                key = hashlib.sha256(b"tenant-a").hexdigest()[:24]
                return {"id": 12, "path": f"tenant-{key}", "full_path": f"tenant-{key}",
                        "description": f"ADP tenant {key}", "parent_id": None}
            if endpoint == "groups/60":
                import hashlib
                key = hashlib.sha256(b"tenant-b").hexdigest()[:24]
                return {"id": 60, "path": f"tenant-{key}", "full_path": f"tenant-{key}",
                        "description": f"ADP tenant {key}", "parent_id": None}
            if endpoint == "groups/24":
                import hashlib
                key = hashlib.sha256(b"tenant-a").hexdigest()[:24]
                return {"id": 24, "parent_id": 12, "path": "product-product-a",
                        "full_path": f"tenant-{key}/product-product-a", "description": "ADP product product-a"}
            if endpoint == "projects/7":
                import hashlib
                key = hashlib.sha256(b"tenant-a").hexdigest()[:24]
                return {"id": 7, "namespace": {"id": 24, "kind": "group"}, "path": "demo",
                        "path_with_namespace": f"tenant-{key}/product-product-a/demo",
                        "visibility": "private", "shared_runners_enabled": False}
            if endpoint.startswith("runners/"):
                runner_id = int(endpoint.split("/")[1]); group = 12 if runner_id == 101 else 60
                return {"id": runner_id, "runner_type": "group_type", "paused": True,
                        "locked": True, "run_untagged": False, "description": f"ADP runner op-{group}",
                        "tag_list": [f"adp-group-{group}"]}
            raise AssertionError(endpoint)
        async def pages(self, endpoint, **kwargs):
            if endpoint.endswith("/members") or endpoint.endswith("/members/all"):
                return [{"id": 4, "access_level": 50}]
            return {"groups/12/runners": [{"id": 101}], "groups/60/runners": [{"id": 202}]}[endpoint]
    targets = {"tenant_parent": 12, "product_subgroup": 24, "project": 7,
               "qualification_parent": 60, "approved_root_sha256": "d" * 64, "reviewed_plan_sha256": "e" * 64}
    config = {"product": {"tenant_id": "tenant-a", "group_id": 12, "product_id": "product-a",
                          "product_group_id": 24, "project_id": 7, "runner_id": 101, "operation_id": "op-12"},
              "qualification": {"tenant_id": "tenant-b", "group_id": 60, "runner_id": 202,
                                "operation_id": "op-60"}}
    # Invalid operation IDs cannot be laundered into live runner readbacks.
    with pytest.raises(ValueError, match="owned runner operation"):
        await observe_registration_groups(Admin(), config, targets)
    for group, runner_id, operation in (("product", 101, "00000000-0000-4000-8000-000000000101"),
                                        ("qualification", 202, "00000000-0000-4000-8000-000000000202")):
        config[group]["operation_id"] = operation
    class BoundAdmin(Admin):
        async def request(self, method, endpoint, **kwargs):
            if endpoint.startswith("runners/"):
                runner_id = int(endpoint.split("/")[1]); group = 12 if runner_id == 101 else 60
                return {"id": runner_id, "runner_type": "group_type", "paused": True,
                        "locked": True, "run_untagged": False, "description": f"ADP runner {config['product' if group == 12 else 'qualification']['operation_id']}",
                        "tag_list": [f"adp-group-{group}"]}
            return await super().request(method, endpoint, **kwargs)
    observed = await observe_registration_groups(BoundAdmin(), config, targets)
    assert set(observed) == {"registration-product-group", "registration-qualification-groups"}
    class LeakingAdmin(BoundAdmin):
        async def pages(self, endpoint, **kwargs):
            if endpoint == "groups/60/runners":
                return [{"id": 101}, {"id": 202}]
            return await super().pages(endpoint, **kwargs)
    with pytest.raises(ValueError, match="isolation"):
        await observe_registration_groups(LeakingAdmin(), config, targets)

# Copied unchanged from the accepted delivery contract, independent of the module.
ACCEPTED_CHECKS = [
    "registration-product-group", "registration-qualification-groups", "ca-root-verified",
    "group-token-scope", "runner-revocation", "instance-no-unreviewed-change",
]


def sample(tmp_path):
    from test_delivery_evidence import inputs
    env, contract, identity, checkout = inputs()
    contract = contract.model_copy(update={"required_checks": ACCEPTED_CHECKS})
    env.update(ADP_DELIVERY_CONTRACT_JSON=contract.model_dump_json(), ADP_CONTRACT_DIGEST=contract.digest,
               ADP_REGISTRATION_TARGETS_JSON=json.dumps({"tenant_parent": 12, "product_subgroup": 24,
                   "project": 7, "qualification_parent": 60, "approved_root_sha256": "d" * 64,
                   "reviewed_plan_sha256": "e" * 64}))
    observed = {
        "registration-product-group": {"tenant_parent": 12, "product_subgroup": 24, "project": 7,
            "tenant_members_verified": True, "product_namespace_verified": True,
            "group_runner_type": "group_type", "runner_paused": True, "shared_runners": False},
        "registration-qualification-groups": {"qualification_parent": 60, "tenant_members_verified": True,
            "distinct_parent": True, "group_runner_type": "group_type", "runner_paused": True,
            "cross_group_assignment": False},
        "ca-root-verified": {"approved_root_sha256": "d" * 64, "https_liveness": 200,
            "chain_verified": True, "stored_bundle_readback_verified": True},
        "group-token-scope": {"group_bound_secrets_verified": True, "runner_group_assignment_verified": True,
            "run_untagged": False, "locked": True, "admin_secret_policy_verified": True,
            "token_secret_policies_verified": True},
        "runner-revocation": {"owned_probe_runner": True, "token_before": "accepted",
            "token_after": "rejected", "runner_absent": True},
        "instance-no-unreviewed-change": {"reviewed_plan_sha256": "e" * 64,
            "applied_plan_matches_review": True, "post_apply_zero_change_plan": True},
    }
    receipt = {
        "schema_version": 1, "source_revision": checkout,
        "workflow_run_id": int(env["GITHUB_RUN_ID"]), "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "account_id": identity["Account"], "region": env["AWS_REGION"],
        "correlation": env["ADP_CORRELATION"], "targets_sha256": target_binding(env)[1],
        "checks": {name: {"verdict": "pass", "observed": fields,
            "evidence_ref": f"s3://private-evidence/gitlab/runs/456/attempts/1/{name}.json?versionId=synthetic-{name}"}
            for name, fields in observed.items()},
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt)); path.chmod(0o600)
    env["ADP_REGISTRATION_RECEIPT"] = str(path)
    return env, contract, identity, checkout, receipt, path


def test_semantic_checks_use_private_versioned_refs_not_action_step_success(tmp_path):
    env, contract, identity, checkout, receipt, _ = sample(tmp_path)
    result = delivery.produce(env, identity, checkout)
    assert [check.id for check in result.checks] == ACCEPTED_CHECKS
    assert [check.evidence_ref for check in result.checks] == [receipt["checks"][name]["evidence_ref"] for name in ACCEPTED_CHECKS]
    assert not set(json.loads(env["ADP_DELIVERY_STEP_OUTCOMES"])) & set(ACCEPTED_CHECKS)


@pytest.mark.parametrize("change", ["missing", "unsafe", "different_run", "different_attempt", "wrong_account",
    "missing_check", "failed", "setup_only", "fake_url", "stale_ref", "stale_attempt_ref", "wrong_group",
    "unrevoked", "duplicate_ref", "changed_targets", "missing_targets", "wrong_plan", "instance_drift",
    "failed_setup", "invented_contract", "setup_union_contract", "wrong_workflow", "boolean_id"])
def test_refuses_missing_stale_or_nonsemantic_live_evidence(tmp_path, change):
    env, contract, identity, checkout, receipt, path = sample(tmp_path)
    checks = receipt["checks"]
    ca = checks["ca-root-verified"]
    if change == "missing": path.unlink()
    elif change == "unsafe": path.chmod(0o644)
    elif change == "different_run": receipt["workflow_run_id"] += 1
    elif change == "different_attempt": receipt["run_attempt"] += 1
    elif change == "wrong_account": receipt["account_id"] = "999999999999"
    elif change == "missing_check": del checks["runner-revocation"]
    elif change == "failed": ca["verdict"] = "failed"
    elif change == "setup_only": ca["observed"] = {"terraform_apply": "success"}
    elif change == "fake_url": ca["evidence_ref"] = "https://github.com/example/actions/runs/456#apply"
    elif change == "stale_ref": ca["evidence_ref"] = ca["evidence_ref"].replace('/runs/456/', '/runs/1456/')
    elif change == "stale_attempt_ref": ca["evidence_ref"] = ca["evidence_ref"].replace('/attempts/1/', '/attempts/2/')
    elif change == "wrong_group": checks["registration-product-group"]["observed"]["tenant_parent"] = 37
    elif change == "unrevoked": checks["runner-revocation"]["observed"]["token_after"] = "accepted"
    elif change == "duplicate_ref": ca["evidence_ref"] = checks["runner-revocation"]["evidence_ref"]
    elif change in {"changed_targets", "boolean_id"}:
        targets = json.loads(env["ADP_REGISTRATION_TARGETS_JSON"])
        targets["tenant_parent"] = 37 if change == "changed_targets" else True
        env["ADP_REGISTRATION_TARGETS_JSON"] = json.dumps(targets)
    elif change == "missing_targets": del env["ADP_REGISTRATION_TARGETS_JSON"]
    elif change == "wrong_plan": checks["instance-no-unreviewed-change"]["observed"]["reviewed_plan_sha256"] = "f" * 64
    elif change == "instance_drift": checks["instance-no-unreviewed-change"]["observed"]["post_apply_zero_change_plan"] = False
    elif change == "failed_setup":
        steps = json.loads(env["ADP_DELIVERY_STEP_OUTCOMES"]); steps["apply"] = "failure"
        env["ADP_DELIVERY_STEP_OUTCOMES"] = json.dumps(steps)
    elif change in {"invented_contract", "setup_union_contract"}:
        required = (["tenant_scope", "qualification_isolation", "ca_trust", "token_scope", "rotation", "revocation"]
                    if change == "invented_contract" else ACCEPTED_CHECKS + list(json.loads(env["ADP_DELIVERY_STEP_OUTCOMES"])))
        contract = contract.model_copy(update={"required_checks": required})
        env.update(ADP_DELIVERY_CONTRACT_JSON=contract.model_dump_json(), ADP_CONTRACT_DIGEST=contract.digest)
    elif change == "wrong_workflow": env["ADP_DELIVERY_WORKFLOW"] = "gitlab-runners-apply.yml"
    if path.exists(): path.write_text(json.dumps(receipt))
    with pytest.raises((ValueError, FileNotFoundError, KeyError)):
        delivery.produce(env, identity, checkout)


def test_refuses_successful_setup_relabelled_as_six_live_checks(tmp_path):
    env, contract, identity, checkout, _, path = sample(tmp_path)
    path.unlink()
    env["ADP_DELIVERY_STEP_OUTCOMES"] = json.dumps(dict.fromkeys(ACCEPTED_CHECKS, "success"))
    with pytest.raises(ValueError): delivery.produce(env, identity, checkout)


@pytest.mark.parametrize("missing", ACCEPTED_CHECKS)
def test_live_checks_must_be_complete_in_contract(tmp_path, missing):
    env, contract, identity, checkout, _, _ = sample(tmp_path)
    reduced = contract.model_copy(update={"required_checks": [key for key in ACCEPTED_CHECKS if key != missing]})
    env.update(ADP_DELIVERY_CONTRACT_JSON=reduced.model_dump_json(), ADP_CONTRACT_DIGEST=reduced.digest)
    with pytest.raises(ValueError): delivery.produce(env, identity, checkout)


def test_private_liveness_recovers_from_reboot_without_changing_tls_client():
    import httpx
    from registration_delivery import wait_for_private_liveness
    responses = iter([httpx.ConnectError('starting'), httpx.Response(503), httpx.Response(200, json={'status': 'ok'})])
    class Client:
        def get(self, url):
            assert url == 'https://gitlab.example.internal/gitlab/-/liveness'
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result
    pauses = []
    wait_for_private_liveness(Client(), 'https://gitlab.example.internal/gitlab/-/liveness', clock=lambda: 0, pause=pauses.append)
    assert pauses == [5, 5]


@pytest.mark.parametrize('status,body', [(401, {}), (302, {}), (200, {'status': 'failed'})])
def test_private_liveness_rejects_non_startup_failure(status, body):
    import httpx
    from registration_delivery import wait_for_private_liveness
    class Client:
        def get(self, url):
            return httpx.Response(status, json=body)
    def forbidden_pause(_):
        raise AssertionError('permanent failure was retried')
    with pytest.raises(ValueError, match='liveness not verified'):
        wait_for_private_liveness(Client(), 'https://gitlab.example.internal/gitlab/-/liveness', pause=forbidden_pause)


def test_private_liveness_has_a_bounded_startup_deadline():
    import httpx
    from registration_delivery import wait_for_private_liveness
    elapsed = [0]
    calls = []
    class Client:
        def get(self, url):
            calls.append(url)
            raise httpx.ReadTimeout('starting')
    def advance(seconds):
        elapsed[0] += seconds
    with pytest.raises(ValueError, match='startup deadline'):
        wait_for_private_liveness(Client(), 'https://gitlab.example.internal/gitlab/-/liveness',
                                  timeout=6, clock=lambda: elapsed[0], pause=advance)
    assert elapsed == [6]
    assert len(calls) == 3
