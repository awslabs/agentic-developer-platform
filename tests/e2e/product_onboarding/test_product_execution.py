"""The Product journey uses the disposable evaluator's actual stage handoff."""

import hashlib

import pytest

from tests.e2e.cli_uplift import bundle, cleanup, ports
from tests.e2e.cli_uplift import live as evaluator
from tests.e2e.product_onboarding import product_execution

RUN_ID = "adp-e2e-20261020-113000-abcdef"
SOURCE = "a" * 40
WORKFLOW_RUN_ID = "product-123456-2"


@pytest.mark.parametrize("durable_intent_available", [True, False])
@pytest.mark.parametrize(
    "worker_results", [None, {}, {"api_denial_oracle": {"revision": 2}}]
)
@pytest.mark.parametrize(
    "probe_change",
    [
        None,
        "failed",
        "stale",
        "wrong_uid",
        "missing_pod",
        "bad_hash",
        "extra_pod",
        "non_mapping",
        "schema_disagreement",
        "late_drift",
        "late_unavailable",
    ],
)
def test_retained_evaluator_stage_dispatches_owned_product_sequence(
    tmp_path, monkeypatch, durable_intent_available, worker_results, probe_change
):
    workspace_id = (
        RUN_ID
        + "-product-"
        + hashlib.sha256((RUN_ID + ":product-workspace").encode()).hexdigest()[:24]
    )
    cfg = {
        "gateway_url": "https://gateway.example.invalid",
        "platform_account": "000000000000",
        "region": "us-east-1",
        "sts_endpoint": "https://sts-fips.us-east-1.amazonaws.com",
        "secrets_endpoint": "https://secretsmanager-fips.us-east-1.amazonaws.com",
        "expected_revision": SOURCE,
        "product_cases": "api_product_lifecycle",
        "cognito_user_pool_id": "owned-pool",
        "internal_invoke_url": "https://private.execute-api.us-east-1.amazonaws.com/dev",
    }
    inventory = {
        "source_revision": SOURCE,
        "digest": "sha256:" + "b" * 64,
        "pods": [
            {"uid": "owned-pod", "address": "10.0.1.10", "digest": "sha256:" + "b" * 64}
        ],
    }
    if probe_change == "schema_disagreement":
        inventory["pods"].append(
            {
                "uid": "second-pod",
                "address": "10.0.1.11",
                "digest": "sha256:" + "b" * 64,
            }
        )

    def reobserve(aws_port, selected_cfg, revision):
        assert selected_cfg == {**cfg, "product_run_id": WORKFLOW_RUN_ID}
        assert revision == SOURCE
        if probe_change == "late_unavailable":
            raise ports.PortError("Product serving inventory read failed")
        if probe_change == "late_drift":
            return {
                **inventory,
                "pods": [{**inventory["pods"][0], "uid": "replacement-pod"}],
            }
        return inventory

    monkeypatch.setattr(product_execution.product_inventory, "observe", reobserve)

    def persist(document, *, critical):
        if (
            not durable_intent_available
            and critical
            and any(
                entry["kind"] == "product_fixture" for entry in document["resources"]
            )
        ):
            raise RuntimeError("durable Product intent unavailable")

    manifest = cleanup.Manifest(tmp_path / "manifest.json", RUN_ID, on_change=persist)
    for kind, identity in (
        ("ec2_instance", "i-owned"),
        # The bootstrap identity's own ADP `users` row, recorded under the ROW
        # id and never the Cognito subject the session is attributed to.
        ("adp_user", "other-tenant/bootstrap-row"),
    ):
        manifest.record(
            kind, identity, account=cfg["platform_account"], region=cfg["region"]
        )
    received = []

    def worker(instance_id, purpose, payload, *, manifest):
        received.append((instance_id, purpose, payload))
        assert manifest.on_change is not None
        assert payload["evaluation_id"] == RUN_ID
        if purpose == "product_workspace":
            manifest.record(
                "adp_org",
                workspace_id,
                account=cfg["platform_account"],
                region=cfg["region"],
            )
            return {"workspace_id": workspace_id, "team_id": "owned-team"}
        if purpose == "product_actor":
            assert payload["product_team_id"] == "owned-team"
            return {
                # The owner publishes BOTH of its identifiers: the Cognito
                # subject its session is attributed to and the ADP `users` row
                # it registered. They are different values live, and a fixture
                # that reused one string for both could not see the ownership
                # defect that failed adp-e2e-20261022-122136-174d4b.
                "session": {
                    "user_id": "owned-actor",
                    "adp_user_id": "owned-row",
                    "session_ref": bundle.REMOTE_DIR + "/product-actor/session.json",
                }
            }
        if purpose == "product_probe":
            assert payload["product_inventory"] == inventory
            proof = {
                "success": True,
                "source_revision": SOURCE,
                "pods": [{"uid": "owned-pod", "schema_sha256": "c" * 64}],
            }
            if probe_change == "non_mapping":
                return None
            if probe_change == "schema_disagreement":
                proof["pods"].append({"uid": "second-pod", "schema_sha256": "d" * 64})
            if probe_change == "failed":
                proof["success"] = False
            elif probe_change == "stale":
                proof["source_revision"] = "d" * 40
            elif probe_change == "wrong_uid":
                proof["pods"][0]["uid"] = "different-pod"
            elif probe_change == "missing_pod":
                proof["pods"] = []
            elif probe_change == "bad_hash":
                proof["pods"][0]["schema_sha256"] = "not-a-schema-hash"
            elif probe_change == "extra_pod":
                proof["pods"].append({"uid": "another-pod", "schema_sha256": "c" * 64})
            return proof
        assert purpose == "product_v01"
        assert payload["product_probe"]["success"] is True
        assert payload["internal_invoke_url"] == cfg["internal_invoke_url"]
        assert payload["other_tenant_id"] == "other-tenant"
        assert payload["product_run_id"] == WORKFLOW_RUN_ID
        assert payload["product_actor_id"] == "owned-actor"
        # The driver needs both namespaces: the subject it authenticates as and
        # the ADP row that the durable create intent is attributed to.
        assert payload["product_actor_row_id"] == "owned-row"
        assert payload["product_intent_key"].startswith("product-create-")
        assert any(
            row["kind"] == "product_fixture"
            and row["id"] == WORKFLOW_RUN_ID
            and row["detail"]["idempotency_key"] == payload["product_intent_key"]
            and row["detail"]["instance_id"] == "i-owned"
            # Both namespaces are durable before the fixture exists: the subject
            # proves a later retirement intent is the original actor's, the row
            # id names the account that still has to be deleted.
            and row["detail"]["actor_id"] == "owned-actor"
            and row["detail"]["actor_row_id"] == "owned-row"
            for row in manifest.outstanding()
        )
        assert any(
            row["kind"] == "adp_user" and row["id"] == workspace_id + "/owned-row"
            for row in manifest.outstanding()
        )
        # The subject is not a row id: recorded here it would resolve to no user
        # and the sweep would report deleting an account it never removed.
        assert not any(
            row["kind"] == "adp_user" and row["id"].endswith("/owned-actor")
            for row in manifest.outstanding()
        )
        return {
            "results": worker_results
            if worker_results is not None
            else {"api_product_lifecycle": {"revision": 2}},
            "cleanup": {"fixture_removed": True},
        }

    def fixture(_aws, _cfg, *, product_workspace_id=None):
        if product_workspace_id is None:
            return lambda _ctx: None
        assert product_workspace_id == workspace_id

        def create(ctx):
            assert ctx["product_workspace_evidence"]["team_id"] == "owned-team"
            return {
                "credential_secret": "owned-fixture",
                "created_username": "owned-actor",
            }

        return create

    monkeypatch.setattr(evaluator, "_admin_fixtures", fixture)
    mapping = product_execution.build_stages(
        cfg,
        inventory,
        WORKFLOW_RUN_ID,
        supplied={"run_worker": worker, "aws": object()},
    )
    context = {
        "evaluation_id": RUN_ID,
        "document": {
            "instance_id": "i-owned",
            "session": {
                # Subject versus registered ADP row, as `install_auth` publishes
                # them: the Product stage must carry both forward.
                "user_id": "bootstrap-admin",
                "adp_user_id": "bootstrap-row",
                "org_id": "other-tenant",
                "session_ref": bundle.REMOTE_DIR + "/session.json",
            },
        },
        "manifest": manifest,
        "expected_hashes": {"install.sh": "d" * 64},
    }
    if probe_change:
        with pytest.raises(
            ports.PortError,
            match=(
                "serving inventory changed|serving inventory read failed"
                if probe_change in ("late_drift", "late_unavailable")
                else "process probe does not match"
            ),
        ):
            mapping["journeys"](context)
        assert [call[1] for call in received] == [
            "product_workspace",
            "product_actor",
            "product_probe",
        ]
        assert not any(
            entry["kind"] == "product_fixture" for entry in manifest.outstanding()
        )
        assert "product_evidence" not in context["document"]
        return
    if not durable_intent_available:
        with pytest.raises(RuntimeError, match="durable Product intent unavailable"):
            mapping["journeys"](context)
        assert [call[1] for call in received] == [
            "product_workspace",
            "product_actor",
            "product_probe",
        ]
        assert "product_evidence" not in context["document"]
        return
    if worker_results is not None:
        with pytest.raises(Exception, match="did not execute every selected API case"):
            mapping["journeys"](context)
        assert "product_evidence" not in context["document"]
        assert any(row["kind"] == "product_fixture" for row in manifest.outstanding())
        return
    mapping["journeys"](context)
    assert [call[1] for call in received] == [
        "product_workspace",
        "product_actor",
        "product_probe",
        "product_v01",
    ]
    assert context["document"]["product_evidence"]["cleanup"]["fixture_removed"] is True
    assert any(
        row["kind"] == "product_fixture" and row["status"] == "deleted"
        for row in manifest.read()["resources"]
    )


def test_denial_user_intent_is_durable_before_on_instance_registration(
    monkeypatch, tmp_path
):
    cfg = {
        "gateway_url": "https://gateway.example.invalid",
        "platform_account": "000000000000",
        "region": "us-east-1",
        "sts_endpoint": "https://sts-fips.us-east-1.amazonaws.com",
        "secrets_endpoint": "https://secretsmanager-fips.us-east-1.amazonaws.com",
        "expected_revision": SOURCE,
        "product_cases": "api_product_lifecycle,api_denial_oracle",
        "cognito_user_pool_id": "us-east-1_example",
        "internal_invoke_url": "https://private.execute-api.us-east-1.amazonaws.com/dev",
    }
    inventory = {"source_revision": SOURCE}
    saved = []
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        RUN_ID,
        on_change=lambda document, *, critical: saved.append(
            (critical, document["resources"][-1]["kind"])
        ),
    )
    manifest.record("ec2_instance", "i-owned")

    def worker(_instance_id, purpose, payload, *, manifest):
        if purpose == "product_workspace":
            manifest.record("adp_org", payload["product_workspace_id"])
            return {
                "workspace_id": payload["product_workspace_id"],
                "team_id": "owned-team",
            }
        assert purpose == "product_actor"
        assert ("adp_user_email", RUN_ID + "-product-user@adp-eval.example") in [
            (entry["kind"], entry["id"].split("/")[-1])
            for entry in manifest.outstanding()
        ]
        assert saved[-1] == (True, "adp_user_email")
        raise ports.PortError("interrupted before actor registration")

    monkeypatch.setattr(
        evaluator,
        "_admin_fixtures",
        lambda *_args, **_kwargs: lambda _ctx: {
            "non_admin_username": RUN_ID + "-product-user@adp-eval.example"
        },
    )
    mapping = product_execution.build_stages(
        cfg,
        inventory,
        WORKFLOW_RUN_ID,
        supplied={"run_worker": worker, "aws": object()},
    )
    ctx = {
        "evaluation_id": RUN_ID,
        "document": {
            "instance_id": "i-owned",
            "session": {
                "user_id": "bootstrap-admin",
                "adp_user_id": "bootstrap-row",
                "org_id": "other-tenant",
                "session_ref": bundle.REMOTE_DIR + "/session.json",
            },
        },
        "manifest": manifest,
        "expected_hashes": {"install.sh": "d" * 64},
    }
    with pytest.raises(ports.PortError, match="interrupted before actor registration"):
        mapping["journeys"](ctx)
    assert "product_evidence" not in ctx["document"]


@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.parametrize(
    "tamper",
    [
        None,
        "workspace",
        "actor",
        "instance",
        "revision",
        "key",
        "account",
        # The two identifier namespaces an independent retirement must keep
        # apart: a row id that owns no recorded account, the Cognito subject
        # passed off as a row, and a fixture that never durably recorded one.
        "foreign_row",
        "subject_as_row",
        "missing_row",
    ],
)
def test_retained_evaluator_retries_only_durably_owned_product_cleanup(
    tmp_path, monkeypatch, verified, tamper
):
    events = []
    cfg = {
        "region": "us-east-1",
        "platform_account": "000000000000",
        "gateway_url": "https://gateway.example.invalid",
        "expected_revision": SOURCE,
        "internal_invoke_url": "https://private.execute-api.us-east-1.amazonaws.com/dev",
        # remote/product_cleanup.py pins the STS endpoint from the dispatched
        # payload before it can sign the retirement call.
        "sts_endpoint": "https://sts-fips.us-east-1.amazonaws.com",
    }
    inventory = {"source_revision": SOURCE}
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json", RUN_ID, on_change=lambda *_args, **_kwargs: True
    )
    instance_id = "i-00000001"
    workspace_id = (
        RUN_ID
        + "-product-"
        + hashlib.sha256((RUN_ID + ":product-workspace").encode()).hexdigest()[:24]
    )
    intent_key = (
        "product-create-"
        + hashlib.sha256((WORKFLOW_RUN_ID + ":" + RUN_ID).encode()).hexdigest()
    )
    manifest.record(
        "ec2_instance",
        instance_id,
        account=cfg["platform_account"],
        region=cfg["region"],
    )
    if tamper != "actor":
        # Owned under the ADP `users` ROW id, which is what the sweep's deleter
        # resolves; the subject below is only the session attribution.
        manifest.record("adp_user", workspace_id + "/owned-row")
    manifest.record("github_app", "shared", reused=True)
    detail = {
        "tenant_id": "foreign-workspace" if tamper == "workspace" else workspace_id,
        "actor_id": "owned-actor",
        "actor_row_id": "owned-row",
        "instance_id": "i-00000002" if tamper == "instance" else instance_id,
        "source_revision": "b" * 40 if tamper == "revision" else SOURCE,
        "idempotency_key": "product-create-" + "b" * 64
        if tamper == "key"
        else intent_key,
    }
    if tamper == "foreign_row":
        detail["actor_row_id"] = "foreign-row"
    elif tamper == "subject_as_row":
        detail["actor_row_id"] = detail["actor_id"]
    elif tamper == "missing_row":
        del detail["actor_row_id"]
    manifest.record(
        "product_fixture",
        WORKFLOW_RUN_ID,
        detail=detail,
        account="000000000001" if tamper == "account" else cfg["platform_account"],
        region=cfg["region"],
    )

    def worker(instance, purpose, payload, *, manifest):
        events.append(purpose)
        assert instance == instance_id and purpose == "product_cleanup"
        assert payload["product_recovery_intent"]["idempotency_key"] == intent_key
        # ProductOperation.actor_id is the workspace users row, so the wire
        # intent carries the row id and never the Cognito subject.
        assert payload["product_recovery_intent"]["actor_id"] == "owned-row"
        assert payload["sts_endpoint"] == cfg["sts_endpoint"]
        assert manifest.outstanding()[0]["kind"] == "ec2_instance"
        return {
            "success": True,
            "stage": "complete",
            "detail": {
                "run_id": WORKFLOW_RUN_ID,
                "fixture_removed": verified,
                "product_id": "owned-product",
                "project_id": "owned-project",
            },
        }

    monkeypatch.setattr(
        product_execution.evaluator, "wire", lambda _cfg, supplied: supplied
    )
    monkeypatch.setattr(
        product_execution.stages,
        "build_stages",
        lambda _cfg, _ports: {
            "cleanup": lambda ctx: events.append("ordinary_cleanup") or True
        },
    )
    mapping = product_execution.build_stages(
        cfg,
        inventory,
        WORKFLOW_RUN_ID,
        supplied={"run_worker": worker},
    )
    context = {
        "evaluation_id": RUN_ID,
        "manifest": manifest,
        "document": {"instance_id": instance_id},
    }
    if not verified or tamper:
        with pytest.raises(Exception, match="Product retirement is still outstanding"):
            mapping["cleanup"](context)
        assert any(row["kind"] == "product_fixture" for row in manifest.outstanding())
    else:
        assert mapping["cleanup"](context) is True
        assert not any(
            row["kind"] == "product_fixture" for row in manifest.outstanding()
        )
    assert events == (
        ["ordinary_cleanup"] if tamper else ["product_cleanup", "ordinary_cleanup"]
    )
    assert any(
        row["kind"] == "github_app" and row["status"] == "reused"
        for row in manifest.read()["resources"]
    )


@pytest.mark.parametrize(
    "wrong",
    [
        None,
        "missing",
        "tenant",
        "username",
        "subject",
        "owner",
        "intent",
        "revocation",
        "product",
    ],
)
def test_retained_denial_actor_receipt_binds_protected_driver_or_refuses(
    tmp_path, monkeypatch, wrong
):
    workspace_id = (
        RUN_ID
        + "-product-"
        + hashlib.sha256((RUN_ID + ":product-workspace").encode()).hexdigest()[:24]
    )
    ordinary_username = RUN_ID + "-product-user@adp-eval.example"
    cfg = {
        "gateway_url": "https://gateway.example.invalid",
        "platform_account": "000000000000",
        "region": "us-east-1",
        "sts_endpoint": "https://sts-fips.us-east-1.amazonaws.com",
        "secrets_endpoint": "https://secretsmanager-fips.us-east-1.amazonaws.com",
        "expected_revision": SOURCE,
        "product_cases": "api_product_lifecycle,api_denial_oracle",
        "cognito_user_pool_id": "owned-pool",
        "internal_invoke_url": "https://private.execute-api.us-east-1.amazonaws.com/dev",
    }
    inventory = {
        "source_revision": SOURCE,
        "pods": [{"uid": "pod"}],
    }
    monkeypatch.setattr(
        product_execution.product_inventory, "observe", lambda *_: inventory
    )
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json", RUN_ID, on_change=lambda *_args, **_kwargs: None
    )
    manifest.record(
        "ec2_instance", "i-owned", account=cfg["platform_account"], region=cfg["region"]
    )
    calls = []
    denial_actor = {
        "org_id": workspace_id,
        "username": ordinary_username,
        "user_id": "ordinary-user",
        "cognito_sub_bound": True,
    }
    if wrong == "tenant":
        denial_actor["org_id"] = "other-tenant"
    elif wrong == "username":
        denial_actor["username"] = "shared-user@adp-eval.example"
    elif wrong == "subject":
        denial_actor["cognito_sub_bound"] = False
    elif wrong == "owner":
        denial_actor["user_id"] = "owner-user"

    def worker(_instance_id, purpose, payload, *, manifest):
        calls.append(purpose)
        if purpose == "product_workspace":
            manifest.record(
                "adp_org",
                workspace_id,
                account=cfg["platform_account"],
                region=cfg["region"],
            )
            return {"workspace_id": workspace_id, "team_id": "owned-team"}
        if purpose == "product_actor":
            if wrong == "intent":
                manifest.mark(
                    "adp_user_email",
                    workspace_id + "/" + ordinary_username,
                    cleanup.DELETED,
                )
            return {
                "session": {
                    # The owner's subject and its registered ADP row, which the
                    # "owner" tamper below collides with in either namespace.
                    "user_id": "owner-user",
                    "adp_user_id": "owner-row",
                    "session_ref": bundle.REMOTE_DIR + "/product-actor/session.json",
                },
                **({} if wrong == "missing" else {"denial_actor": denial_actor}),
            }
        if purpose == "product_probe":
            return {
                "success": True,
                "source_revision": SOURCE,
                "pods": [{"uid": "pod", "schema_sha256": "a" * 64}],
            }
        assert purpose == "product_v01"
        assert payload["product_denial_actor"] == denial_actor
        assert any(
            entry["kind"] == "adp_user_email" for entry in manifest.outstanding()
        )
        return {
            "results": {
                "api_product_lifecycle": {"revision": 2, "product_id": "owned-product"},
                "api_denial_oracle": {
                    "product_id": "wrong-product"
                    if wrong == "product"
                    else "owned-product",
                    "revision": 2,
                    "history_count": 2,
                    "revocation": {
                        "user_id": "wrong-user"
                        if wrong == "revocation"
                        else "ordinary-user",
                        "membership_revoked": True,
                    },
                },
            },
            "cleanup": {"fixture_removed": True},
        }

    monkeypatch.setattr(
        evaluator,
        "_admin_fixtures",
        lambda *_args, **_kwargs: lambda _ctx: {
            "non_admin_username": ordinary_username,
            "credential_secret": f"adp/cli-uplift-eval/{RUN_ID}-product-fixtures",
        },
    )
    mapping = product_execution.build_stages(
        cfg,
        inventory,
        WORKFLOW_RUN_ID,
        supplied={"run_worker": worker, "aws": object()},
    )
    context = {
        "evaluation_id": RUN_ID,
        "document": {
            "instance_id": "i-owned",
            "session": {
                "user_id": "bootstrap-admin",
                "adp_user_id": "bootstrap-row",
                "org_id": "other-tenant",
                "session_ref": bundle.REMOTE_DIR + "/session.json",
            },
        },
        "manifest": manifest,
        "expected_hashes": {"install.sh": "a" * 64},
    }
    if wrong:
        with pytest.raises(
            ports.PortError, match="Product denial actor|every selected API case"
        ):
            mapping["journeys"](context)
        assert calls == (
            ["product_workspace", "product_actor", "product_probe", "product_v01"]
            if wrong in ("revocation", "product")
            else ["product_workspace", "product_actor"]
        )
        assert "product_evidence" not in context["document"]
    else:
        mapping["journeys"](context)
        assert calls == [
            "product_workspace",
            "product_actor",
            "product_probe",
            "product_v01",
        ]
        assert set(context["document"]["product_evidence"]["results"]) == {
            "api_product_lifecycle",
            "api_denial_oracle",
        }


@pytest.mark.parametrize(
    "wrong",
    [
        "missing_tick_digest",
        "wrong_tick_digest",
        "divergent_heads",
        "missing_head",
        "wrong_image_head",
        "wrong_pod",
        "missing_observation",
        "stale_revision",
        "missing_tick",
        "wrong_inventory_digest",
    ],
)
def test_native_proof_refuses_incomplete_or_unmatched_release_before_writes(
    monkeypatch, wrong
):
    inventory = {
        "digest": "sha256:" + "b" * 64,
        "source_revision": SOURCE,
        "pods": [{"uid": "pod-one"}, {"uid": "pod-two"}],
    }
    native = {
        "source_revision": SOURCE,
        "database_heads": ["head", "head"],
        "image_heads": ["head", "head"],
        "tick_digest": inventory["digest"],
        "tick_verified": True,
        "pod_uids": ["pod-one", "pod-two"],
        "observation_ref": "private-runtime-observation",
    }
    if wrong == "missing_tick_digest":
        native.pop("tick_digest")
    elif wrong == "wrong_tick_digest":
        native["tick_digest"] = "sha256:" + "c" * 64
    elif wrong == "divergent_heads":
        native["database_heads"] = native["image_heads"] = ["head", "other"]
    elif wrong == "missing_head":
        native["database_heads"] = native["image_heads"] = ["head", ""]
    elif wrong == "wrong_image_head":
        native["image_heads"] = ["head", "other"]
    elif wrong == "wrong_pod":
        native["pod_uids"] = ["pod-one", "other-pod"]
    elif wrong == "missing_observation":
        native["observation_ref"] = ""
    elif wrong == "stale_revision":
        native["source_revision"] = "c" * 40
    elif wrong == "missing_tick":
        native["tick_verified"] = False
    else:
        inventory["digest"] = "unbound"
    monkeypatch.setattr(product_execution, "recovery_available", lambda: True)
    monkeypatch.setattr(product_execution, "native_observation", lambda *_: native)
    monkeypatch.setattr(
        product_execution.statestore,
        "default_store",
        lambda *_: pytest.fail("Product create state was bound"),
    )
    with pytest.raises(ports.PortError, match="native migration and tick proof"):
        product_execution.evaluate(
            {"expected_revision": SOURCE}, inventory, WORKFLOW_RUN_ID, object()
        )


@pytest.mark.parametrize(
    "drift", ["revision", "digest", "pod_uid", "address", "missing_pod", "unavailable"]
)
def test_inventory_is_reobserved_after_native_proof_before_any_product_intent(
    monkeypatch, drift
):
    from copy import deepcopy

    from tests.e2e.product_onboarding import product_inventory

    inventory = {
        "source_revision": SOURCE,
        "digest": "sha256:" + "b" * 64,
        "pods": [
            {"uid": "pod-one", "address": "10.1.1.10", "digest": "sha256:" + "b" * 64},
            {"uid": "pod-two", "address": "10.1.1.11", "digest": "sha256:" + "b" * 64},
        ],
    }
    native = {
        "source_revision": SOURCE,
        "database_heads": ["head", "head"],
        "image_heads": ["head", "head"],
        "tick_verified": True,
        "tick_digest": inventory["digest"],
        "pod_uids": ["pod-one", "pod-two"],
        "observation_ref": "private-observation",
    }
    observed = deepcopy(inventory)
    if drift == "revision":
        observed["source_revision"] = "c" * 40
    elif drift == "digest":
        observed["digest"] = "sha256:" + "c" * 64
    elif drift == "pod_uid":
        observed["pods"][1]["uid"] = "replacement-pod"
    elif drift == "address":
        observed["pods"][0]["address"] = "10.1.1.12"
    elif drift == "missing_pod":
        observed["pods"].pop()
    monkeypatch.setattr(product_execution, "recovery_available", lambda: True)
    monkeypatch.setattr(product_execution, "native_observation", lambda *_: native)
    calls = []

    def reobserve(_aws, _cfg, revision):
        calls.append(revision)
        if drift == "unavailable":
            raise ValueError("Serving pod inventory cannot be observed")
        return observed

    monkeypatch.setattr(product_inventory, "observe", reobserve)
    monkeypatch.setattr(
        product_execution.statestore,
        "default_store",
        lambda *_: pytest.fail("Durable Product create intent was reached"),
    )
    with pytest.raises((ValueError, ports.PortError), match="inventory|Inventory"):
        product_execution.evaluate(
            {"expected_revision": SOURCE}, inventory, WORKFLOW_RUN_ID, object()
        )
    assert calls == [SOURCE]
