"""Drive the actual Gateway Product entrypoint through a controlled evaluator run."""

import copy
import json

import pytest

from tests.e2e.cli_uplift import bundle, cases, cleanup, config, ports, stages
from tests.e2e.cli_uplift import live as evaluator
from tests.e2e.product_onboarding import live, product_execution, product_inventory

SOURCE = "a" * 40
RUN_ID = "product-123456-2"


@pytest.mark.parametrize("denial", [False, True])
@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize(
    "interrupted,lost_instance", [(False, False), (True, False), (True, True)]
)
def test_product_entrypoint_runs_retained_evaluator_and_publishes_clean_boundary(
    monkeypatch, tmp_path, interrupted, lost_instance, denial, recovery
):
    cfg = config.load(config.EXAMPLE_PATH)
    cfg.update(
        expected_revision=SOURCE,
        gateway_deployment="reviewed-dev",
        gateway_url="https://gateway.example.invalid",
        instance_profile="owned-instance-profile",
        state_bucket="private-state-bucket",
        credential_secret_name="owned-fixture-name",
        internal_invoke_url="https://private.execute-api.us-east-1.amazonaws.com/dev",
    )
    inventory = {
        "source_revision": SOURCE,
        "digest": "sha256:" + "b" * 64,
        "pods": [
            {"uid": "owned-pod", "address": "10.0.1.10", "digest": "sha256:" + "b" * 64}
        ],
    }
    native = {
        "source_revision": SOURCE,
        "database_heads": ["native-head"],
        "image_heads": ["native-head"],
        "tick_verified": True,
        "tick_digest": inventory["digest"],
        "pod_uids": ["owned-pod"],
        "observation_ref": "controlled-native-proof",
    }
    received = []
    terminated = []
    persisted_intents = []
    selection = (
        "api_product_lifecycle,api_denial_oracle" if denial else "api_product_lifecycle"
    ) + (",api_recovery_cleanup" if recovery else "")

    def worker(_instance, purpose, payload, *, manifest):
        received.append(purpose)
        if purpose == "product_workspace":
            manifest.record(
                "adp_org",
                payload["product_workspace_id"],
                account=cfg["platform_account"],
                region=cfg["region"],
            )
            return {
                "workspace_id": payload["product_workspace_id"],
                "team_id": "owned-team",
            }
        if purpose == "product_actor":
            if denial:
                assert payload["non_admin_username"] == (
                    payload["evaluation_id"] + "-product-user@adp-eval.example"
                )
                assert any(
                    entry["kind"] == "adp_user_email"
                    and entry["id"]
                    == payload["product_workspace_id"]
                    + "/"
                    + payload["non_admin_username"]
                    for entry in persisted_intents
                )
            return {
                "session": {
                    "user_id": "owned-actor",
                    "adp_user_id": "owned-actor-row",
                    "session_ref": bundle.REMOTE_DIR + "/product-actor/session.json",
                },
                **(
                    {
                        "denial_actor": {
                            "org_id": payload["product_workspace_id"],
                            "username": payload["non_admin_username"],
                            "user_id": "ordinary-user",
                            "cognito_sub_bound": True,
                        }
                    }
                    if denial
                    else {}
                ),
            }
        if purpose == "product_probe":
            assert payload["product_inventory"] == inventory
            assert payload["product_cases"] == selection
            return {
                "success": True,
                "source_revision": SOURCE,
                "pods": [{"uid": "owned-pod", "schema_sha256": "c" * 64}],
            }
        if purpose == "product_cleanup":
            assert payload["product_recovery_intent"]["run_id"] == RUN_ID
            if interrupted and received.count("product_cleanup") == 1:
                return {"success": False}
            if lost_instance:
                raise ports.PortError("original evaluator instance is unavailable")
            return {
                "success": True,
                "stage": "complete",
                "detail": {
                    "run_id": RUN_ID,
                    "fixture_removed": True,
                    "product_id": "owned-product",
                    "project_id": "owned-project",
                },
            }
        assert purpose == "product_v01"
        assert payload["product_run_id"] == RUN_ID
        assert payload["product_cases"] == selection
        if denial:
            assert payload["product_denial_actor"]["user_id"] == "ordinary-user"
        assert any(
            entry["kind"] == "product_fixture"
            and entry["status"] == "pending"
            and entry["id"] == RUN_ID
            and entry["detail"]["idempotency_key"] == payload["product_intent_key"]
            for entry in persisted_intents
        )
        assert payload["product_probe"]["success"] is True
        assert payload["internal_invoke_url"] == cfg["internal_invoke_url"]
        if interrupted:
            raise ports.PortError("response lost after Product create")
        results = {
            "api_product_lifecycle": {"revision": 2, "product_id": "owned-product"}
        }
        if denial:
            results["api_denial_oracle"] = {
                "product_id": "owned-product",
                "revision": 2,
                "history_count": 2,
                "revocation": {"user_id": "ordinary-user", "membership_revoked": True},
            }
        if recovery:
            results["api_recovery_cleanup"] = {
                "product_id": "owned-product",
                "cleanup_verified": True,
                "shared_unchanged": True,
                "attempts": [{"state": "response_unknown"}],
            }
        return {
            "results": results,
            "cleanup": {"fixture_removed": True, "product_id": "owned-product"},
        }

    def prepare_actor(_aws, _cfg, *, product_workspace_id=None):
        if product_workspace_id is None:
            return lambda _ctx: None

        def created(ctx):
            assert (
                ctx["product_workspace_evidence"]["workspace_id"]
                == product_workspace_id
            )
            return {
                "credential_secret": "owned-fixture-name",
                "created_username": "owned-actor",
                **(
                    {
                        "non_admin_username": ctx["evaluation_id"]
                        + "-product-user@adp-eval.example"
                    }
                    if denial
                    else {}
                ),
            }

        return created

    def preflight(_cfg, _ports):
        def run(ctx):
            ctx["expected_hashes"] = {"install.sh": "d" * 64}
            ctx["preflight"]["deployed_revision"] = SOURCE

        return run

    def provision(_cfg, _ports):
        def run(ctx):
            ctx["manifest"].record(
                "ec2_instance",
                "i-00000001",
                account=cfg["platform_account"],
                region=cfg["region"],
            )
            ctx["document"]["instance_id"] = "i-00000001"

        return run

    def login(_cfg, _ports):
        def run(ctx):
            ctx["manifest"].record(
                "adp_user",
                "other-tenant/bootstrap-row",
                account=cfg["platform_account"],
                region=cfg["region"],
            )
            ctx["document"]["session"] = {
                "user_id": "bootstrap-admin",
                "adp_user_id": "bootstrap-row",
                "org_id": "other-tenant",
                "session_ref": bundle.REMOTE_DIR + "/session.json",
            }
            for case_id in ("E01", "C01", "E02"):
                ctx["record"](case_id, cases.PASSED)

        return run

    def retire(_cfg, _ports):
        def run(ctx):
            expected = {
                (entry["kind"], entry["id"]) for entry in ctx["manifest"].outstanding()
            }
            ok, results = cleanup.sweep(
                ctx["manifest"],
                {
                    kind: lambda _identifier, **_location: None
                    for kind in (
                        "ec2_instance",
                        "adp_org",
                        "adp_user",
                        "adp_user_email",
                    )
                },
            )
            assert {(result["kind"], result["id"]) for result in results} == expected
            return ok

        return run

    class Store:
        def __init__(self):
            self.documents = {}
            self.binding = None
            self.private = {}

        def _put(self, key, value):
            self.private[key] = copy.deepcopy(value)

        def _get(self, key):
            return copy.deepcopy(self.private.get(key))

        def claim(self, *_args):
            return True

        def release(self, *_args):
            return True

        def bind_product_run(self, run_id, binding):
            assert run_id == RUN_ID and self.binding is None
            self.binding = binding

        def product_run(self, run_id):
            assert run_id == RUN_ID
            return self.binding

        def load(self, evaluation_id):
            return copy.deepcopy(self.documents[evaluation_id])

        def save(self, evaluation_id, document, manifest=None):
            previous = self.documents.get(evaluation_id, (None, None))
            self.documents[evaluation_id] = copy.deepcopy(
                (document, manifest if manifest is not None else previous[1])
            )
            if manifest is not None:
                persisted_intents[:] = [dict(entry) for entry in manifest["resources"]]
            return True

    store = Store()
    monkeypatch.setattr(
        product_execution.product_sessions, "retain", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        product_execution.product_sessions,
        "restore",
        lambda _cfg, ctx, _store: {
            **ctx["document"]["session"],
            "prefix": ctx["evaluation_id"],
            "access_token": "private-bootstrap",
        },
    )
    monkeypatch.setattr(
        evaluator,
        "_deleters",
        lambda *_args: lambda _cfg, _ctx: {
            "ec2_instance": lambda instance, **_kwargs: terminated.append(instance),
        },
    )

    def independent_retire(_cfg, _aws, intent):
        assert terminated == ["i-00000001"]
        assert intent["run_id"] == RUN_ID
        received.append("independent_cleanup")
        return {
            "success": True,
            "stage": "complete",
            "detail": {
                "run_id": RUN_ID,
                "fixture_removed": True,
                "product_id": "owned-product",
                "project_id": "owned-project",
            },
        }

    monkeypatch.setattr(product_execution, "retire_independently", independent_retire)
    monkeypatch.setattr(product_execution, "native_observation", lambda *_: native)
    monkeypatch.setattr(product_execution.statestore, "default_store", lambda *_: store)
    monkeypatch.setattr(evaluator, "_admin_fixtures", prepare_actor)
    monkeypatch.setattr(evaluator, "_run_worker", lambda *_args: worker)
    for name, implementation in (
        ("preflight_stage", preflight),
        ("ec2_stage", provision),
        ("install_auth_stage", login),
        ("personal_aws_stage", lambda *_: lambda _ctx: None),
        ("evidence_stage", lambda *_: lambda _ctx: None),
        ("cleanup_stage", retire),
    ):
        monkeypatch.setattr(stages, name, implementation)
    monkeypatch.setattr(config, "from_environment", lambda *_: cfg)

    class Aws:
        def call(self, service, operation, **_kwargs):
            if (service, operation) == ("sts", "get_caller_identity"):
                return {
                    "Account": str(cfg["platform_account"]),
                    "Arn": f"arn:aws:sts::{cfg['platform_account']}:assumed-role/adp-cli-uplift-eval-orchestrator/fixture",
                }
            assert (service, operation) == ("ec2", "describe_instances")
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-00000001",
                                "State": {
                                    "Name": "terminated" if terminated else "running"
                                },
                            }
                        ]
                    }
                ]
            }

    monkeypatch.setattr(
        ports, "default_ports", lambda *_: {"aws": Aws(), "http": object()}
    )
    monkeypatch.setattr(product_inventory, "observe", lambda *_: inventory)
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("CLI_UPLIFT_EVAL_BINDINGS_JSON", "protected-binding")
    monkeypatch.setenv("CLI_UPLIFT_EVAL_GATEWAY_CATALOG", "protected-catalog")
    monkeypatch.setenv(
        "CLI_UPLIFT_EVAL_INTERNAL_INVOKE_URL", cfg["internal_invoke_url"]
    )
    for name, value in (
        ("INSTANCE_PROFILE", cfg["instance_profile"]),
        ("STATE_BUCKET", cfg["state_bucket"]),
        ("CREDENTIAL_SECRET_NAME", cfg["credential_secret_name"]),
    ):
        monkeypatch.setenv("CLI_UPLIFT_EVAL_" + name, value)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "boundary.txt"))
    output = tmp_path / "dispatch.json"
    arguments = [
        "--cases",
        selection,
        "--expected-revision",
        SOURCE,
        "--dispatch-report",
        str(output),
        "--api-evidence",
        str(tmp_path / "api-evidence.json"),
    ]
    if interrupted:
        with pytest.raises(ports.PortError, match="verified|cleanup"):
            live.main(arguments)
    else:
        live.main(arguments)
    report = json.loads(output.read_text())
    cfg["product_cases"] = selection
    if interrupted:
        assert not (tmp_path / "api-evidence.json").exists()
        assert report["status"] == "incomplete" and report["cleanup_verified"] is False
        assert received[-2:] == ["product_v01", "product_cleanup"]
        assert (tmp_path / "boundary.txt").read_text().endswith("cleanup_ok=false\n")
        recovered = product_execution.recover(cfg, RUN_ID, Aws(), store=store)
        assert recovered["cleanup_verified"] is True
        assert received[-1] == "independent_cleanup"
        assert (
            store.documents[recovered["evaluation_id"]][1]["resources"][-1]["status"]
            == "deleted"
        )
        return
    assert report["status"] == "pass" and report["cleanup_verified"] is True
    api_evidence = json.loads((tmp_path / "api-evidence.json").read_text())
    assert api_evidence["run_id"] == report["run_id"]
    assert api_evidence["source_revision"] == SOURCE
    assert api_evidence["observation_ref"] == report["observation_ref"]
    assert api_evidence["inventory"]["pods"] == [
        {"uid": "owned-pod", "digest": inventory["digest"]}
    ]
    assert set(api_evidence["product"]["results"]) == set(selection.split(","))
    assert api_evidence["product"]["cleanup"]["fixture_removed"] is True
    assert report["selected_cases"] == selection.split(",")
    assert report["observation_ref"] == native["observation_ref"]
    assert received == [
        "product_workspace",
        "product_actor",
        "product_probe",
        "product_v01",
    ]
    boundary = (tmp_path / "boundary.txt").read_text()
    assert "evaluation_id=adp-e2e-" in boundary and boundary.endswith(
        "cleanup_ok=true\n"
    )


def test_protected_retirement_url_overlay_requires_regional_invoke_endpoint():
    base = config.load(config.EXAMPLE_PATH)
    selected = config.from_environment(
        {
            "CLI_UPLIFT_EVAL_INTERNAL_INVOKE_URL": "https://example123.execute-api.us-east-1.amazonaws.com/dev"
        },
        base=base,
    )
    assert (
        selected["internal_invoke_url"]
        == "https://example123.execute-api.us-east-1.amazonaws.com/dev"
    )
    with pytest.raises(config.ConfigError, match="regional execute-api"):
        config.from_environment(
            {
                "CLI_UPLIFT_EVAL_INTERNAL_INVOKE_URL": "https://example123.execute-api.us-west-2.amazonaws.com/dev"
            },
            base=base,
        )
