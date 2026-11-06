"""Verify durable Product recovery and its protected workflow boundary."""

import hashlib
import io
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from botocore.credentials import Credentials

from tests.e2e.cli_uplift import cleanup, config, ports, statestore
from tests.e2e.product_onboarding import product_execution, recovery_job

SOURCE = "a" * 40
RUN_ID = "product-123456-2"
EVALUATION_ID = "adp-e2e-20261020-113000-abcdef"
ROOT = Path(__file__).resolve().parents[3]


class RecoveryAws:
    def __init__(self, cfg, role="adp-cli-uplift-eval-orchestrator", account=None):
        self.account = str(cfg["platform_account"] if account is None else account)
        self.role = role

    def call(self, service, operation):
        assert (service, operation) == ("sts", "get_caller_identity")
        return {
            "Account": self.account,
            "Arn": f"arn:aws:sts::{self.account}:assumed-role/{self.role}/fixture",
        }


def test_recovery_pointer_is_private_immutable_and_verified(monkeypatch):
    store = statestore.S3StateStore(object(), "private-bucket")
    objects = {}
    monkeypatch.setattr(
        store, "_put", lambda key, value: objects.__setitem__(key, value)
    )
    monkeypatch.setattr(store, "_get", objects.get)
    binding = {"evaluation_id": EVALUATION_ID}
    store.bind_product_run(RUN_ID, binding)
    store.bind_product_run(RUN_ID, binding)
    assert store.product_run(RUN_ID) == binding
    assert list(objects) == [statestore.product_run_key(RUN_ID)]
    with pytest.raises(statestore.StateStoreError, match="another evaluation"):
        store.bind_product_run(RUN_ID, {"evaluation_id": "different"})
    assert store.product_run(RUN_ID) == binding
    with pytest.raises(statestore.StateStoreError, match="workflow run identity"):
        store.product_run("../another-run")


@pytest.mark.parametrize(
    "selection", ["api_product_lifecycle", "api_product_lifecycle,api_denial_oracle"]
)
@pytest.mark.parametrize(
    "tamper", ["missing", "revision", "workflow", "fingerprint", "selection"]
)
def test_recovery_refuses_missing_or_mismatched_pointer(monkeypatch, tamper, selection):
    cfg = config.load(config.EXAMPLE_PATH)
    cfg["expected_revision"] = SOURCE
    cfg["product_cases"] = selection
    binding = product_execution.recovery_binding(cfg, RUN_ID, EVALUATION_ID)
    if tamper == "missing":
        binding = None
    else:
        binding[
            {
                "revision": "expected_revision",
                "workflow": "workflow_run_id",
                "fingerprint": "config_fingerprint",
                "selection": "product_cases",
            }[tamper]
        ] = "wrong"

    class Store:
        def product_run(self, run_id):
            assert run_id == RUN_ID
            return binding

    monkeypatch.setattr(
        product_execution.runner,
        "main",
        lambda *args, **kwargs: pytest.fail("cleanup started"),
    )
    with pytest.raises(Exception, match="identity|binding"):
        product_execution.recover(cfg, RUN_ID, RecoveryAws(cfg), store=Store())


@pytest.mark.parametrize(
    "role,account",
    [
        ("foreign-role", None),
        ("adp-cli-uplift-eval-orchestrator", "000000000001"),
    ],
)
def test_recovery_refuses_foreign_identity_before_state_access(
    monkeypatch, role, account
):
    cfg = config.load(config.EXAMPLE_PATH)
    monkeypatch.setattr(
        product_execution.statestore,
        "default_store",
        lambda *_: pytest.fail("state accessed before identity check"),
    )
    with pytest.raises(ports.PortError, match="reviewed recovery role"):
        product_execution.recover(cfg, RUN_ID, RecoveryAws(cfg, role, account))


@pytest.mark.parametrize(
    "selection", ["api_product_lifecycle", "api_product_lifecycle,api_denial_oracle"]
)
@pytest.mark.parametrize("role", ["adp-cli-uplift-eval-orchestrator", "foreign-role"])
@pytest.mark.parametrize("account", [None, "000000000001"])
def test_recovery_restores_only_exact_selected_product_run(
    monkeypatch, selection, role, account
):
    cfg = config.load(config.EXAMPLE_PATH)
    cfg.update(expected_revision=SOURCE, product_cases=selection)
    binding = product_execution.recovery_binding(cfg, RUN_ID, EVALUATION_ID)
    document = {"product_run_id": RUN_ID, "instance_id": "i-00000001"}
    workspace_id = (
        EVALUATION_ID
        + "-product-"
        + hashlib.sha256((EVALUATION_ID + ":product-workspace").encode()).hexdigest()[
            :24
        ]
    )
    manifest = {
        "prefix": EVALUATION_ID,
        "resources": [
            {
                "prefix": EVALUATION_ID,
                "kind": "product_fixture",
                "id": RUN_ID,
                "account": str(cfg["platform_account"]),
                "region": cfg["region"],
                "detail": {
                    "tenant_id": workspace_id,
                    "instance_id": document["instance_id"],
                    "source_revision": SOURCE,
                    "idempotency_key": "product-create-"
                    + hashlib.sha256(
                        (RUN_ID + ":" + EVALUATION_ID).encode()
                    ).hexdigest(),
                },
            }
        ],
    }

    class Store:
        def product_run(self, run_id):
            assert run_id == RUN_ID
            return binding

        def load(self, evaluation_id):
            assert evaluation_id == EVALUATION_ID
            return document, manifest

    def cleanup_selected(arguments, *, stages, store):
        assert arguments[:3] == ["--mode", "cleanup", "--restore"]
        assert arguments[arguments.index("--evaluation-id") + 1] == EVALUATION_ID
        assert store.product_run(RUN_ID) == binding
        restored_cfg = config.load(Path(arguments[arguments.index("--config") + 1]))
        assert restored_cfg["product_cases"] == selection
        assert restored_cfg["product_run_id"] == RUN_ID
        product_execution.runner.State(
            Path(arguments[arguments.index("--state-dir") + 1])
        ).write({"cleanup_ok": True})
        return 0

    monkeypatch.setattr(product_execution.runner, "main", cleanup_selected)
    aws = RecoveryAws(cfg, role=role, account=account)
    if role != "adp-cli-uplift-eval-orchestrator" or account is not None:
        with pytest.raises(ports.PortError, match="reviewed recovery role"):
            product_execution.recover(cfg, RUN_ID, aws, store=Store())
        return
    assert product_execution.recover(cfg, RUN_ID, aws, store=Store()) == {
        "evaluation_id": EVALUATION_ID,
        "cleanup_verified": True,
    }


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "account",
        "region",
        "tenant_id",
        "instance_id",
        "source_revision",
        "idempotency_key",
    ],
)
def test_recovery_refuses_foreign_product_fixture_before_cleanup(monkeypatch, field):
    cfg = config.load(config.EXAMPLE_PATH)
    cfg.update(expected_revision=SOURCE, product_cases="api_product_lifecycle")
    binding = product_execution.recovery_binding(cfg, RUN_ID, EVALUATION_ID)
    workspace_id = (
        EVALUATION_ID
        + "-product-"
        + hashlib.sha256((EVALUATION_ID + ":product-workspace").encode()).hexdigest()[
            :24
        ]
    )
    detail = {
        "tenant_id": workspace_id,
        "instance_id": "i-00000001",
        "source_revision": SOURCE,
        "idempotency_key": "product-create-"
        + hashlib.sha256((RUN_ID + ":" + EVALUATION_ID).encode()).hexdigest(),
    }
    entry = {
        "prefix": EVALUATION_ID,
        "kind": "product_fixture",
        "id": RUN_ID,
        "account": str(cfg["platform_account"]),
        "region": cfg["region"],
        "detail": detail,
    }
    if field in detail:
        detail[field] = "foreign"
    else:
        entry[field] = "foreign"

    class Store:
        def product_run(self, _run_id):
            return binding

        def load(self, _evaluation_id):
            return {"product_run_id": RUN_ID, "instance_id": "i-00000001"}, {
                "prefix": EVALUATION_ID,
                "resources": [entry],
            }

    monkeypatch.setattr(
        product_execution.runner,
        "main",
        lambda *args, **kwargs: pytest.fail("cleanup started"),
    )
    with pytest.raises(ports.PortError, match="fixture is not owned"):
        product_execution.recover(cfg, RUN_ID, RecoveryAws(cfg), store=Store())


@pytest.mark.parametrize("tamper", ["run", "manifest", "resource"])
def test_recovery_refuses_unowned_restored_state(monkeypatch, tamper):
    cfg = config.load(config.EXAMPLE_PATH)
    cfg["expected_revision"] = SOURCE
    cfg["product_cases"] = "api_product_lifecycle"
    binding = product_execution.recovery_binding(cfg, RUN_ID, EVALUATION_ID)
    document = {"product_run_id": RUN_ID}
    manifest = {"prefix": "", "resources": []}
    if tamper == "run":
        document["product_run_id"] = "product-999-1"
    elif tamper == "manifest":
        manifest["prefix"] = "foreign-evaluation"
    else:
        manifest["resources"].append({"prefix": "foreign-evaluation"})

    class Store:
        def product_run(self, _run_id):
            return binding

        def load(self, _evaluation_id):
            return document, manifest

    monkeypatch.setattr(
        product_execution.runner,
        "main",
        lambda *args, **kwargs: pytest.fail("cleanup started"),
    )
    with pytest.raises(Exception, match="not owned"):
        product_execution.recover(cfg, RUN_ID, RecoveryAws(cfg), store=Store())


def test_product_sweep_retains_instance_until_verified_retirement(tmp_path):
    manifest = cleanup.Manifest(tmp_path / "manifest.json", EVALUATION_ID)
    location = {"account": "000000000000", "region": "us-east-1"}
    manifest.record("ec2_instance", "i-owned", **location)
    manifest.record("adp_org", "owned-tenant", **location)
    manifest.record("adp_user", "owned-tenant/actor", **location)
    manifest.record("ec2_instance", "i-other", **location)
    manifest.record("github_app", "shared", reused=True)
    manifest.record(
        "product_fixture",
        RUN_ID,
        detail={
            "tenant_id": "owned-tenant",
            "actor_id": "actor",
            "instance_id": "i-owned",
        },
        **location,
    )
    deleted = []
    deleters = {
        kind: lambda identifier, **_scope: deleted.append(identifier)
        for kind in ("ec2_instance", "adp_org", "adp_user")
    }
    ok, _ = cleanup.sweep(manifest, deleters)
    assert ok is False
    assert deleted == ["i-other"]
    assert {item["id"] for item in manifest.outstanding()} == {
        "i-owned",
        "owned-tenant",
        "owned-tenant/actor",
        RUN_ID,
    }
    manifest.mark("product_fixture", RUN_ID, "deleted")
    ok, _ = cleanup.sweep(manifest, deleters)
    assert ok is True
    assert set(deleted) == {"i-owned", "i-other", "owned-tenant", "owned-tenant/actor"}
    assert "shared" not in deleted
    assert manifest.outstanding() == []


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "running",
        "wrong_role",
        "wrong_instance",
        "wrong_workspace",
        "missing_actor",
        "false_receipt",
    ],
)
def test_terminated_instance_recovers_only_the_original_product(
    tmp_path, monkeypatch, failure
):
    cfg = {
        "gateway_url": "https://gateway.example.invalid",
        "platform_account": "000000000000",
        "region": "us-east-1",
        "expected_revision": SOURCE,
        "internal_invoke_url": "https://private.execute-api.us-east-1.amazonaws.com/dev",
        "sts_endpoint": "https://sts.us-east-1.amazonaws.com",
    }
    instance_id = "i-00000001"
    workspace_id = (
        EVALUATION_ID
        + "-product-"
        + hashlib.sha256((EVALUATION_ID + ":product-workspace").encode()).hexdigest()[
            :24
        ]
    )
    actor_id = "owned-actor"
    actor_row_id = "owned-actor-row"
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        EVALUATION_ID,
        on_change=lambda *_args, **_kwargs: None,
    )
    manifest.record(
        "ec2_instance",
        instance_id,
        account=cfg["platform_account"],
        region=cfg["region"],
    )
    if failure != "missing_actor":
        manifest.record("adp_user", workspace_id + "/" + actor_row_id)
    manifest.record(
        "product_fixture",
        RUN_ID,
        detail={
            "tenant_id": "other-workspace"
            if failure == "wrong_workspace"
            else workspace_id,
            "actor_id": actor_id,
            "actor_row_id": actor_row_id,
            "source_revision": SOURCE,
            "idempotency_key": "product-create-"
            + hashlib.sha256((RUN_ID + ":" + EVALUATION_ID).encode()).hexdigest(),
            "instance_id": "i-00000002" if failure == "wrong_instance" else instance_id,
        },
        account=cfg["platform_account"],
        region=cfg["region"],
    )
    attempts = []

    class Aws:
        def call(self, service, operation, **kwargs):
            attempts.append((service, operation))
            if (service, operation) == ("sts", "get_caller_identity"):
                return {
                    "Account": cfg["platform_account"],
                    "Arn": f"arn:aws:sts::000000000000:assumed-role/{'other' if failure == 'wrong_role' else 'adp-cli-uplift-eval-orchestrator'}/run",
                }
            assert (service, operation) == ("ec2", "describe_instances")
            assert kwargs == {"InstanceIds": [instance_id]}
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": instance_id,
                                "State": {
                                    "Name": "running"
                                    if failure == "running"
                                    else "terminated"
                                },
                            }
                        ]
                    }
                ]
            }

    def worker(*_args, **_kwargs):
        attempts.append("instance-unavailable")
        raise ports.PortError("Instance unavailable")

    def retire(_cfg, _aws, intent):
        attempts.append("independent-retirement")
        assert intent["tenant_id"] == workspace_id and intent["run_id"] == RUN_ID
        return {
            "success": True,
            "stage": "complete",
            "detail": {
                "run_id": RUN_ID,
                "fixture_removed": failure != "false_receipt",
                "product_id": "owned-product",
                "project_id": "owned-project",
            },
        }

    monkeypatch.setattr(product_execution, "retire_independently", retire)
    monkeypatch.setattr(
        product_execution.evaluator, "wire", lambda _cfg, supplied: supplied
    )
    monkeypatch.setattr(
        product_execution.stages,
        "build_stages",
        lambda *_: {
            "cleanup": lambda _ctx: attempts.append("ordinary_cleanup") or True
        },
    )
    mapping = product_execution.build_stages(
        cfg,
        {"source_revision": SOURCE},
        RUN_ID,
        supplied={"run_worker": worker, "aws": Aws()},
    )
    ctx = {
        "evaluation_id": EVALUATION_ID,
        "document": {"instance_id": instance_id},
        "manifest": manifest,
    }
    if failure is not None:
        with pytest.raises(ports.PortError, match="retirement is still outstanding"):
            mapping["cleanup"](ctx)
        assert any(item["kind"] == "product_fixture" for item in manifest.outstanding())
    else:
        mapping["cleanup"](ctx)
        assert ctx["document"]["product_cleanup_receipt"] == {
            "run_id": RUN_ID, "fixture_removed": True,
            "product_id": "owned-product", "project_id": "owned-project",
        }
        assert not any(
            item["kind"] == "product_fixture" for item in manifest.outstanding()
        )
    assert ("independent-retirement" in attempts) is (
        failure in (None, "false_receipt")
    )
    assert attempts[-1] == "ordinary_cleanup"


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "wrong_role",
        "wrong_account",
        "wrong_route",
        "denied",
        "oversize",
        "false_receipt",
    ],
)
def test_independent_retirement_signs_only_the_reviewed_route_and_checks_receipt(
    monkeypatch, failure
):
    cfg = {
        "platform_account": "000000000000",
        "region": "us-east-1",
        "internal_invoke_url": "https://private.execute-api.us-east-1.amazonaws.com/dev",
    }
    if failure == "wrong_route":
        cfg["internal_invoke_url"] = "https://other.example.invalid/dev"
    intent = {
        "run_id": RUN_ID,
        "tenant_id": "owned-tenant",
        "actor_id": "owned-actor",
        "source_revision": SOURCE,
        "idempotency_key": "product-create-" + "a" * 64,
    }
    requests = []

    class Aws:
        def call(self, service, operation):
            assert (service, operation) == ("sts", "get_caller_identity")
            return {
                "Account": "000000000001"
                if failure == "wrong_account"
                else cfg["platform_account"],
                "Arn": f"arn:aws:sts::000000000000:assumed-role/{'wrong' if failure == 'wrong_role' else 'adp-cli-uplift-eval-orchestrator'}/run",
            }

        def session(self):
            class Session:
                def get_credentials(self):
                    return Credentials("EXAMPLEKEY", "EXAMPLESECRET", "EXAMPLETOKEN")

            return Session()

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 20
            assert (
                request.full_url
                == cfg["internal_invoke_url"] + "/internal/v1/product-evaluation/retire"
            )
            assert request.get_method() == "POST"
            assert json.loads(request.data) == intent
            assert request.get_header("Authorization").startswith("AWS4-HMAC-SHA256 ")
            if failure == "denied":
                raise OSError("private provider response")
            receipt = {
                "run_id": RUN_ID,
                "fixture_removed": failure != "false_receipt",
                "cleanup_revision": 3,
                "product_id": "owned-product",
                "project_id": "owned-project",
            }
            return Response(
                b"x" * 8193 if failure == "oversize" else json.dumps(receipt).encode()
            )

    def opener(*handlers):
        assert any(type(handler).__name__ == "NoRedirect" for handler in handlers)
        assert any(
            type(handler).__name__ == "ProxyHandler" and handler.proxies == {}
            for handler in handlers
        )
        return Opener()

    monkeypatch.setattr(product_execution.urllib.request, "build_opener", opener)
    if failure:
        with pytest.raises(
            ports.PortError, match="identity|denied or unavailable|incomplete"
        ):
            product_execution.retire_independently(cfg, Aws(), intent)
    else:
        assert (
            product_execution.retire_independently(cfg, Aws(), intent)["detail"][
                "fixture_removed"
            ]
            is True
        )
    assert len(requests) == (
        0 if failure in ("wrong_role", "wrong_account", "wrong_route") else 1
    )


@pytest.mark.parametrize(
    "selection", ["api_product_lifecycle", "api_product_lifecycle,api_denial_oracle"]
)
def test_recovery_job_reports_success_only_after_verified_cleanup(
    monkeypatch, tmp_path, selection
):
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(
        recovery_job.live,
        "resolved_config",
        lambda revision: {"expected_revision": revision},
    )
    monkeypatch.setattr(
        recovery_job.ports, "default_ports", lambda cfg: {"aws": object()}
    )
    monkeypatch.setattr(
        recovery_job.product_execution,
        "recover",
        lambda cfg, run_id, aws: {
            "cleanup_verified": cfg["product_cases"] == selection,
            "evaluation_id": EVALUATION_ID,
        },
    )
    assert (
        recovery_job.main(["--expected-revision", SOURCE, "--cases", selection])[
            "cleanup_verified"
        ]
        is True
    )
    assert (tmp_path / "out").read_text() == "cleanup_ok=true\n"
    monkeypatch.setattr(
        recovery_job.product_execution,
        "recover",
        lambda cfg, run_id, aws: {"cleanup_verified": False},
    )
    with pytest.raises(ValueError, match="verify cleanup"):
        recovery_job.main(["--expected-revision", SOURCE, "--cases", selection])
    assert (tmp_path / "out").read_text() == "cleanup_ok=true\n"


@pytest.mark.parametrize(
    "selection",
    [
        "api_denial_oracle,api_product_lifecycle",
        "api_recovery_cleanup,api_product_lifecycle",
    ],
)
def test_recovery_rejects_unordered_selection_before_target_access(
    monkeypatch, selection
):
    monkeypatch.setattr(
        recovery_job.live,
        "resolved_config",
        lambda *_: pytest.fail("target configuration read"),
    )
    with pytest.raises(ValueError, match="reviewed ordered Product selection"):
        recovery_job.main(["--expected-revision", SOURCE, "--cases", selection])


def test_recovery_workflow_runs_independently_and_never_falls_back_to_live_output():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/gateway-live-tests.yml").read_text()
    )
    job = workflow["jobs"]["recover_product"]
    assert job["needs"] == "live"
    assert "always()" in job["if"]
    assert job["environment"] == "dev"
    assert job["permissions"]["id-token"] == "write"
    steps = job["steps"]
    assert any("aws sts get-caller-identity" in step.get("run", "") for step in steps)
    assert any(
        "tests.e2e.product_onboarding.recovery_job" in step.get("run", "")
        for step in steps
    )
    assert "steps.recover.outputs.cleanup_ok" in job["outputs"]["cleanup_ok"]
    assert '--cases "$PRODUCT_CASES"' in next(
        step["run"]
        for step in steps
        if "tests.e2e.product_onboarding.recovery_job" in step.get("run", "")
    )
    output = workflow[True]["workflow_call"]["outputs"]["cleanup_ok"]["value"]
    assert "jobs.recover_product.outputs.cleanup_ok == 'true'" in output
    assert "inputs.product_cases == '' || inputs.product_cases == 'none'" in output


def test_protected_recovery_is_opt_in_after_verified_dev_release_checks():
    deploy = yaml.safe_load((ROOT / ".github/workflows/gateway-deploy.yml").read_text())
    assert "product_recovery_hold" not in deploy[True]["workflow_dispatch"]["inputs"]
    call = deploy["jobs"]["product-recovery"]
    assert call["needs"] == ["product-acceptance-guard", "product-cli-evidence", "product-acceptance"]
    assert "product_recovery_hold" not in call["if"]
    assert "always()" in call["if"]
    assert "needs.product-acceptance-guard.result == 'success'" in call["if"]
    assert "needs.product-cli-evidence.result == 'success'" in call["if"]
    assert "needs.product-acceptance.result == 'success'" in call["if"]
    assert "refs/heads/main" in call["if"]
    assert "inputs.product_cases == 'api_product_lifecycle,api_denial_oracle,api_recovery_cleanup'" in call["if"]
    assert "(inputs.environment || 'dev') == 'dev'" in call["if"]
    assert call["uses"] == "./.github/workflows/gateway-product-recovery.yml"
    assert call["with"]["expected_revision"] == "${{ inputs.adp_source_revision }}"


def test_automatic_identity_is_durable_only_after_signed_checks_and_owned_cleanup():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-live-tests.yml").read_text())
    live_steps = workflow["jobs"]["live"]["steps"]
    published = next(index for index, step in enumerate(live_steps) if step.get("id") == "published_evidence")
    staged = next(index for index, step in enumerate(live_steps) if "product_automatic_identity stage" in step.get("run", ""))
    assert published < staged
    assert "steps.published_evidence.outcome == 'success'" in live_steps[staged]["if"]
    assert "steps.product_packet.outcome == 'success'" in live_steps[staged]["if"]
    assert "--grades test-results/product-d01-cases.json" in live_steps[staged]["run"]
    recovery_steps = workflow["jobs"]["recover_product"]["steps"]
    retired = next(index for index, step in enumerate(recovery_steps) if step.get("id") == "recover")
    finalized = next(index for index, step in enumerate(recovery_steps) if "product_automatic_identity complete" in step.get("run", ""))
    assert retired < finalized
    held_steps = yaml.safe_load((ROOT / ".github/workflows/gateway-product-recovery.yml").read_text())["jobs"]["hold"]["steps"]
    verified = next(index for index, step in enumerate(held_steps) if "product_automatic_identity verify" in step.get("run", ""))
    created = next(index for index, step in enumerate(held_steps) if "--hold-for-rollback" in step.get("run", ""))
    assert verified < created


def test_protected_recovery_orders_private_cross_job_readbacks_and_compensation():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-product-recovery.yml").read_text())
    jobs = workflow["jobs"]
    assert set(jobs) == {
        "hold", "handoff", "quiesce", "read_quiesced", "baseline",
        "read_baseline", "prepare_restore", "restore", "read_candidate", "cleanup",
    }
    assert "--hold-for-rollback" in str(jobs["hold"]["steps"])
    assert "product_recovery_handoff" in str(jobs["handoff"]["steps"])
    assert jobs["handoff"]["needs"] == "hold"
    for phase, upstream in (("quiesce", "handoff"), ("baseline", ["handoff", "read_quiesced"]), ("restore", ["handoff", "prepare_restore"])):
        job = jobs[phase]
        assert job["environment"] == "adp-gateway-deploy-dev"
        assert job["permissions"]["id-token"] == "write"
        assert job["needs"] == upstream
        assert "./.github/actions/trusted-deployment" in str(job["steps"])
        assert "toJSON(needs.handoff.outputs)" in str(job["steps"])
        assert f"--phase {'candidate' if phase == 'restore' else phase}" in str(job["steps"])
        assert "product-recovery-control.py" in str(job["steps"])
    for phase, upstream, choice in (("read_quiesced", "quiesce", "quiesced"), ("read_baseline", "baseline", "baseline"), ("read_candidate", "restore", "candidate")):
        job = jobs[phase]
        assert job["environment"] == "dev"
        assert job["needs"] == upstream
        assert f"--choice {choice}" in str(job["steps"])
        assert "product_recovery_observation" in str(job["steps"])
    assert "always()" in jobs["prepare_restore"]["if"]
    assert "needs.quiesce.result != 'skipped'" in jobs["prepare_restore"]["if"]
    assert "--choice restore-intent" in str(jobs["prepare_restore"]["steps"])
    assert "needs.prepare_restore.result == 'success'" in jobs["restore"]["if"]
    assert "always()" in jobs["cleanup"]["if"]
    assert "--hold-for-rollback" in str(jobs["cleanup"]["steps"])
    assert "needs.read_candidate.result" in str(jobs["cleanup"]["steps"])
    assert "steps.retire.outputs.cleanup_ok" in str(jobs["cleanup"]["steps"])
    assert not any("upload" in str(job["steps"]) for name, job in jobs.items() if name != "cleanup")
    steps = jobs["cleanup"]["steps"]
    final = next(index for index, step in enumerate(steps) if "product_d01_finalization" in step.get("run", ""))
    assert final > next(index for index, step in enumerate(steps) if step.get("id") == "retire")
    assert steps[final]["if"] == "always()"
    assert steps[final + 1]["uses"] == "./.github/actions/upload-ci-evidence-s3"
    assert steps[final + 1]["with"]["path"] == "test-results/product-d01-final.json"


@pytest.mark.parametrize(
    ("readback", "cleanup", "accepted"),
    [("success", "true", True), ("failure", "true", False), ("skipped", "true", False), ("success", "false", False), ("success", "", False)],
)
def test_qualification_refuses_restoration_or_owned_cleanup_failure(readback, cleanup, accepted):
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-product-recovery.yml").read_text())
    check = next(
        step for step in workflow["jobs"]["cleanup"]["steps"]
        if step.get("name") == "Refuse qualification without independent restoration and cleanup"
    )
    assert check["if"] == "always()"
    result = subprocess.run(
        ["bash", "-e", "-c", check["run"]],
        env={**os.environ, "READBACK": readback, "CLEANUP": cleanup},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert (result.returncode == 0) is accepted


@pytest.mark.parametrize(("failures", "accepted"), [(0, True), (2, True), (3, False)])
def test_protected_restore_retries_only_within_original_attempt(tmp_path, failures, accepted):
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-product-recovery.yml").read_text())
    restore = workflow["jobs"]["restore"]["steps"][-1]["run"]
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "aws").write_text("#!/bin/sh\nexit 0\n")
    (commands / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (commands / "python3").write_text(
        "#!/bin/bash\n"
        'count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)\n'
        'count=$((count + 1))\n'
        'printf "%s" "$count" > "$COUNT_FILE"\n'
        'test "$count" -gt "$FAILURES"\n'
    )
    for command in commands.iterdir():
        command.chmod(0o700)
    count_file = tmp_path / "attempts"
    result = subprocess.run(
        ["bash", "-c", restore],
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "BINDING": '{"run_id":"product-42-1"}',
            "COUNT_FILE": str(count_file),
            "FAILURES": str(failures),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert (result.returncode == 0) is accepted
    assert count_file.read_text() == str(min(failures + 1, 3))
