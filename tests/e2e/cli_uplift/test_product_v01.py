"""The instance driver couples lifecycle writes to verified owned cleanup."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "remote"))
import common
import product_v01
from tests.e2e.product_onboarding.product_recovery import InterruptedWrite
from tests.e2e.product_onboarding.test_product_profile_api import FixtureAPI


@pytest.fixture
def protected_dispatch(selected, tmp_path, monkeypatch):
    from tests.e2e.cli_uplift import bundle, cleanup

    config, fixture = selected
    run_id = config["evaluation_id"]
    workspace = run_id + "-product-" + "b" * 24
    config["product_workspace_id"] = workspace
    fixture.tenant_id = workspace
    monkeypatch.setattr(
        product_v01,
        "local_owner",
        lambda _config: (
            {
                "org_id": workspace,
                "user_id": "owned-actor",
                "adp_user_id": "owned-actor-row",
                "access_token": "owner-token",
            },
            fixture.client,
        ),
    )
    monkeypatch.setattr(bundle, "REMOTE_DIR", str(tmp_path))
    revision = config["expected_revision"]
    config.update(
        instance_id="i-owned",
        gateway_url="https://gateway.example.invalid",
        region="us-east-1",
        platform_account="000000000000",
        session_ref=str(tmp_path / "session.json"),
        product_actor_session_ref=str(tmp_path / "product-actor/session.json"),
        internal_invoke_url="https://private.execute-api.us-east-1.amazonaws.com/dev",
        product_actor_id="owned-actor",
        product_actor_row_id="owned-actor-row",
        sts_endpoint="https://sts.us-east-1.amazonaws.com",
        product_inventory={
            "source_revision": revision,
            "digest": "sha256:" + "c" * 64,
            "pods": [
                {
                    "uid": "owned-pod",
                    "address": "10.1.1.10",
                    "digest": "sha256:" + "c" * 64,
                }
            ],
        },
        product_probe={
            "success": True,
            "source_revision": revision,
            "pods": [{"uid": "owned-pod", "schema_sha256": "b" * 64}],
        },
    )
    cfg = {
        key: config[key]
        for key in (
            "gateway_url",
            "region",
            "platform_account",
            "expected_revision",
            "internal_invoke_url",
            "sts_endpoint",
        )
    }
    cfg.update(
        product_run_id=config["product_run_id"],
        cognito_user_pool_id="pool",
        product_cases=config["product_cases"],
    )
    manifest = cleanup.Manifest(
        tmp_path / "run.json", run_id, on_change=lambda *_args, **_kwargs: None
    )
    for kind, identity in (
        ("ec2_instance", "i-owned"),
        ("adp_org", workspace),
        ("adp_user", config["other_tenant_id"] + "/bootstrap-admin"),
        ("cognito_user", f"pool/{run_id}-product-admin@adp-eval.example"),
        ("adp_user", workspace + "/owned-actor-row"),
    ):
        manifest.record(
            kind, identity, account=cfg["platform_account"], region=cfg["region"]
        )
    manifest.record(
        "product_fixture",
        config["product_run_id"],
        detail={
            "tenant_id": workspace,
            "actor_id": "owned-actor",
            "actor_row_id": "owned-actor-row",
            "source_revision": revision,
            "idempotency_key": config["product_intent_key"],
            "instance_id": "i-owned",
            "actor_secret_id": f"adp/cli-uplift-eval/{run_id}-product-fixtures",
            "actor_cognito_id": f"pool/{run_id}-product-admin@adp-eval.example",
        },
        account=cfg["platform_account"],
        region=cfg["region"],
    )
    return config, cfg, manifest, fixture


def test_protected_dispatch_refuses_unrecorded_bootstrap_user(protected_dispatch):
    from tests.e2e.cli_uplift import cleanup, live, ports

    config, cfg, manifest, _fixture = protected_dispatch
    manifest.mark("adp_user", "other-tenant/bootstrap-admin", cleanup.DELETED)
    sent = []
    runner = live._run_worker(object(), cfg, lambda *_args: sent.append("install"))
    with pytest.raises(ports.PortError, match="Product driver dispatch"):
        runner("i-owned", "product_v01", config, manifest=manifest)
    assert sent == []


def test_instance_driver_refuses_unverified_second_tenant(selected):
    config, fixture = selected
    config["other_tenant_id"] = "foreign-tenant"
    with pytest.raises(common.RemoteError, match="separate tenant identity"):
        product_v01.run_selected(
            config, lambda: pytest.fail("release observed before actor check")
        )
    assert fixture.posts == 0


def test_protected_dispatch_runs_driver_and_verifies_retirement(
    protected_dispatch, monkeypatch
):
    from tests.e2e.cli_uplift import live

    config, cfg, manifest, fixture = protected_dispatch
    sent = []
    retired = []
    original_get = fixture.get
    fixture.get = (
        lambda path: SimpleNamespace(status_code=404) if retired else original_get(path)
    )
    monkeypatch.setattr(
        product_v01.product_probe,
        "execute",
        lambda _config, evidence: evidence.update(config["product_probe"]),
    )

    def retire(payload, evidence):
        ownership = json.loads(
            (Path(payload["work_dir"]) / "product-ownership.json").read_text()
        )
        retired.append(ownership)
        evidence.update(
            success=True,
            detail={
                "run_id": ownership["run_id"],
                "product_id": ownership["product_id"],
                "project_id": ownership["project_id"],
                "fixture_removed": True,
            },
        )

    monkeypatch.setattr(product_v01.product_cleanup, "execute", retire)

    class Ssm:
        def json_result(self, instance, _commands, *, purpose, timeout):
            sent.append((instance, purpose))
            document = {}
            product_v01.execute(config, document)
            return {"Status": "Success"}, document

    runner = live._run_worker(Ssm(), cfg, lambda *_args: sent.append("install"))
    receipt = runner("i-owned", "product_v01", config, manifest=manifest)
    assert receipt["results"]["api_product_lifecycle"]["revision"] == 2
    assert receipt["cleanup"]["fixture_removed"] is True
    assert fixture.posts == 2 and len(retired) == 1
    assert sent == ["install", ("i-owned", "product_v01")]


@pytest.mark.parametrize(
    "wrong", [None, "selection", "actor", "missing", "revocation", "product", "history"]
)
@pytest.mark.parametrize("recovery", [False, True])
def test_protected_denial_dispatch_binds_selected_case_and_member(
    protected_dispatch, wrong, recovery
):
    from tests.e2e.cli_uplift import live, ports

    config, cfg, manifest, _fixture = protected_dispatch
    cfg["product_cases"] = "api_product_lifecycle,api_denial_oracle" + (
        ",api_recovery_cleanup" if recovery else ""
    )
    config["product_cases"] = cfg["product_cases"]
    workspace = config["product_workspace_id"]
    username = config["evaluation_id"] + "-product-user@adp-eval.example"
    manifest.record(
        "adp_user_email",
        workspace + "/" + username,
        account=cfg["platform_account"],
        region=cfg["region"],
    )
    config["product_denial_actor"] = {
        "org_id": workspace,
        "username": username,
        "user_id": "ordinary-user",
        "cognito_sub_bound": True,
    }
    if wrong == "selection":
        config["product_cases"] = "api_product_lifecycle"
    elif wrong == "actor":
        config["product_denial_actor"]["user_id"] = "owned-actor"
    results = {
        "api_product_lifecycle": {
            "product_id": "owned-product",
            "project_id": "owned-project",
            "revision": 2,
        },
        "api_denial_oracle": {
            "product_id": "owned-product",
            "revision": 2,
            "history_count": 2,
            "revocation": {"user_id": "ordinary-user", "membership_revoked": True},
        },
    }
    if wrong == "missing":
        results.pop("api_denial_oracle")
    elif wrong == "revocation":
        results["api_denial_oracle"]["revocation"]["membership_revoked"] = False
    elif wrong == "product":
        results["api_denial_oracle"]["product_id"] = "foreign-product"
    elif wrong == "history":
        results["api_denial_oracle"]["history_count"] = 1
    if recovery:
        results["api_recovery_cleanup"] = {
            "product_id": "owned-product",
            "cleanup_verified": True,
            "shared_unchanged": True,
            "attempts": [{"state": "response_unknown"}],
        }
    sent = []

    class Ssm:
        def json_result(self, instance, _commands, *, purpose, timeout):
            sent.append((instance, purpose))
            return {"Status": "Success"}, {
                "success": True,
                "stage": "complete",
                "results": results,
                "cleanup": {
                    "run_id": config["product_run_id"],
                    "product_id": "owned-product",
                    "project_id": "owned-project",
                    "fixture_removed": True,
                },
            }

    runner = live._run_worker(Ssm(), cfg, lambda *_args: sent.append("install"))
    if wrong is not None:
        with pytest.raises(
            ports.PortError,
            match="Product process probe dispatch|Product driver dispatch|verified lifecycle and retirement",
        ):
            runner("i-owned", "product_v01", config, manifest=manifest)
    else:
        assert (
            runner("i-owned", "product_v01", config, manifest=manifest)["results"]
            == results
        )
    assert sent == (
        []
        if wrong in ("selection", "actor")
        else ["install", ("i-owned", "product_v01")]
    )


@pytest.mark.parametrize(
    "change",
    [
        "actor",
        "workspace",
        "proof",
        "pod",
        "workflow",
        "retirement",
        "instance",
        "intent_key",
        "actor_id",
        "intent_instance",
    ],
)
def test_protected_dispatch_refuses_mismatched_inputs_before_ssm(
    protected_dispatch, change
):
    from tests.e2e.cli_uplift import live, ports

    config, cfg, manifest, _fixture = protected_dispatch
    if change == "actor":
        config["product_actor_session_ref"] = config["session_ref"]
    elif change == "workspace":
        config["product_workspace_id"] = "foreign-workspace"
    elif change == "proof":
        config["product_probe"]["success"] = False
    elif change == "pod":
        config["product_probe"]["pods"][0]["uid"] = "other-pod"
    elif change == "workflow":
        config["workflow_run_id"] = "product-123456-3"
    elif change == "retirement":
        config["internal_invoke_url"] = "https://foreign.example.invalid"
    elif change == "intent_key":
        config["product_intent_key"] = "product-create-" + "f" * 64
    elif change == "actor_id":
        config["product_actor_id"] = "foreign-actor"
    elif change == "intent_instance":
        document = manifest.read()
        next(row for row in document["resources"] if row["kind"] == "product_fixture")[
            "detail"
        ]["instance_id"] = "i-foreign"
        manifest._write(document)
    else:
        config["instance_id"] = "i-foreign"
    sent = []
    runner = live._run_worker(object(), cfg, lambda *_args: sent.append("install"))
    with pytest.raises(
        ports.PortError, match="Product (process probe|driver) dispatch"
    ):
        runner("i-owned", "product_v01", config, manifest=manifest)
    assert sent == []


def test_protected_dispatch_rejects_false_retirement_receipt(protected_dispatch):
    from tests.e2e.cli_uplift import live, ports

    config, cfg, manifest, _fixture = protected_dispatch

    class Ssm:
        def json_result(self, *_args, **_kwargs):
            return {"Status": "Success"}, {
                "success": True,
                "stage": "complete",
                "results": {
                    "api_product_lifecycle": {
                        "product_id": "owned",
                        "project_id": "project",
                        "revision": 2,
                    }
                },
                "cleanup": {
                    "run_id": config["product_run_id"],
                    "product_id": "other",
                    "project_id": "project",
                    "fixture_removed": True,
                },
            }

    runner = live._run_worker(Ssm(), cfg, lambda *_args: None)
    with pytest.raises(ports.PortError, match="verified lifecycle and retirement"):
        runner("i-owned", "product_v01", config, manifest=manifest)


@pytest.fixture
def selected(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    run_id = "adp-e2e-20261019-000000-abcdef"
    config = {
        "work_dir": str(tmp_path),
        "evaluation_id": run_id,
        "product_run_id": "product-123456-2",
        "product_intent_key": "product-create-" + "b" * 64,
        "product_actor_id": "owned-actor",
        "product_actor_row_id": "owned-actor-row",
        "workflow_run_id": "product-123456-2",
        "product_cases": "api_product_lifecycle",
        "product_workspace_id": "owned-tenant",
        "expected_revision": "a" * 40,
        "other_tenant_id": "other-tenant",
    }
    fixture = FixtureAPI(tenant_id="owned-tenant")
    session = {
        "org_id": "owned-tenant",
        "user_id": "owned-actor",
        # ProductOperation.actor_id stores the workspace users row, not the
        # Cognito subject, so the driver carries both identifiers.
        "adp_user_id": "owned-actor-row",
        "access_token": "owner-token",
    }
    monkeypatch.setattr(
        product_v01, "local_owner", lambda _config: (session, fixture.client)
    )
    monkeypatch.setattr(
        product_v01.common, "load_session", lambda _config: {"org_id": "other-tenant"}
    )
    return config, fixture


def test_instance_driver_calls_checked_lifecycle_and_owned_retirement(
    selected, monkeypatch
):
    config, fixture = selected
    retired = []
    readbacks = []
    original_get = fixture.get

    def get(path):
        if retired:
            readbacks.append(path)
            return SimpleNamespace(status_code=404)
        return original_get(path)

    fixture.get = get

    def retire(payload, evidence):
        record = json.loads(
            (Path(payload["work_dir"]) / "product-ownership.json").read_text()
        )
        retired.append(record)
        evidence.update(
            success=True,
            detail={
                "run_id": record["run_id"],
                "product_id": record["product_id"],
                "project_id": record["project_id"],
                "fixture_removed": True,
            },
        )

    monkeypatch.setattr(product_v01.product_cleanup, "execute", retire)
    observed = []

    def snapshot():
        observed.append(config["expected_revision"])
        return config["expected_revision"]

    result = product_v01.run_selected(config, snapshot)
    assert len(observed) == 2
    assert fixture.posts == 2 and fixture.edits == fixture.reads == 1
    assert result["results"]["api_product_lifecycle"]["revision"] == 2
    assert result["cleanup"]["fixture_removed"] is True
    assert retired[0]["run_id"] == config["product_run_id"]
    assert retired[0]["actor_id"] == "owned-actor-row"
    assert retired[0]["idempotency_key"] == fixture.create_key
    path = f"/v1/products/{fixture.product_id}/projects/owned-project"
    assert readbacks == [path, path + "/changes", path + "/revisions/1"]


@pytest.mark.parametrize("stale", ["none", "missing_proof", "before", "after"])
def test_dispatcher_product_driver_requires_fresh_pod_proof_and_cleanup(
    selected, monkeypatch, stale
):
    config, fixture = selected
    proof = {
        "success": True,
        "source_revision": config["expected_revision"],
        "pods": [{"uid": "owned-pod", "schema_sha256": "b" * 64}],
    }
    config["product_inventory"] = {
        "source_revision": config["expected_revision"],
        "digest": "sha256:" + "c" * 64,
        "pods": [
            {"uid": "owned-pod", "address": "10.1.1.10", "digest": "sha256:" + "c" * 64}
        ],
    }
    config["product_probe"] = proof
    observations = []

    def probe(_config, evidence):
        observations.append("snapshot")
        current = dict(proof)
        if stale == "before" or (stale == "after" and len(observations) == 2):
            current["pods"] = [{"uid": "owned-pod", "schema_sha256": "d" * 64}]
        evidence.update(current)

    monkeypatch.setattr(product_v01.product_probe, "execute", probe)
    retired = []
    get = fixture.get

    def read(path):
        return SimpleNamespace(status_code=404) if retired else get(path)

    fixture.get = read

    def retire(payload, evidence):
        ownership = json.loads(
            (Path(payload["work_dir"]) / "product-ownership.json").read_text()
        )
        retired.append(ownership)
        evidence.update(
            success=True,
            detail={
                "run_id": ownership["run_id"],
                "product_id": ownership["product_id"],
                "project_id": ownership["project_id"],
                "fixture_removed": True,
            },
        )

    monkeypatch.setattr(product_v01.product_cleanup, "execute", retire)
    if stale == "missing_proof":
        config.pop("product_probe")
    evidence = {}
    if stale == "none":
        product_v01.execute(config, evidence)
        assert evidence["success"] is True
        assert evidence["cleanup"]["fixture_removed"] is True
        assert len(observations) == 2 and len(retired) == 1
        assert fixture.posts == 2
    else:
        with pytest.raises(common.RemoteError, match="process"):
            product_v01.execute(config, evidence)
        assert evidence == {}
        assert fixture.posts == (2 if stale == "after" else 0)
        assert len(retired) == (1 if stale == "after" else 0)


@pytest.mark.parametrize(
    "corruption",
    [
        "cases",
        "missing_product_run",
        "wrong_workflow_run",
        "wrong_evaluation_run",
        "workspace",
        "tenant",
        "source",
        "missing_retirement",
        "false_retirement",
        "visible_retired",
    ],
)
def test_instance_driver_refuses_unsafe_selection_or_cleanup(
    selected, monkeypatch, corruption
):
    config, fixture = selected
    retired = []
    if corruption == "cases":
        config["product_cases"] = "unreviewed_case"
    if corruption == "tenant":
        config["other_tenant_id"] = "owned-tenant"
    if corruption == "workspace":
        config["product_workspace_id"] = "foreign-tenant"
    if corruption == "missing_product_run":
        del config["product_run_id"]
    if corruption == "wrong_workflow_run":
        config["workflow_run_id"] = "product-123456-3"
    if corruption == "wrong_evaluation_run":
        config["evaluation_id"] = "product-123456-2"

    def snapshot():
        return "b" * 40 if corruption == "source" else config["expected_revision"]

    def retire(payload, evidence):
        if corruption == "missing_retirement":
            raise common.RemoteError("retirement refused")
        retired.append(True)
        record = json.loads(
            (Path(payload["work_dir"]) / "product-ownership.json").read_text()
        )
        evidence.update(
            success=True,
            detail={
                "run_id": record["run_id"],
                "product_id": "foreign-product"
                if corruption == "false_retirement"
                else record["product_id"],
                "project_id": record["project_id"],
                "fixture_removed": True,
            },
        )

    monkeypatch.setattr(product_v01.product_cleanup, "execute", retire)
    if corruption == "visible_retired":
        original_get = fixture.get
        fixture.get = lambda path: (
            SimpleNamespace(status_code=200) if retired else original_get(path)
        )
    with pytest.raises(
        (ValueError, common.RemoteError),
        match="selection|identity|tenant|workspace|release|retirement|cleanup|still visible",
    ):
        product_v01.run_selected(config, snapshot)
    assert fixture.posts == (
        0
        if corruption
        in {
            "cases",
            "missing_product_run",
            "wrong_workflow_run",
            "wrong_evaluation_run",
            "workspace",
            "tenant",
            "source",
        }
        else 2
    )


def test_unknown_create_response_retires_by_original_manifest_and_still_fails(
    selected, monkeypatch
):
    config, fixture = selected
    fixture.response_lost_once = True
    retired = []
    readbacks = []

    def get(path):
        readbacks.append(path)
        return SimpleNamespace(status_code=404)

    fixture.get = get

    def retire(payload, evidence):
        record = json.loads(
            (Path(payload["work_dir"]) / "product-ownership.json").read_text()
        )
        retired.append(record)
        evidence.update(
            success=True,
            detail={
                "run_id": record["run_id"],
                "product_id": fixture.product_id,
                "project_id": "owned-project",
                "fixture_removed": True,
            },
        )

    monkeypatch.setattr(product_v01.product_cleanup, "execute", retire)
    with pytest.raises(InterruptedWrite, match="response lost"):
        product_v01.run_selected(config, lambda: config["expected_revision"])
    assert fixture.posts == 1 and fixture.edits == 0
    assert len(retired) == 1
    assert retired[0]["idempotency_key"] == fixture.create_key
    assert retired[0]["document"]["attempts"][-1]["state"] == "response_unknown"
    assert "product_id" not in retired[0]
    assert len(readbacks) == 3


def test_protected_dispatch_refuses_missing_durable_create_intent(protected_dispatch):
    from tests.e2e.cli_uplift import cleanup, live, ports

    config, cfg, manifest, _fixture = protected_dispatch
    manifest.mark("product_fixture", config["product_run_id"], cleanup.DELETED)
    sent = []
    runner = live._run_worker(object(), cfg, lambda *_args: sent.append("install"))
    with pytest.raises(ports.PortError, match="Product driver dispatch"):
        runner("i-owned", "product_v01", config, manifest=manifest)
    assert sent == []


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "intent",
        "subject_actor",
        "sts",
        "key",
        "instance",
        "intent_instance",
        "status",
        "receipt",
    ],
)
def test_protected_recovery_dispatch_verifies_instance_and_retirement(
    protected_dispatch, failure
):
    from tests.e2e.cli_uplift import bundle, live, ports

    config, cfg, manifest, _fixture = protected_dispatch
    fixture = next(
        row for row in manifest.outstanding() if row["kind"] == "product_fixture"
    )
    payload = {
        "evaluation_id": config["evaluation_id"],
        "instance_id": config["instance_id"],
        "product_run_id": config["product_run_id"],
        "workflow_run_id": config["workflow_run_id"],
        "product_recovery_intent": {
            "run_id": config["product_run_id"],
            **{
                key: fixture["detail"][key]
                for key in (
                    "tenant_id",
                    "source_revision",
                    "idempotency_key",
                )
            },
            # The wire actor is the ADP `users` row the retirement resolves,
            # never the Cognito subject the detail also records.
            "actor_id": fixture["detail"]["actor_row_id"],
        },
        "expected_revision": config["expected_revision"],
        "internal_invoke_url": config["internal_invoke_url"],
        "sts_endpoint": cfg["sts_endpoint"],
        "work_dir": bundle.REMOTE_DIR,
        "platform_account": config["platform_account"],
        "region": config["region"],
    }
    if failure == "intent":
        payload["product_recovery_intent"]["actor_id"] = "unowned-actor"
    elif failure == "subject_actor":
        # The Cognito subject is a real identifier of this run's owner, and
        # still the wrong namespace for the retirement to resolve.
        payload["product_recovery_intent"]["actor_id"] = fixture["detail"]["actor_id"]
    elif failure == "sts":
        payload.pop("sts_endpoint")
    elif failure == "key":
        payload["product_recovery_intent"]["idempotency_key"] = (
            "product-create-" + "f" * 64
        )
    elif failure == "instance":
        payload["instance_id"] = "i-unowned"
    elif failure == "intent_instance":
        document = manifest.read()
        next(row for row in document["resources"] if row["kind"] == "product_fixture")[
            "detail"
        ]["instance_id"] = "i-unowned"
        manifest._write(document)
    sent = []

    class Ssm:
        def json_result(self, *_args, **_kwargs):
            sent.append("ssm")
            return (
                {"Status": "Failed" if failure == "status" else "Success"},
                {
                    "success": True,
                    "stage": "complete",
                    "detail": {
                        "run_id": config["product_run_id"],
                        "fixture_removed": failure != "receipt",
                        "product_id": "owned-product",
                        "project_id": "owned-project",
                    },
                },
            )

    run = live._run_worker(Ssm(), cfg, lambda *_args: sent.append("install"))
    if failure is not None:
        with pytest.raises(
            ports.PortError, match="Product cleanup dispatch|Protected Product"
        ):
            run("i-owned", "product_cleanup", payload, manifest=manifest)
        assert sent == (
            []
            if failure
            in (
                "intent",
                "subject_actor",
                "sts",
                "key",
                "instance",
                "intent_instance",
            )
            else ["install", "ssm"]
        )
    else:
        receipt = run("i-owned", "product_cleanup", payload, manifest=manifest)
        assert receipt["detail"]["fixture_removed"] is True
        assert sent == ["install", "ssm"]


def test_on_instance_denial_uses_real_revocation_hook_before_oracle(
    selected, monkeypatch
):
    config, fixture = selected
    config.update(
        product_cases="api_product_lifecycle,api_denial_oracle",
        gateway_url="https://gateway.example.invalid",
        credential_secret=f"adp/cli-uplift-eval/{config['evaluation_id']}-product-fixtures",
        product_team_id="owned-team",
        product_denial_actor={"user_id": "ordinary-user", "org_id": "owned-tenant"},
    )
    owner = {
        "org_id": "owned-tenant",
        "user_id": "owned-actor",
        "adp_user_id": "owned-actor-row",
        "access_token": "owner-token",
    }
    observed = []
    monkeypatch.setattr(
        product_v01,
        "local_owner",
        lambda _config, *, allow_foreign=False: (owner, fixture.client)
        if allow_foreign
        else pytest.fail("foreign actor was not enabled"),
    )
    monkeypatch.setattr(
        product_v01.common,
        "load_session",
        lambda _config: {"org_id": "other-tenant", "access_token": "foreign-token"},
    )
    monkeypatch.setattr(
        product_v01.product_denial_actor,
        "prepare",
        lambda _config, token, tenant, actor: (
            observed.append(("prepared", token, tenant, actor)) or "revoked-token",
            lambda: observed.append("revoked")
            or {"user_id": "ordinary-user", "membership_revoked": True},
        ),
    )

    def denial(client_factory, **fixtures):
        assert observed[-1] == "revoked"
        assert fixtures["foreign_token"] == "foreign-token"
        assert fixtures["revoked_token"] == "revoked-token"
        with client_factory("revoked-token") as revoked:
            assert revoked.token == "revoked-token"
        observed.append("denial")
        return {"product_id": fixtures["product_id"], "revision": 2}

    monkeypatch.setattr(product_v01.live, "api_denial_oracle", denial)
    original_get = fixture.get
    retired = []

    def get(path):
        if retired:
            return SimpleNamespace(status_code=404)
        return original_get(path)

    fixture.get = get

    def retire(payload, evidence):
        manifest = json.loads(
            (Path(payload["work_dir"]) / "product-ownership.json").read_text()
        )
        retired.append(manifest)
        evidence.update(
            success=True,
            detail={
                "run_id": manifest["run_id"],
                "product_id": manifest["product_id"],
                "project_id": manifest["project_id"],
                "fixture_removed": True,
            },
        )

    monkeypatch.setattr(product_v01.product_cleanup, "execute", retire)
    result = product_v01.run_selected(config, lambda: config["expected_revision"])
    assert observed == [
        ("prepared", "owner-token", "owned-tenant", "owned-actor"),
        "revoked",
        "denial",
    ]
    assert result["results"]["api_denial_oracle"]["revocation"] == {
        "user_id": "ordinary-user",
        "membership_revoked": True,
    }
    assert result["cleanup"]["fixture_removed"] is True


def test_instance_release_hold_retains_owned_history_and_original_idempotency(
    selected, monkeypatch
):
    config, fixture = selected
    config["hold_for_rollback"] = True
    retired = []
    monkeypatch.setattr(
        product_v01.product_cleanup, "execute", lambda *_: retired.append(True)
    )
    original_get = fixture.get

    def get(path):
        if path.endswith("/changes"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "items": [
                        {
                            "revision": 1,
                            "product_id": fixture.product_id,
                            "event_id": "first",
                        },
                        {
                            "revision": 2,
                            "product_id": fixture.product_id,
                            "event_id": "second",
                        },
                    ]
                },
            )
        return original_get(path)

    fixture.get = get
    result = product_v01.run_selected(config, lambda: config["expected_revision"])
    assert retired == []
    assert result["cleanup"] == {"fixture_removed": False}
    assert result["held"]["run_id"] == config["product_run_id"]
    assert result["held"]["idempotency_key"] == fixture.create_key
    assert result["held"]["history_ref"].startswith("sha256:")
    assert result["held"]["idempotency_receipt"].startswith("sha256:")
    assert result["held"]["product_id"] == fixture.product_id


def test_instance_release_hold_retires_if_history_is_unavailable(selected, monkeypatch):
    config, fixture = selected
    config["hold_for_rollback"] = True
    retired = []
    original_get = fixture.get

    def get(path):
        if retired:
            return SimpleNamespace(status_code=404)
        if path.endswith("/changes"):
            return SimpleNamespace(status_code=503)
        return original_get(path)

    fixture.get = get

    def retire(payload, evidence):
        retired.append(True)
        evidence.update(
            success=True,
            detail={
                "run_id": config["product_run_id"],
                "product_id": fixture.product_id,
                "project_id": "owned-project",
                "fixture_removed": True,
            },
        )

    monkeypatch.setattr(product_v01.product_cleanup, "execute", retire)
    with pytest.raises(common.RemoteError, match="original history"):
        product_v01.run_selected(config, lambda: config["expected_revision"])
    assert retired == [True]


def test_protected_dispatch_retains_only_its_own_held_product(
    protected_dispatch, monkeypatch
):
    from tests.e2e.cli_uplift import live, ports

    config, cfg, manifest, fixture = protected_dispatch
    config["hold_for_rollback"] = True
    cfg["hold_for_rollback"] = True
    original_get = fixture.get

    def get(path):
        if path.endswith("/changes"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "items": [
                        {
                            "revision": 1,
                            "product_id": fixture.product_id,
                            "event_id": "first",
                        },
                        {
                            "revision": 2,
                            "product_id": fixture.product_id,
                            "event_id": "second",
                        },
                    ]
                },
            )
        return original_get(path)

    fixture.get = get
    monkeypatch.setattr(
        product_v01.product_probe,
        "execute",
        lambda _config, receipt: receipt.update(config["product_probe"]),
    )
    monkeypatch.setattr(
        product_v01.product_cleanup,
        "execute",
        lambda *_: pytest.fail("retired held Product"),
    )

    class Ssm:
        def json_result(self, instance, _commands, *, purpose, timeout):
            receipt = {}
            product_v01.execute(config, receipt)
            return {"Status": "Success"}, receipt

    dispatched = []
    runner = live._run_worker(Ssm(), cfg, lambda *_: dispatched.append("install"))
    held = runner("i-owned", "product_v01", config, manifest=manifest)
    assert held["cleanup"] == {"fixture_removed": False}
    assert held["held"]["product_id"] == held["lifecycle"]["product_id"]
    assert dispatched == ["install"]
    config["hold_for_rollback"] = False
    with pytest.raises(ports.PortError, match="Product driver dispatch"):
        runner("i-owned", "product_v01", config, manifest=manifest)


def _install_auth_recording(tmp_path, resources):
    """Drive the real install/auth stage over a reported resource list.

    That stage is the producer of the bootstrap login's ownership records and
    the protected Product dispatch above is their only consumer, so the two
    have to agree on what such a record carries. The fixture above records the
    bootstrap row by hand, which cannot show that the producer does.
    """
    from tests.e2e.cli_uplift import cases, cleanup, stages

    cfg = {
        "platform_account": "000000000000",
        "region": "us-east-1",
        "cognito_user_pool_id": "pool",
    }
    manifest = cleanup.Manifest(
        tmp_path / "run.json",
        "adp-e2e-20260101-000000-abcdef",
        on_change=lambda *_args, **_kwargs: None,
    )
    ctx = {
        "document": {"instance_id": "i-owned"},
        "expected_hashes": {"install.sh": "a" * 64},
        "matrix": {"C01": {"status": cases.NOT_RUN}},
        "manifest": manifest,
        "transcript": [],
        "preflight": {},
        "record": lambda *_args, **_kwargs: None,
    }
    evidence = {
        "success": True,
        "session": {"org_id": "bootstrap-tenant", "user_id": "bootstrap-subject"},
        "resources": resources,
        "install": {"success": True, "hashes_match": True, "executable": True},
        "login": {"authenticated": True, "user_id": "bootstrap-subject"},
    }
    stage = stages.install_auth_stage(
        cfg,
        {
            "worker_config": lambda *_args: {},
            "run_worker": lambda *_args, **_kwargs: evidence,
        },
    )
    return cfg, manifest, stage, ctx


def test_bootstrap_adp_account_is_recorded_where_the_run_owns_it(tmp_path):
    """The run's own ADP row is ownership, so it must say where it lives.

    The protected Product driver dispatch counts a separate tenant only from
    ownership recorded in the account and region under evaluation. Recorded
    with no location at all, the row the bootstrap login registered for itself
    was attributable to nothing, and deploy run 39957554663 refused the run for
    having no verified second tenant after the probe had already proved every
    serving pod.
    """
    identity = "bootstrap-tenant/bootstrap-row"
    cfg, manifest, stage, ctx = _install_auth_recording(
        tmp_path, [["adp_user", identity]]
    )
    stage(ctx)
    assert ("adp_user", identity) in {
        (entry["kind"], entry["id"])
        for entry in manifest.outstanding()
        if entry.get("account") == cfg["platform_account"]
        and entry.get("region") == cfg["region"]
    }


def test_install_auth_never_attributes_a_two_account_kind_by_default(tmp_path):
    """A kind that can live in either account is refused, never defaulted."""
    from tests.e2e.cli_uplift import stages

    _cfg, manifest, stage, ctx = _install_auth_recording(
        tmp_path, [["iam_role", "adp-eval-bootstrap-role"]]
    )
    with pytest.raises(stages.StageError, match="either account"):
        stage(ctx)
    assert manifest.outstanding() == []
