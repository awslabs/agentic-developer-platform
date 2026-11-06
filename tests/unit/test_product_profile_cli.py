"""Synthetic Product driver checks; none supply deployed CLI acceptance."""

from __future__ import annotations

import copy
import hashlib
import importlib
import sys
from pathlib import Path

import pytest

from tests.e2e.cli_uplift import bundle, cases, live, runner, stages

REMOTE = Path(__file__).resolve().parents[1] / "e2e/cli_uplift/remote"
sys.path.insert(0, str(REMOTE))
product_profile = importlib.import_module("product_profile")
common = importlib.import_module("common")


class ProductCli:
    def __init__(self):
        self.calls = []
        self.profile = None

    def run(self, arguments):
        self.calls.append(arguments)
        verb = arguments[1]
        if verb == "create":
            self.profile = {
                "product_id": "owned-product",
                "project_id": "owned-project",
                "tenant_id": "fixture-tenant",
                "revision": 1,
                "product": {
                    "name": arguments[arguments.index("--name") + 1],
                    "purpose": "Installed CLI verification",
                },
            }
        if verb == "list":
            return 0, {"status": "ok", "detail": {"items": [{"id": "owned-product"}]}}
        if verb == "update":
            self.profile["revision"] = 2
            self.profile["product"]["purpose"] = "Installed CLI readback verified"
        if verb == "lifecycle":
            self.profile["revision"] += 1
            if arguments[arguments.index("--action") + 1] == "archive":
                self.profile["archived_at"] = "fixed-clock"
            else:
                self.profile.pop("archived_at")
        return 0, {"status": "ok", "detail": copy.deepcopy(self.profile)}


@pytest.fixture
def fixture(tmp_path):
    binary = tmp_path / "adp"
    binary.write_text("installed wrapper")
    helper = tmp_path / "adp-product.py"
    helper.write_text("installed product helper")
    expected = {
        name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in ("adp", "adp-product.py")
    }
    config = {
        "cli_path": str(binary),
        "expected_hashes": expected,
        "evaluation_id": "adp-e2e-20261019-220000-aabbcc",
        "org_id": "fixture-tenant",
        "test_user_id": "fixture-owner",
        "product_profile": {
            "owned_mutations_authorized": True,
            "tenant_id": "fixture-tenant",
            "login_user_id": "fixture-owner",
        },
    }
    return config, ProductCli(), {"transcript": []}


def test_installed_cli_roundtrip_uses_independent_api_readback(fixture):
    config, cli, evidence = fixture
    reads = []

    def readback(path):
        reads.append(path)
        return 200, copy.deepcopy(cli.profile)

    product_profile.run_roundtrip(config, cli, readback, evidence)
    assert [call[1] for call in cli.calls] == [
        "create",
        "show",
        "list",
        "update",
        "lifecycle",
        "lifecycle",
    ]
    assert len(reads) >= 5
    assert evidence["success"] is True
    assert [entry["revision"] for entry in evidence["api_readback"]] == [1, 1, 2, 3, 4]


@pytest.mark.parametrize("failure", ["stale_helper", "unowned", "mismatched_api"])
def test_roundtrip_rejects_incomplete_release_or_readback(fixture, failure):
    config, cli, evidence = fixture
    if failure == "stale_helper":
        Path(config["cli_path"]).with_name("adp-product.py").write_text("stale helper")
    if failure == "unowned":
        config["product_profile"]["tenant_id"] = "wrong-tenant"

    def readback(_path):
        observed = copy.deepcopy(cli.profile)
        if failure == "mismatched_api":
            observed["revision"] = 99
        return 200, observed

    with pytest.raises(common.RemoteError):
        product_profile.run_roundtrip(config, cli, readback, evidence)
    assert evidence.get("success") is not True
    if failure != "mismatched_api":
        assert cli.calls == []


def test_roundtrip_selector_dispatches_a_shipped_driver():
    selected = cases.suite_cases("product-profile")
    assert {"PR03-C01", "PR01"} <= {case.id for case in selected}
    assert stages.JOURNEY_DRIVERS["PR03-C01"] == "product_profile_roundtrip"
    assert "product_profile_roundtrip" in bundle.purposes()


def test_product_cli_packet_handoff_documents_callable_selector_and_private_artifact():
    documentation = (
        Path(__file__).resolve().parents[2]
        / "docs/regression-testing/cli-uplift-evaluation.md"
    ).read_text()
    handoff = documentation.split("### Product installed-CLI handoff (#8247)", 1)[1]
    arguments = runner.parse_args(
        ["--config", "protected.json", "--mode", "start", "--suite", "product-profile"]
    )
    assert arguments.mode == "start" and arguments.suite == ["product-profile"]
    assert "PR03-C01" in {case.id for case in cases.suite_cases(arguments.suite[0])}
    assert "python -m tests.e2e.cli_uplift.runner" in handoff
    assert "--mode start --suite product-profile" in handoff
    assert all(
        f'--{option} "${variable}"' in handoff
        for option, variable in (
            ("config", "EVAL_RUN_CONFIG"),
            ("state-dir", "EVAL_STATE_DIR"),
            ("out-dir", "EVAL_OUT_DIR"),
        )
    )
    assert all(
        f"$EVAL_OUT_DIR/{filename}" in handoff
        for filename in (
            "report.json",
            "results.xml",
            "product-profile-cli.private.json",
        )
    )
    assert "CLI_UPLIFT_EVAL_BINDINGS_JSON" in handoff
    assert "CLI_UPLIFT_EVAL_GATEWAY_CATALOG" in handoff
    for variable in (
        "CLI_UPLIFT_EVAL_STATE_BUCKET",
        "CLI_UPLIFT_EVAL_INTERNAL_INVOKE_URL",
        "product_run_id",
    ):
        assert variable in handoff
    assert "There is no pre-provisioned `product_profile` binding" in handoff
    assert "retirement receipt" in handoff
    prose = " ".join(handoff.replace("`", "").split())
    assert "coordinator invokes V03 for that exact run" in prose
    assert "one human accepts or rejects" in prose
    assert '--mode cleanup --config "$EVAL_RUN_CONFIG"' in handoff
    assert '--evaluation-id "$EVALUATION_ID" --restore' in handoff


@pytest.mark.parametrize("use_factory", [False, True])
def test_denial_retry_selector_runs_shipped_driver_after_owned_roundtrip(
    tmp_path, use_factory
):
    from tests.e2e.cli_uplift import cleanup

    selected = cases.suite_cases("product-profile")
    assert [case.id for case in selected if case.id.startswith("PR03-")] == [
        "PR03-C01",
        "PR03-C02",
        "PR03-C03",
    ]
    assert stages.JOURNEY_DRIVERS["PR03-C02"] == "product_profile_denial_retry"
    assert "product_profile_denial_retry" in bundle.purposes()
    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    owned = {"product_id": "owned-product", "project_id": "owned-project"}
    configuration = {
        "product_profile": {
            "owned_mutations_authorized": True,
            "tenant_id": "fixture-tenant",
            "login_user_id": "fixture-owner",
            "foreign_tenant_id": "foreign-tenant",
            "foreign_user_id": "foreign-user",
            "foreign_fixture_name": "fixture-foreign",
        },
        "platform_account": "000000000101",
        "region": "us-east-1",
        "gateway_url": "https://gateway.example.test/api",
        "sts_endpoint": None,
        "secrets_endpoint": None,
        "expected_revision": "a" * 40,
        "cognito_user_pool_id": "pool-owned",
        "claude_model": "fixture-model",
        "proxy_port": 19191,
        "usage_wait_seconds": 5,
        "cloudtrail_wait_seconds": 5,
        "inference_timeout_seconds": 5,
        "max_output_length": 256,
        "absence_wait_seconds": 5,
    }
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json", evaluation_id, on_change=lambda *_args, **_kw: None
    )
    matrix = cases.new_matrix(("product-profile",))
    context = {
        "document": {
            "instance_id": "owned-host",
            "session": {"org_id": "fixture-tenant", "user_id": "fixture-owner"},
        },
        "matrix": matrix,
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
        "evaluation_id": evaluation_id,
        "attempt_id": evaluation_id + "-a1",
        "manifest": manifest,
        "fault": "none",
        "preflight": {},
        "transcript": [],
        "correlation": {},
    }
    calls = []

    def worker(purpose):
        def drive(_instance, ctx):
            calls.append(purpose)
            payload = live._journey_payload(configuration, ctx)
            if purpose == "product_profile_roundtrip":
                assert payload["owned_product"] == {}
                return {
                    "success": True,
                    "owned_product": owned,
                    "cli_events": [
                        {
                            "argv": ["adp", "product", "show"],
                            "exit_code": 0,
                            "output": {},
                        }
                    ]
                    * 6,
                    "api_readback": [
                        {**owned, "revision": revision} for revision in (1, 1, 2, 3, 4)
                    ],
                    "installed_hashes": {"adp": "a" * 64, "adp-product.py": "b" * 64},
                }
            assert purpose == "product_profile_denial_retry"
            assert payload["owned_product"] == owned
            return {
                "success": True,
                "cli_events": [
                    {"argv": ["adp", "product", "show"], "exit_code": 0, "output": {}}
                ],
                "installed_hashes": {"adp": "a" * 64, "adp-product.py": "b" * 64},
                "denials": [{"error_code": "not_found"}] * 3,
                "retry": {"committed_revision": 5},
            }

        return drive

    deleters = {"product_fixture": lambda *_args: None}

    def recovery_factory(actual_cfg, actual_ctx):
        assert actual_cfg is configuration
        assert actual_ctx is context
        return deleters

    stages.journeys_stage(
        configuration,
        {
            "journey": worker,
            "deleters": recovery_factory if use_factory else deleters,
        },
    )(context)
    assert calls == ["product_profile_roundtrip", "product_profile_denial_retry"]
    assert matrix["PR03-C01"]["status"] == cases.PASSED
    assert matrix["PR03-C02"]["status"] == cases.PASSED
    assert len(context["document"]["product_cli_attempts"]) == 2
    assert manifest.read()["resources"][0]["detail"]["case"] == "PR03-C01"


def test_denial_retry_selector_blocks_without_this_attempts_owned_roundtrip():
    matrix = cases.new_matrix(("case:PR03-C02",))
    outcomes = []
    stages.journeys_stage(
        {},
        {
            "deleters": {"product_fixture": lambda *_args: None},
            "journey": lambda _purpose: pytest.fail("worker must not be dispatched"),
        },
    )(
        {
            "document": {"instance_id": "owned-host"},
            "matrix": matrix,
            "record": lambda *result: outcomes.append(result),
        }
    )
    assert outcomes[0][0:2] == ("PR03-C02", cases.BLOCKED)
    assert "verified roundtrip Product" in outcomes[0][2]["detail"]


class NegativeCli:
    def __init__(self, profile, *, foreign=False, duplicate=False):
        self.profile = profile
        self.foreign = foreign
        self.duplicate = duplicate
        self.calls = []

    def run(self, arguments, *, expected=None):
        self.calls.append(arguments)
        verb = arguments[1]
        if self.foreign:
            if verb == "list":
                return 0, {"status": "ok", "detail": {"items": []}}
            return 5, {
                "status": "failed",
                "error": {"code": "not_found", "message": "Not found"},
            }
        if verb == "update" and "stale_" in arguments[-1]:
            return 4, {"status": "failed", "error": {"code": "stale_revision"}}
        if verb == "update" and self.duplicate:
            self.profile["revision"] += 1
        return 0, {"status": "ok", "detail": copy.deepcopy(self.profile)}


@pytest.fixture
def negative_fixture(fixture):
    config, _cli, evidence = fixture
    config["product_profile"]["foreign_tenant_id"] = "other-tenant"
    config["owned_product"] = {
        "product_id": "owned-product",
        "project_id": "owned-project",
    }
    profile = {
        "product_id": "owned-product",
        "project_id": "owned-project",
        "tenant_id": "fixture-tenant",
        "revision": 4,
        "product": {
            "name": "Owned Product " + config["evaluation_id"],
            "purpose": "Installed CLI readback verified",
        },
    }
    cli = NegativeCli(profile)
    foreign = NegativeCli(profile, foreign=True)
    return config, cli, foreign, evidence


def test_denied_and_retried_product_changes_are_read_back(negative_fixture):
    config, cli, foreign, evidence = negative_fixture
    reads = []

    def readback(path):
        reads.append(path)
        return 200, copy.deepcopy(cli.profile)

    def drop(arguments, path, key):
        assert path == "/api/v1/products/owned-product/projects/owned-project"
        assert arguments[-1] == key
        cli.profile["revision"] += 1
        cli.profile["product"]["purpose"] = "Retry reconciled"
        return (
            4,
            {"status": "failed", "error": {"code": "unknown_mutation_outcome"}},
            True,
        )

    product_profile.run_denial_retry(config, cli, foreign, readback, drop, evidence)
    assert evidence["success"] is True
    assert evidence["retry"] == {
        "initial_revision": 4,
        "committed_revision": 5,
        "dropped_exit_code": 4,
        "dropped_error_code": "unknown_mutation_outcome",
        "replay_exit_code": 0,
    }
    assert [(item["command"], item["error_code"]) for item in evidence["denials"]] == [
        ("update", "stale_revision"),
        ("show", "not_found"),
        ("update", "not_found"),
    ]
    assert len(reads) >= 4
    assert [call[1] for call in foreign.calls] == ["show", "update", "list"]


@pytest.mark.parametrize(
    "failure",
    [
        "stale_helper",
        "foreign_disclosure",
        "drop_missing",
        "duplicate_replay",
        "unowned_product",
    ],
)
def test_negative_driver_fails_closed(negative_fixture, failure):
    config, cli, foreign, evidence = negative_fixture
    if failure == "stale_helper":
        Path(config["cli_path"]).with_name("adp-product.py").write_text("stale helper")
    if failure == "unowned_product":
        cli.profile["product"]["name"] = "Unrelated Product"
    if failure == "foreign_disclosure":
        foreign.foreign = False

    def readback(_path):
        return 200, copy.deepcopy(cli.profile)

    def drop(_arguments, _path, _key):
        cli.profile["revision"] += 1
        cli.profile["product"]["purpose"] = "Retry reconciled"
        if failure == "duplicate_replay":
            cli.duplicate = True
        return (
            4,
            {"status": "failed", "error": {"code": "unknown_mutation_outcome"}},
            failure != "drop_missing",
        )

    with pytest.raises(common.RemoteError):
        product_profile.run_denial_retry(config, cli, foreign, readback, drop, evidence)
    assert evidence.get("success") is not True
    if failure in ("stale_helper", "unowned_product"):
        assert cli.calls == []


def test_loopback_relay_drops_one_committed_response_only(monkeypatch):
    import http.client

    from product_profile_proxy import drop_product_response

    requests = []

    class Response:
        status = 200

        def __init__(self):
            self.headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return b'{"revision":5}'

    class Opener:
        def open(self, request, timeout):
            assert timeout == 60
            requests.append(
                (
                    request.get_method(),
                    request.full_url,
                    request.get_header("Idempotency-key"),
                )
            )
            return Response()

    monkeypatch.setattr(
        "product_profile_proxy.urllib.request.build_opener", lambda *_args: Opener()
    )
    target = "/api/v1/products/id/projects/pid"
    with drop_product_response(
        "https://gateway.example.test", target, "run-owned-key"
    ) as (url, state):
        port = int(url.rsplit(":", 1)[1])
        for index, key in enumerate(("other-key", "run-owned-key", "run-owned-key")):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(
                "PATCH", target, body=b"{}", headers={"Idempotency-Key": key}
            )
            if index == 1:
                with pytest.raises(http.client.RemoteDisconnected):
                    connection.getresponse()
            else:
                with connection.getresponse() as response:
                    assert response.status == 200
                    assert response.read() == b'{"revision":5}'
            connection.close()
        assert state == {"forwarded": True, "dropped": True}
    assert len(requests) == 3


def test_missing_product_recovery_blocks_before_creating_anything():
    matrix = cases.new_matrix(("case:PR03-C01",))
    outcomes = []
    context = {
        "document": {"instance_id": "owned-host"},
        "matrix": matrix,
        "record": lambda *result: outcomes.append(result),
    }
    stage = stages.journeys_stage(
        {},
        {
            "deleters": {},
            "journey": lambda _purpose: pytest.fail("worker must not be dispatched"),
        },
    )
    stage(context)
    assert outcomes[0][0:2] == ("PR03-C01", cases.BLOCKED)
    assert "durable, authorized deleter" in outcomes[0][2]["detail"]
    verdict, _reasons = cases.accept(matrix, ("case:PR03-C01",))
    assert verdict != cases.PASSED


def test_product_intent_is_durable_before_remote_dispatch(tmp_path):
    from tests.e2e.cli_uplift import cleanup

    saved = []
    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    manifest = cleanup.Manifest(
        tmp_path / "owned" / "manifest.json",
        evaluation_id,
        on_change=lambda document, *, critical: saved.append(
            (copy.deepcopy(document), critical)
        ),
    )
    matrix = cases.new_matrix(("case:PR03-C01",))
    outcomes = []

    def worker(_instance, _context):
        assert saved[-1][1] is True
        record = saved[-1][0]["resources"][0]
        assert record["kind"] == "product_fixture" and record["id"] == evaluation_id
        assert (
            saved[-1][0]["diagnostic_intents"]["PR03-C01:" + evaluation_id]
            == record["detail"]
        )
        assert record["detail"] == {
            "evaluation_id": evaluation_id,
            "case": "PR03-C01",
            "criterion": "AC-01",
            "tenant_id": "fixture-tenant",
            "name": "Owned Product " + evaluation_id,
            "create_key": "create_" + evaluation_id,
            "idempotency_key": "create_" + evaluation_id,
            "actor_id": "fixture-owner",
            "source_revision": "a" * 40,
            "instance_id": "owned-host",
            "actor_secret_id": f"adp/cli-uplift-eval/{evaluation_id}-product-fixtures",
            "actor_cognito_id": f"pool-owned/{evaluation_id}-product-admin@adp-eval.example",
        }
        return {"success": False, "stage": "blocked"}

    context = {
        "document": {
            "instance_id": "owned-host",
            "session": {"org_id": "fixture-tenant", "user_id": "fixture-owner"},
        },
        "matrix": matrix,
        "record": lambda *result: outcomes.append(result),
        "evaluation_id": evaluation_id,
        "manifest": manifest,
        "fault": "none",
        "transcript": [],
        "correlation": {},
    }
    stage = stages.journeys_stage(
        {
            "expected_revision": "a" * 40,
            "cognito_user_pool_id": "pool-owned",
            "platform_account": "000000000101",
            "region": "us-east-1",
            "product_profile": {
                "owned_mutations_authorized": True,
                "tenant_id": "fixture-tenant",
                "login_user_id": "fixture-owner",
            },
        },
        {
            "deleters": {"product_fixture": lambda *_args: None},
            "journey": lambda _purpose: worker,
        },
    )
    stage(context)
    assert outcomes[0][0:2] == ("PR03-C01", cases.FAILED)
    assert manifest.outstanding()[0]["kind"] == "product_fixture"


def test_product_cleanup_failure_stays_red_and_reused_connections_survive(tmp_path):
    from tests.e2e.cli_uplift import cleanup

    manifest = cleanup.Manifest(tmp_path / "owned" / "manifest.json", "owned-run")
    manifest.record("product_fixture", "owned-run")
    manifest.record("adp_connection", "shared-connection", reused=True)
    ok, _results = cleanup.sweep(manifest, {})
    assert ok is False
    assert manifest.outstanding()[0]["status"] == cleanup.FAILED
    assert manifest.read()["resources"][1]["status"] == cleanup.REUSED

    def failed_cleanup(_identity):
        raise RuntimeError("Product purge is not available")

    ok, _results = cleanup.sweep(manifest, {"product_fixture": failed_cleanup})
    assert ok is False
    assert manifest.outstanding()[0]["status"] == cleanup.FAILED
    assert manifest.read()["resources"][1]["status"] == cleanup.REUSED


def test_durable_manifest_failure_prevents_product_dispatch(tmp_path):
    from tests.e2e.cli_uplift import cleanup

    evaluation_id = "adp-e2e-20261019-220000-aabbcc"

    def unavailable_store(_document, *, critical):
        assert critical is True
        raise RuntimeError("private recovery manifest cannot be published")

    manifest = cleanup.Manifest(
        tmp_path / "owned" / "manifest.json", evaluation_id, on_change=unavailable_store
    )
    called = []
    context = {
        "document": {
            "instance_id": "owned-host",
            "session": {"org_id": "fixture-tenant", "user_id": "fixture-owner"},
        },
        "matrix": cases.new_matrix(("case:PR03-C01",)),
        "record": lambda *result: called.append(result),
        "evaluation_id": evaluation_id,
        "manifest": manifest,
    }
    stage = stages.journeys_stage(
        {
            "expected_revision": "a" * 40,
            "cognito_user_pool_id": "pool-owned",
            "product_profile": {
                "owned_mutations_authorized": True,
                "tenant_id": "fixture-tenant",
                "login_user_id": "fixture-owner",
            },
        },
        {
            "deleters": {"product_fixture": lambda *_args: None},
            "journey": lambda _purpose: pytest.fail("worker must not be dispatched"),
        },
    )
    with pytest.raises(RuntimeError, match="cannot be published"):
        stage(context)
    assert not called
    assert (
        manifest.read().get("diagnostic_intents", {}).get("PR03-C01:" + evaluation_id)
    )


def test_cleanup_selector_uses_recovery_sweep_and_retains_failed_attempts(tmp_path):
    from tests.e2e.cli_uplift import cleanup

    assert "PR03-C03" in {case.id for case in cases.suite_cases("product-profile")}
    assert stages.CLEANUP_DRIVERS["PR03-C03"] == "product_fixture"
    assert "PR03-C03" not in stages.JOURNEY_DRIVERS
    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    revision = "a" * 40
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json", evaluation_id, on_change=lambda *_args, **_kw: None
    )
    manifest.record("product_fixture", evaluation_id, detail={"case": "PR03-C01"})
    manifest.record("adp_connection", "shared-connection", reused=True)
    matrix = cases.new_matrix(("product-profile",))
    cases.record(matrix, "PR03-C01", cases.PASSED)
    owned = {"product_id": "owned-product", "project_id": "owned-project"}
    document = {
        "product_cli_attempts": [
            {
                "case_id": "PR03-C01",
                "attempt_id": evaluation_id + "-a1",
                "requirements_revision": revision,
                "owned_product": owned,
                "status": "passed",
            }
        ]
    }
    context = {
        "document": document,
        "matrix": matrix,
        "evaluation_id": evaluation_id,
        "attempt_id": evaluation_id + "-a1",
        "manifest": manifest,
        "fault": "none",
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
    }
    configuration = {"expected_revision": revision}
    fail = stages.cleanup_stage(configuration, {"deleters": lambda *_args: {}})
    assert fail(context) is False
    assert matrix["PR03-C03"]["status"] == cases.FAILED
    assert document["product_cli_attempts"][-1]["cleanup_results"] == [
        {"kind": "product_fixture", "status": cleanup.FAILED}
    ]
    assert manifest.outstanding()[0]["kind"] == "product_fixture"

    cases.record(matrix, "PR03-C03", cases.NOT_RUN)
    context["attempt_id"] = evaluation_id + "-a2"
    owned_products = {evaluation_id}

    def remove_and_verify(identifier):
        assert identifier == evaluation_id
        owned_products.remove(identifier)
        assert identifier not in owned_products
        document["product_retirement_receipt"] = {
            "run_id": evaluation_id,
            "product_id": owned["product_id"],
            "project_id": owned["project_id"],
            "cleanup_revision": 6,
            "fixture_removed": True,
        }
        document["product_recovery_attempts"] = [
            {
                "state": "reconcile_started",
                "idempotency_key": "create_" + evaluation_id,
            },
            {"state": "reconcile_received", **owned},
        ]

    recover = stages.cleanup_stage(
        configuration,
        {"deleters": lambda *_args: {"product_fixture": remove_and_verify}},
    )
    assert recover(context) is True
    assert not owned_products
    assert matrix["PR03-C03"]["status"] == cases.PASSED
    assert [attempt["status"] for attempt in document["product_cli_attempts"][1:]] == [
        "failed",
        "passed",
    ]
    assert document["product_cli_attempts"][-1]["owned_product"] == owned
    assert (
        document["product_cli_attempts"][-1]["retirement_receipt"]
        == document["product_retirement_receipt"]
    )
    assert (
        document["product_cli_attempts"][-1]["recovery_attempts"]
        == document["product_recovery_attempts"]
    )
    assert manifest.read()["resources"][1]["status"] == cleanup.REUSED


def test_cleanup_selector_rejects_missing_run_owned_fixture(tmp_path):
    from tests.e2e.cli_uplift import cleanup

    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    manifest = cleanup.Manifest(tmp_path / "manifest.json", evaluation_id)
    manifest.record("adp_connection", "shared-connection", reused=True)
    matrix = cases.new_matrix(("case:PR03-C03",))
    context = {
        "document": {},
        "matrix": matrix,
        "evaluation_id": evaluation_id,
        "attempt_id": evaluation_id + "-a1",
        "manifest": manifest,
        "fault": "none",
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
    }
    stage = stages.cleanup_stage(
        {"expected_revision": "a" * 40}, {"deleters": lambda *_args: {}}
    )
    assert stage(context) is True
    assert matrix["PR03-C03"]["status"] == cases.FAILED
    assert context["document"]["product_cli_attempts"][0]["status"] == "failed"
    assert manifest.read()["resources"][0]["status"] == cleanup.REUSED


def test_product_cleanup_attempt_is_durable_before_deleter_failure(tmp_path):
    from tests.e2e.cli_uplift import cleanup, ports

    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    document = {"product_cli_attempts": []}
    saved = []
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        evaluation_id,
        on_change=lambda *_args, **_kwargs: saved.append(copy.deepcopy(document)),
    )
    manifest.record("product_fixture", evaluation_id, detail={"case": "PR03-C01"})
    matrix = cases.new_matrix(("case:PR03-C03",))
    context = {
        "evaluation_id": evaluation_id,
        "attempt_id": evaluation_id + "-a1",
        "document": document,
        "manifest": manifest,
        "matrix": matrix,
        "fault": "none",
    }

    def unavailable(_cfg, _ctx):
        assert saved[-1]["product_cli_attempts"][0]["status"] == "failed"
        assert (
            manifest.read()["diagnostic_intents"][
                "PR03-C03:" + context["attempt_id"] + ":" + evaluation_id
            ]["attempt_id"]
            == context["attempt_id"]
        )
        raise ports.PortError("protected removal unavailable")

    with pytest.raises(ports.PortError, match="protected removal unavailable"):
        stages.cleanup_stage(
            {"expected_revision": "a" * 40}, {"deleters": unavailable}
        )(context)
    assert document["product_cli_attempts"] == saved[-1]["product_cli_attempts"]
    assert manifest.outstanding()[0]["kind"] == "product_fixture"


def product_report_fixture(*, attempts=True, cleanup_ok=True):
    from tests.e2e.cli_uplift import report

    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    revision = "a" * 40
    matrix = cases.new_matrix(("case:PR03-C01",))
    cases.record(matrix, "PR03-C01", cases.PASSED, {"checks": ["synthetic-only"]})
    supplied = (
        [
            {
                "case_id": "PR03-C01",
                "attempt_id": evaluation_id + "-a1",
                "status": "passed",
                "requirements_revision": revision,
                "cli_events": [
                    {
                        "actor": "owner",
                        "argv": [
                            "adp",
                            "product",
                            "show",
                            "--product",
                            "private-fixture-id",
                            "--json",
                        ],
                        "exit_code": 0,
                        "output": {
                            "status": "ok",
                            "detail": {"product_id": "private-fixture-id"},
                        },
                    }
                ],
                "api_readback": [
                    {"revision": count, "status": 200} for count in (1, 1, 2, 3, 4)
                ],
                "installed_hashes": {"adp": "e" * 64, "adp-product.py": "f" * 64},
            }
        ]
        if attempts
        else []
    )
    document = report.build(
        matrix=matrix,
        suites=("case:PR03-C01",),
        config={"expected_revision": revision},
        evaluation_id=evaluation_id,
        attempt_id=evaluation_id + "-a1",
        cleanup_ok=cleanup_ok,
        timing={
            "started_at": "2026-10-19T22:00:00Z",
            "ended_at": "2026-10-19T22:01:00Z",
            "duration_seconds": 60,
        },
        correlation={},
        transcript=["adp product show --product <redacted> --json"],
        preflight={
            "deployed_revision": revision,
            "expected_release": {
                "files": {"adp": "e" * 64, "adp-product.py": "f" * 64}
            },
            "product_release_check": {
                "source_revision": revision,
                "workflow_revision": "b" * 40,
                "image_digest": "sha256:" + "c" * 64,
                "cli_manifest_sha256": "d" * 64,
            },
        },
        product_cli_attempts=supplied,
    )
    return document, matrix, supplied


def test_private_report_requires_recovery_proof_not_an_extra_cli_command():
    from tests.e2e.cli_uplift import report

    document, _matrix, supplied = product_report_fixture()
    document["product_run_id"] = "product-123456-2"
    document["cases"].append({"id": "PR03-C03", "status": cases.PASSED})
    recovery = {
        "case_id": "PR03-C03",
        "attempt_id": document["attempt_id"],
        "requirements_revision": document["expected_revision"],
        "status": "passed",
        "absence_verified": True,
        "owned_product": {"product_id": "owned-product", "project_id": "owned-project"},
        "cleanup_results": [{"kind": "product_fixture", "status": "deleted"}],
        "retirement_receipt": {
            "run_id": document["evaluation_id"],
            "product_id": "owned-product",
            "project_id": "owned-project",
            "cleanup_revision": 6,
            "fixture_removed": True,
        },
        "recovery_attempts": [],
    }
    private = report.product_cli_report(document, [*supplied, recovery])
    assert private["workflow_run_id"] == document["product_run_id"]
    assert not any("PR03-C03 lacks" in failure for failure in private["failures"])
    assert private["attempts"][-1]["cleanup_results"] == recovery["cleanup_results"]

    recovery["absence_verified"] = False
    private = report.product_cli_report(document, [*supplied, recovery])
    assert any("PR03-C03 lacks" in failure for failure in private["failures"])


def complete_product_report_fixture():
    from tests.e2e.cli_uplift import report

    document, _matrix, _attempts = product_report_fixture()
    evaluation_id = document["evaluation_id"]
    owned = {"product_id": "private-product", "project_id": "private-project"}
    hashes = {"adp": "e" * 64, "adp-product.py": "f" * 64}
    previous = {
        "case_id": "PR03-C01",
        "attempt_id": evaluation_id + "-a1",
        "requirements_revision": document["expected_revision"],
        "status": "failed",
        "cli_events": [
            {
                "actor": "owner",
                "argv": ["adp", "product", "create", "--json"],
                "exit_code": 5,
                "output": {"status": "failed"},
            }
        ],
    }
    roundtrip = {
        **previous,
        "attempt_id": evaluation_id + "-a2",
        "status": "passed",
        "owned_product": owned,
        "cli_events": [
            {
                "actor": "owner",
                "argv": ["adp", "product", verb, "--json"],
                "exit_code": 0,
                "output": {"status": "ok", "detail": owned},
            }
            for verb in ("create", "show", "list", "update", "lifecycle", "lifecycle")
        ],
        "api_readback": [
            {"status": 200, "revision": revision, **owned}
            for revision in (1, 1, 2, 3, 4)
        ],
        "installed_hashes": hashes,
    }
    denial = {
        "case_id": "PR03-C02",
        "attempt_id": evaluation_id + "-a2",
        "requirements_revision": document["expected_revision"],
        "status": "passed",
        "cli_events": [
            {
                "actor": "foreign",
                "argv": ["adp", "product", "show", "--json"],
                "exit_code": 5,
                "output": {"status": "failed", "error": {"code": "not_found"}},
            }
        ],
        "denials": [
            {"command": verb, "exit_code": 5, "error_code": "not_found"}
            for verb in ("show", "update", "list")
        ],
        "retry": {"reconciled": True},
        "installed_hashes": hashes,
    }
    cleanup_attempt = {
        "case_id": "PR03-C03",
        "attempt_id": evaluation_id + "-a2",
        "requirements_revision": document["expected_revision"],
        "status": "passed",
        "owned_product": owned,
        "absence_verified": True,
        "cleanup_results": [{"kind": "product_fixture", "status": "deleted"}],
        "retirement_receipt": {
            "run_id": evaluation_id,
            **owned,
            "cleanup_revision": 6,
            "fixture_removed": True,
        },
        "recovery_attempts": [
            {
                "state": "reconcile_started",
                "idempotency_key": "create_" + evaluation_id,
            },
            {"state": "reconcile_received", **owned},
        ],
    }
    attempts = [previous, roundtrip, denial, cleanup_attempt]
    document["attempt_id"] = evaluation_id + "-a2"
    matrix = cases.new_matrix(("case:PR03-C01", "case:PR03-C02", "case:PR03-C03"))
    for case_id in matrix:
        cases.record(matrix, case_id, cases.PASSED)
    document = report.build(
        matrix=matrix,
        suites=("case:PR03-C01", "case:PR03-C02", "case:PR03-C03"),
        config={"expected_revision": document["expected_revision"]},
        evaluation_id=evaluation_id,
        attempt_id=document["attempt_id"],
        cleanup_ok=True,
        timing=document["timing"],
        correlation={},
        preflight=document["preflight"],
        product_cli_attempts=attempts,
    )
    return document, matrix, attempts


def test_complete_private_cli_report_contains_every_attempt_and_retirement(tmp_path):
    import json

    from tests.e2e.cli_uplift import report

    document, matrix, attempts = complete_product_report_fixture()
    assert document["status"] == cases.PASSED
    paths = report.write(
        tmp_path,
        document,
        matrix,
        document["evaluation_id"],
        product_run_id="product-123456-2",
        product_cli_attempts=attempts,
    )
    payload = json.loads(Path(paths["product_cli_private"]).read_text())
    assert payload["status"] == "passed"
    assert Path(paths["product_cli_private"]).stat().st_mode & 0o777 == 0o600
    assert [
        (row["case_id"], row["criterion"], row["scenario"]) for row in payload["cases"]
    ] == [
        ("PR03-C01", "AC-01", "SCN-01"),
        ("PR03-C02", "AC-02", "SCN-02"),
        ("PR03-C03", "AC-03", "SCN-03"),
    ]
    assert payload["cases"][0]["attempt_ids"] == [
        document["evaluation_id"] + "-a1",
        document["evaluation_id"] + "-a2",
    ]
    assert payload["attempts"][1]["cli_events"] == attempts[1]["cli_events"]
    assert payload["attempts"][2]["cli_events"] == attempts[2]["cli_events"]
    assert payload["attempts"][1]["api_readback"] == attempts[1]["api_readback"]
    assert (
        payload["attempts"][3]["retirement_receipt"]
        == attempts[3]["retirement_receipt"]
    )
    assert [item["state"] for item in payload["attempts"][3]["recovery_attempts"]] == [
        "reconcile_started",
        "reconcile_received",
    ]
    for artifact in ("report", "junit"):
        assert "private-product" not in Path(paths[artifact]).read_text()


@pytest.mark.parametrize(
    "failure",
    [
        "missing_command",
        "missing_exit",
        "string_argv",
        "wrong_readback",
        "missing_receipt",
        "wrong_receipt",
        "stale_attempt",
        "foreign_attempt",
        "not_run",
        "failed_cleanup",
    ],
)
def test_private_cli_report_refuses_incomplete_or_foreign_proof(failure):
    from tests.e2e.cli_uplift import report

    document, _matrix, attempts = complete_product_report_fixture()
    document = copy.deepcopy(document)
    attempts = copy.deepcopy(attempts)
    if failure == "missing_command":
        attempts[1]["cli_events"] = []
    elif failure == "missing_exit":
        attempts[2]["cli_events"][0].pop("exit_code")
    elif failure == "string_argv":
        attempts[2]["cli_events"][0]["argv"] = "adp product show --json"
    elif failure == "wrong_readback":
        attempts[1]["api_readback"][0]["product_id"] = "foreign-product"
    elif failure == "missing_receipt":
        attempts[3].pop("retirement_receipt")
    elif failure == "wrong_receipt":
        attempts[3]["retirement_receipt"]["product_id"] = "foreign-product"
    elif failure == "stale_attempt":
        attempts[0]["requirements_revision"] = "0" * 40
    elif failure == "foreign_attempt":
        attempts[2]["attempt_id"] = "another-evaluation-a2"
    elif failure == "not_run":
        document["cases"][2]["status"] = cases.NOT_RUN
    else:
        document["cleanup"] = "failed"
    private = report.product_cli_report(document, attempts)
    assert private["status"] == "incomplete"
    assert private["failures"]


def test_private_cli_report_keeps_real_command_output_separate(tmp_path):
    import json

    from tests.e2e.cli_uplift import report

    document, matrix, attempts = product_report_fixture()
    assert report.validate(document) == []
    paths = report.write(
        tmp_path,
        document,
        matrix,
        document["evaluation_id"],
        product_cli_attempts=attempts,
        product_run_id="product-123456-2",
    )
    private = Path(paths["product_cli_private"])
    assert private.stat().st_mode & 0o777 == 0o600
    payload = json.loads(private.read_text())
    assert payload["surface"] == "installed-cli"
    assert payload["workflow_run_id"] == "product-123456-2"
    assert "product_run_id" not in document
    assert payload["requirements_revision"] == "a" * 40
    assert payload["workflow_revision"] == "b" * 40
    assert (
        payload["attempts"][0]["cli_events"][0]["output"]["detail"]["product_id"]
        == "private-fixture-id"
    )
    assert payload["status"] == "incomplete"
    assert document["status"] == cases.FAILED
    assert "private-fixture-id" not in Path(paths["report"]).read_text()
    assert "private-fixture-id" not in Path(paths["junit"]).read_text()


@pytest.mark.parametrize(
    "failure", ["missing", "bad_hash", "bad_cleanup", "no_release"]
)
def test_missing_product_cli_evidence_cannot_report_acceptance(failure):
    from tests.e2e.cli_uplift import report

    document, _matrix, attempts = product_report_fixture(
        attempts=failure != "missing", cleanup_ok=failure != "bad_cleanup"
    )
    if failure == "bad_hash":
        attempts[0]["installed_hashes"]["adp"] = "0" * 64
    if failure == "no_release":
        document["preflight"]["product_release_check"].pop("workflow_revision")
    private = report.product_cli_report(document, attempts)
    assert private["status"] == "incomplete"
    assert private["failures"]
    assert document["status"] == cases.FAILED
    assert document["full_acceptance"] is False


def test_private_cli_report_preserves_previous_failed_attempt():
    from tests.e2e.cli_uplift import report

    document, _matrix, attempts = product_report_fixture()
    previous = {
        **attempts[0],
        "attempt_id": document["evaluation_id"] + "-a0",
        "status": "failed",
    }
    private = report.product_cli_report(document, [previous, *attempts])
    assert private["cases"][0]["attempt_ids"] == [
        previous["attempt_id"],
        attempts[0]["attempt_id"],
    ]
    assert [entry["status"] for entry in private["attempts"]] == ["failed", "passed"]


def test_recording_cli_captures_real_json_exit_and_redacted_stderr(monkeypatch):
    import json

    def command(_argv, **_kwargs):
        return (
            4,
            json.dumps({"status": "failed", "error": {"code": "stale_revision"}}),
            "private server message",
        )

    monkeypatch.setattr(common, "bounded", command)
    evidence = {"transcript": []}
    cli = product_profile.RecordingCli(
        "/installed/adp", {}, evidence["transcript"], evidence, "owner"
    )
    code, result = cli.run(["product", "update", "--revision", "1"], expected=None)
    assert code == 4 and result["error"]["code"] == "stale_revision"
    assert evidence["cli_events"][0]["argv"] == [
        "adp",
        "product",
        "update",
        "--revision",
        "1",
        "--json",
    ]
    assert evidence["cli_events"][0]["exit_code"] == 4
    assert evidence["cli_events"][0]["stderr_diagnostic"]["redacted"] == "<redacted>"
    assert "private server message" not in json.dumps(evidence["cli_events"])


def product_owner_fixture(tmp_path):
    from tests.e2e.cli_uplift import cleanup

    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    config = {
        "platform_account": "000000000101",
        "region": "us-east-1",
        "gateway_url": "https://gateway.example.test",
        "cognito_user_pool_id": "pool-owned",
        "expected_revision": "a" * 40,
        "product_run_id": "product-123-1",
    }
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        evaluation_id,
        on_change=lambda *_args, **_kwargs: None,
    )
    manifest.record(
        "ec2_instance",
        "i-12345678",
        account=config["platform_account"],
        region=config["region"],
    )
    manifest.record(
        "cognito_user",
        f"pool-owned/{evaluation_id}-admin@adp-eval.example",
        account=config["platform_account"],
        region=config["region"],
    )
    manifest.record(
        "adp_user",
        "bootstrap-tenant/bootstrap-owner",
        account=config["platform_account"],
        region=config["region"],
    )
    context = {
        "evaluation_id": evaluation_id,
        "document": {
            "instance_id": "i-12345678",
            "product_run_id": "product-123-1",
            "session": {
                "session_ref": bundle.REMOTE_DIR + "/session.json",
                "user_id": "bootstrap-owner",
                "org_id": "bootstrap-tenant",
                "created_username": evaluation_id + "-admin@adp-eval.example",
            },
        },
        "preflight": {
            "product_release_check": {
                "source_revision": "a" * 40,
                "workflow_revision": "b" * 40,
                "run_attempt": 1,
            }
        },
        "expected_hashes": {"install.sh": "a" * 64, "adp-product.py": "b" * 64},
        "manifest": manifest,
    }
    return config, context


@pytest.mark.parametrize("intent_store_available", [True, False])
def test_production_product_owner_prepares_isolated_cli_session(
    tmp_path, monkeypatch, intent_store_available
):
    config, context = product_owner_fixture(tmp_path)
    evaluation_id, manifest = context["evaluation_id"], context["manifest"]
    dispatched = []

    def worker(instance_id, purpose, payload, *, manifest):
        dispatched.append((purpose, payload))
        assert instance_id == "i-12345678"
        assert payload["product_cases"] == "api_product_lifecycle"
        if purpose == "product_workspace":
            assert payload["session_ref"] == bundle.REMOTE_DIR + "/session.json"
            manifest.record(
                "adp_org",
                payload["product_workspace_id"],
                account=config["platform_account"],
                region=config["region"],
            )
            return {
                "success": True,
                "stage": "complete",
                "workspace_id": payload["product_workspace_id"],
                "team_id": "owned-team",
            }
        assert purpose == "product_actor"
        assert payload["work_dir"] == bundle.REMOTE_DIR + "/product-actor"
        assert payload["expected_hashes"] == context["expected_hashes"]
        assert any(
            entry["kind"] == "adp_user_email"
            and entry["id"]
            == payload["product_workspace_id"] + "/" + payload["created_username"]
            and entry.get("account") == config["platform_account"]
            and entry.get("region") == config["region"]
            for entry in manifest.outstanding()
        )
        return {
            "success": True,
            "session": {
                "created_username": evaluation_id + "-product-admin@adp-eval.example",
                "session_ref": bundle.REMOTE_DIR + "/product-actor/session.json",
                "cli_path": "/home/ec2-user/.adp/bin/adp",
                "org_id": payload["product_workspace_id"],
                "user_id": "verified-owner",
            },
        }

    monkeypatch.setattr(
        live,
        "_admin_fixtures",
        lambda *_args, **_kwargs: lambda _ctx: {
            "credential_secret": "owned-secret",
            "created_username": evaluation_id + "-product-admin@adp-eval.example",
        },
    )
    from tests.e2e.product_onboarding import product_sessions

    retained = []
    monkeypatch.setattr(
        product_sessions, "retain", lambda *_args, owner=False: retained.append(owner)
    )
    prepare = live._product_profile_owner(config, worker, object(), object(), object())
    if not intent_store_available:
        from tests.e2e.cli_uplift import ports

        def unavailable(snapshot, *, critical):
            if critical and any(
                entry["kind"] == "adp_user_email" for entry in snapshot["resources"]
            ):
                raise ports.PortError("registration intent was not durably saved")

        manifest.on_change = unavailable
        with pytest.raises(ports.PortError, match="not durably saved"):
            prepare(context)
        assert [purpose for purpose, _payload in dispatched] == ["product_workspace"]
        assert retained == [False]
        return
    fixture = prepare(context)
    assert retained == [False, True]
    assert [purpose for purpose, _payload in dispatched] == [
        "product_workspace",
        "product_actor",
    ]
    assert fixture == {
        "owned_mutations_authorized": True,
        "tenant_id": context["document"]["product_actor_session"]["org_id"],
        "login_user_id": "verified-owner",
        "foreign_tenant_id": "bootstrap-tenant",
        "foreign_user_id": "bootstrap-owner",
        "foreign_session_ref": bundle.REMOTE_DIR + "/session.json",
    }
    assert any(
        entry["kind"] == "adp_user"
        and entry["id"] == fixture["tenant_id"] + "/verified-owner"
        for entry in manifest.outstanding()
    )
    assert live.wire(config).get("prepare_product_profile") is not None


@pytest.mark.parametrize("cleanup_denied", [False, True])
def test_lost_owner_registration_recovers_by_email_after_host_termination(
    tmp_path, monkeypatch, cleanup_denied
):
    from types import SimpleNamespace

    from tests.e2e.cli_uplift import cleanup, ports
    from tests.e2e.product_onboarding import product_sessions

    config, context = product_owner_fixture(tmp_path)
    evaluation_id, manifest = context["evaluation_id"], context["manifest"]
    username = evaluation_id + "-product-admin@adp-eval.example"
    saved = {}
    private = {}
    store = SimpleNamespace(
        _get=lambda key: copy.deepcopy(private.get(key)),
        _put=lambda key, value: private.update({key: copy.deepcopy(value)}),
    )

    def persist(snapshot, *, critical):
        saved.update(
            document=copy.deepcopy(context["document"]),
            manifest=copy.deepcopy(snapshot),
            critical=critical,
        )

    manifest.on_change = persist
    bootstrap = context["document"]["session"]
    ssm = SimpleNamespace(
        json_result=lambda *_args, **_kwargs: (
            {"Status": "Success"},
            {**bootstrap, "access_token": "private-token", "refresh_token": "refresh"},
        )
    )

    def verify_session(_cfg, path, *, token):
        assert path == "/auth/cli/admin-session" and token == "private-token"
        return 200, {**bootstrap, "verified": True, "role": "platform_admin"}

    monkeypatch.setattr(product_sessions, "request", verify_session)

    def fixtures(_context):
        for kind, identifier in (
            ("cognito_user", "pool-owned/" + username),
            ("secret", f"adp/cli-uplift-eval/{evaluation_id}-product-fixtures"),
        ):
            manifest.record(
                kind,
                identifier,
                account=config["platform_account"],
                region=config["region"],
            )
        return {"created_username": username}

    monkeypatch.setattr(live, "_admin_fixtures", lambda *_args, **_kwargs: fixtures)
    users = [{"id": "shared-user", "email": "shared@example.invalid"}]

    def worker(_instance, purpose, payload, *, manifest):
        workspace = payload["product_workspace_id"]
        if purpose == "product_workspace":
            manifest.record(
                "adp_org",
                workspace,
                account=config["platform_account"],
                region=config["region"],
            )
            return {"team_id": "owned-team"}
        assert purpose == "product_actor"
        assert saved["critical"] is True
        assert any(
            entry["kind"] == "adp_user_email"
            and entry["id"] == workspace + "/" + username
            for entry in saved["manifest"]["resources"]
        )
        users.append({"id": "registered-owner", "email": username, "org_id": workspace})
        raise ports.PortError("registration committed but worker response lost")

    with pytest.raises(ports.PortError, match="worker response lost"):
        live._product_profile_owner(config, worker, object(), ssm, store)(context)
    assert "product_actor_session" not in saved["document"]
    assert not any(
        entry["kind"] == "adp_user" and entry["id"].endswith("/registered-owner")
        for entry in saved["manifest"]["resources"]
    )
    recovered_manifest = cleanup.Manifest(tmp_path / "recovery.json", evaluation_id)
    recovered_manifest._write(saved["manifest"])
    recovered_manifest.mark("ec2_instance", "i-12345678", cleanup.DELETED)
    recovery = {
        "evaluation_id": evaluation_id,
        "document": copy.deepcopy(saved["document"]),
        "manifest": recovered_manifest,
    }
    recovered_manifest.on_change = lambda snapshot, **_kwargs: saved.update(
        manifest=copy.deepcopy(snapshot)
    )
    ssm.json_result = lambda *_args, **_kwargs: pytest.fail("host was terminated")
    monkeypatch.setattr(
        live, "_vault_token", lambda *_args: pytest.fail("host was terminated")
    )
    removed = []

    class UserAPI:
        denied = cleanup_denied

        def get(self, url, *, token, expect):
            assert token == "private-token" and expect is None
            assert url.endswith("/users")
            return 200, {"users": copy.deepcopy(users)}

        def request(self, url, *, method, token, expect):
            assert token == "private-token" and method == "DELETE" and expect is None
            assert url.endswith("/users/registered-owner")
            if self.denied:
                return 503, {}
            removed.append("owner")
            users[:] = [user for user in users if user["id"] != "registered-owner"]
            return 204, None

    api = UserAPI()
    deleters = live._deleters(object(), config, api, ssm, store)(config, recovery)
    for kind in ("adp_org", "adp_user", "secret", "cognito_user"):
        deleters[kind] = lambda _identifier, kind=kind, **_kwargs: removed.append(kind)
    ok, _results = cleanup.sweep(recovered_manifest, deleters)
    if cleanup_denied:
        assert ok is False
        assert removed == []
        assert {entry["kind"] for entry in recovered_manifest.outstanding()} == {
            "adp_user_email",
            "adp_org",
            "adp_user",
            "secret",
            "cognito_user",
        }
        assert any(
            entry["kind"] == "adp_user_email" and entry["status"] == cleanup.FAILED
            for entry in saved["manifest"]["resources"]
        )
        api.denied = False
        ok, _results = cleanup.sweep(recovered_manifest, deleters)
    assert ok is True
    assert removed[0] == "owner"
    assert recovered_manifest.outstanding() == []
    assert users == [{"id": "shared-user", "email": "shared@example.invalid"}]


@pytest.mark.parametrize(
    "missing", ["release", "bootstrap", "durability", "wrong_owner", "wrong_run"]
)
def test_production_product_owner_refuses_unverified_setup(
    tmp_path, monkeypatch, missing
):
    from tests.e2e.cli_uplift import cleanup, ports

    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    config = {
        "platform_account": "000000000101",
        "region": "us-east-1",
        "gateway_url": "https://gateway.example.test",
        "cognito_user_pool_id": "pool-owned",
        "expected_revision": "a" * 40,
        "product_run_id": "product-123-1",
    }
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        evaluation_id,
        on_change=None if missing == "durability" else lambda *_args, **_kwargs: None,
    )
    if missing != "durability":
        manifest.record(
            "cognito_user",
            f"pool-owned/{evaluation_id}-admin@adp-eval.example",
            account=config["platform_account"],
            region=config["region"],
        )
        manifest.record(
            "ec2_instance",
            "i-12345678",
            account=config["platform_account"],
            region=config["region"],
        )
    context = {
        "evaluation_id": evaluation_id,
        "manifest": manifest,
        "document": {
            "instance_id": "i-12345678",
            "product_run_id": "product-123-1",
            "session": {
                "session_ref": bundle.REMOTE_DIR + "/session.json",
                "user_id": "bootstrap-owner",
                "org_id": "bootstrap-tenant",
                "created_username": evaluation_id + "-admin@adp-eval.example",
            },
        },
        "preflight": {
            "product_release_check": {
                "source_revision": "a" * 40,
                "workflow_revision": "b" * 40,
                "run_attempt": 1,
            }
        },
        "expected_hashes": {"adp-product.py": "b" * 64},
    }
    if missing == "wrong_run":
        context["document"]["product_run_id"] = "product-999-1"
    if missing == "release":
        context["preflight"].clear()
    if missing == "bootstrap":
        context["document"]["session"].clear()
    dispatched = []

    def worker(_instance_id, purpose, payload, *, manifest):
        dispatched.append(purpose)
        if purpose == "product_workspace":
            return {"team_id": "owned-team"}
        return {
            "success": True,
            "session": {
                "created_username": evaluation_id + "-product-admin@adp-eval.example",
                "session_ref": bundle.REMOTE_DIR + "/product-actor/session.json",
                "org_id": "wrong-tenant",
                "user_id": "other",
                "cli_path": "/bin/adp",
            },
        }

    monkeypatch.setattr(live, "_admin_fixtures", lambda *_args, **_kw: lambda _ctx: {})
    from tests.e2e.product_onboarding import product_sessions

    monkeypatch.setattr(product_sessions, "retain", lambda *_args, owner=False: None)
    with pytest.raises(ports.PortError):
        live._product_profile_owner(config, worker, object(), object(), object())(
            context
        )
    assert dispatched == (
        ["product_workspace", "product_actor"] if missing == "wrong_owner" else []
    )


def test_protected_product_stage_uses_prepared_owner_not_configured_fixture(tmp_path):
    from tests.e2e.cli_uplift import cleanup

    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        evaluation_id,
        on_change=lambda *_args, **_kwargs: None,
    )
    matrix = cases.new_matrix(("case:PR03-C01",))
    context = {
        "evaluation_id": evaluation_id,
        "attempt_id": evaluation_id + "-a1",
        "manifest": manifest,
        "document": {
            "instance_id": "i-12345678",
            "session": {"user_id": "bootstrap-user", "org_id": "bootstrap-tenant"},
        },
        "matrix": matrix,
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
        "fault": "none",
        "transcript": [],
        "correlation": {},
    }
    owned = {"product_id": "owned-product", "project_id": "owned-project"}
    calls = []

    def prepare(actual):
        calls.append("prepare")
        actual["document"]["product_actor_session"] = {
            "org_id": "owned-workspace",
            "user_id": "owned-user",
        }
        actual["product_profile"] = {
            "owned_mutations_authorized": True,
            "tenant_id": "owned-workspace",
            "login_user_id": "owned-user",
        }

    def drive(_instance, actual):
        calls.append("drive")
        assert actual["product_profile"]["tenant_id"] != "configured-shared-tenant"
        return {
            "success": True,
            "owned_product": owned,
            "cli_events": [
                {"argv": ["adp", "product", "show"], "exit_code": 0, "output": {}}
            ]
            * 6,
            "api_readback": [
                {**owned, "revision": number} for number in (1, 1, 2, 3, 4)
            ],
            "installed_hashes": {"adp": "a" * 64, "adp-product.py": "b" * 64},
        }

    stages.journeys_stage(
        {
            "expected_revision": "a" * 40,
            "cognito_user_pool_id": "pool-owned",
            "platform_account": "000000000101",
            "region": "us-east-1",
            "product_profile": {
                "owned_mutations_authorized": True,
                "tenant_id": "configured-shared-tenant",
                "login_user_id": "bootstrap-user",
            },
        },
        {
            "prepare_product_profile": prepare,
            "deleters": {"product_fixture": lambda *_args: None},
            "journey": lambda purpose: drive
            if purpose == "product_profile_roundtrip"
            else None,
        },
    )(context)
    assert calls == ["prepare", "drive"]
    assert matrix["PR03-C01"]["status"] == cases.PASSED
    assert manifest.outstanding()[0]["detail"]["tenant_id"] == "owned-workspace"


def test_product_checkpoint_installs_with_run_owned_bootstrap(tmp_path):
    from tests.e2e.cli_uplift import cleanup

    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    matrix = cases.new_matrix(("case:PR03-C01",))
    manifest = cleanup.Manifest(tmp_path / "manifest.json", evaluation_id)
    context = {
        "evaluation_id": evaluation_id,
        "document": {"instance_id": "i-12345678"},
        "preflight": {"expected_release": {"files": {"adp": "a" * 64}}},
        "matrix": matrix,
        "manifest": manifest,
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
        "transcript": [],
    }
    calls = []

    def fixtures(actual):
        assert actual is context
        calls.append("owned fixture")
        return {
            "credential_secret": "adp/cli-uplift-eval/owned-fixtures",
            "created_username": evaluation_id + "-admin@adp-eval.example",
        }

    def worker(_instance, purpose, payload):
        calls.append(purpose)
        assert payload["credential_secret"] == "adp/cli-uplift-eval/owned-fixtures"
        assert payload["admin_challenges_required"] is True
        assert payload["login_required"] is True
        return {
            "success": True,
            "session": {
                "created_username": evaluation_id + "-admin@adp-eval.example",
                "user_id": "run-owned-user",
                "org_id": "foreign-tenant",
                "session_ref": bundle.REMOTE_DIR + "/session.json",
            },
            "login": {
                "authenticated": True,
                "bad_credentials_rejected": True,
                "refresh_succeeded": True,
                "user_id": "run-owned-user",
            },
        }

    stages.install_auth_stage(
        {
            "platform_account": "000000000101",
            "region": "us-east-1",
            "cognito_user_pool_id": "pool-owned",
        },
        {
            "admin_fixtures": fixtures,
            "run_worker": worker,
            "worker_config": lambda *_args: {"credential_secret": "shared-secret"},
        },
    )(context)
    assert calls == ["owned fixture", "install_auth"]
    assert (
        context["document"]["session"]["created_username"]
        == evaluation_id + "-admin@adp-eval.example"
    )


@pytest.mark.parametrize("missing_store", [False, True])
def test_product_bootstrap_session_is_retained_before_product_mutation(
    tmp_path, monkeypatch, missing_store
):
    from tests.e2e.cli_uplift import cleanup, ports
    from tests.e2e.product_onboarding import product_sessions

    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    config = {
        "platform_account": "000000000101",
        "region": "us-east-1",
        "cognito_user_pool_id": "pool-owned",
        "product_run_id": "product-123-1",
    }
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        evaluation_id,
        on_change=lambda *_args, **_kwargs: None,
    )
    context = {
        "evaluation_id": evaluation_id,
        "document": {"instance_id": "i-12345678", "product_run_id": "product-123-1"},
        "preflight": {"expected_release": {"files": {"adp": "a" * 64}}},
        "matrix": cases.new_matrix(("case:PR03-C01",)),
        "manifest": manifest,
        "transcript": [],
        "record": lambda *_args: None,
    }
    identity = {
        "created_username": evaluation_id + "-admin@adp-eval.example",
        "user_id": "foreign-user",
        "org_id": "foreign-workspace",
        "session_ref": bundle.REMOTE_DIR + "/session.json",
    }

    class Store:
        def __init__(self):
            self.private = {}

        def _get(self, key):
            return self.private.get(key)

        def _put(self, key, value):
            self.private[key] = value

    class Ssm:
        def json_result(self, instance, command, *, purpose, timeout):
            assert instance == "i-12345678" and purpose == "session_handoff"
            assert timeout == 120 and "session.json" in command[-1]
            return {"Status": "Success"}, {
                **identity,
                "access_token": "private-owner-token",
                "refresh_token": "private-refresh-token",
            }

    store = None if missing_store else Store()
    monkeypatch.setattr(product_sessions, "verify", lambda *_args: True)

    def worker(*_args):
        return {
            "session": identity,
            "resources": [("adp_user", "foreign-workspace/foreign-user")],
        }

    stage = stages.install_auth_stage(
        config,
        {
            "admin_fixtures": lambda _ctx: {
                "created_username": identity["created_username"]
            },
            "run_worker": worker,
            "worker_config": lambda *_args: {},
            "retain_product_session": live._retain_product_session(
                config, store, Ssm()
            ),
        },
    )
    if missing_store:
        with pytest.raises(ports.PortError, match="private durable state"):
            stage(context)
        assert context["manifest"].outstanding()
    else:
        stage(context)
        assert context["document"]["session"]["user_id"] == "foreign-user"
        private = store.private[product_sessions.key(evaluation_id)]
        assert private["binding"]["org_id"] == "foreign-workspace"
        assert private["access_token"] == "private-owner-token"
        assert "private-owner-token" not in str(context["document"])


@pytest.mark.parametrize("invalid", [None, "reference", "identity", "tenant"])
def test_foreign_product_session_is_original_run_owned_bootstrap(
    tmp_path, monkeypatch, invalid
):
    from tests.e2e.cli_uplift import ports

    owner_dir = tmp_path / "product-actor"
    owner_dir.mkdir()
    reference = str(tmp_path / "session.json")
    fixture = {
        "tenant_id": "owned-workspace",
        "login_user_id": "owned-user",
        "foreign_tenant_id": "foreign-workspace",
        "foreign_user_id": "foreign-user",
        "foreign_session_ref": reference,
    }
    if invalid == "reference":
        fixture["foreign_session_ref"] = str(tmp_path / "elsewhere.json")
    if invalid == "tenant":
        fixture["foreign_tenant_id"] = "owned-workspace"
    configuration = {
        "work_dir": str(owner_dir),
        "session_ref": str(owner_dir / "session.json"),
        "gateway_url": "https://gateway.example.test",
        "region": "us-east-1",
        "sts_endpoint": "https://sts.example.test",
        "cli_path": "/bin/adp",
        "org_id": "owned-workspace",
        "test_user_id": "owned-user",
        "product_profile": fixture,
    }
    sessions = []

    def session(config):
        sessions.append(config["session_ref"])
        return {
            "access_token": "owner"
            if config["work_dir"] == str(owner_dir)
            else "foreign"
        }

    monkeypatch.setattr(common, "session_tokens", session)
    monkeypatch.setattr(
        common,
        "api",
        lambda *_args, **_kwargs: (
            200,
            {
                "verified": True,
                "user_id": "incorrect-user"
                if invalid == "identity"
                else "foreign-user",
                "org_id": "foreign-workspace",
            },
        ),
    )
    called = []
    monkeypatch.setattr(
        product_profile,
        "run_denial_retry",
        lambda config, owner, foreign, readback, fault, evidence: called.append(
            (owner.actor, foreign.actor, callable(fault))
        ),
    )
    if invalid:
        with pytest.raises((ports.PortError, common.RemoteError)):
            product_profile.execute_denial_retry(configuration, {"transcript": []})
        assert called == []
        assert sessions == (
            [configuration["session_ref"], reference] if invalid == "identity" else []
        )
    else:
        product_profile.execute_denial_retry(configuration, {"transcript": []})
        assert sessions == [configuration["session_ref"], reference]
        assert called == [("owner", "foreign", True)]


def test_shipped_denial_driver_receives_only_verified_owner_and_foreign_ref(
    monkeypatch,
):
    from tests.e2e.cli_uplift import ports

    config = {
        "platform_account": "000000000101",
        "region": "us-east-1",
        "gateway_url": "https://gateway.example.test",
        "sts_endpoint": "https://sts.example.test",
        "secrets_endpoint": "https://secrets.example.test",
        "expected_revision": "a" * 40,
        "claude_model": "fixture-model",
        "proxy_port": 19191,
        "usage_wait_seconds": 5,
        "cloudtrail_wait_seconds": 5,
        "inference_timeout_seconds": 5,
        "max_output_length": 256,
        "absence_wait_seconds": 5,
        "product_profile": {"tenant_id": "configured-shared-tenant"},
    }
    dispatched = []
    monkeypatch.setattr(
        live,
        "_run_worker",
        lambda *_args: lambda _instance, purpose, payload: dispatched.append(
            (purpose, payload)
        ),
    )
    driver = live._journey(None, config, None)("product_profile_denial_retry")
    assert driver is not None
    context = {
        "evaluation_id": "adp-e2e-20261019-220000-aabbcc",
        "document": {
            "instance_id": "i-12345678",
            "session": {
                "org_id": "foreign-workspace",
                "user_id": "foreign-user",
                "session_ref": bundle.REMOTE_DIR + "/session.json",
            },
            "product_actor_session": {
                "org_id": "owned-workspace",
                "user_id": "owned-user",
                "session_ref": bundle.REMOTE_DIR + "/product-actor/session.json",
                "cli_path": bundle.REMOTE_DIR + "/product-actor/cli/adp",
            },
        },
        "product_profile": {
            "tenant_id": "owned-workspace",
            "login_user_id": "owned-user",
            "owned_mutations_authorized": True,
            "foreign_tenant_id": "foreign-workspace",
            "foreign_user_id": "foreign-user",
            "foreign_session_ref": bundle.REMOTE_DIR + "/session.json",
        },
        "preflight": {},
    }
    driver("i-12345678", context)
    purpose, payload = dispatched.pop()
    assert purpose == "product_profile_denial_retry"
    assert payload["org_id"] == "owned-workspace"
    assert payload["session_ref"] == bundle.REMOTE_DIR + "/product-actor/session.json"
    assert (
        payload["product_profile"]["foreign_session_ref"]
        == bundle.REMOTE_DIR + "/session.json"
    )
    assert payload["product_profile"] != config["product_profile"]
    context["product_profile"]["login_user_id"] = "mismatched-user"
    with pytest.raises(ports.PortError):
        driver("i-12345678", context)
    assert dispatched == []


def retirement_fixture(tmp_path):
    from tests.e2e.cli_uplift import cleanup, product_profile_recovery

    evaluation_id = "adp-e2e-20261019-220000-aabbcc"
    config = {
        "platform_account": "000000000101",
        "region": "us-east-1",
        "gateway_url": "https://gateway.example.test",
        "expected_revision": "a" * 40,
        "cognito_user_pool_id": "pool-owned",
        "product_run_id": "product-123-1",
        "state_bucket": "private-eval-state",
        "internal_invoke_url": "https://private.execute-api.us-east-1.amazonaws.com/dev",
    }
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        evaluation_id,
        on_change=lambda *_args, **_kwargs: None,
    )
    for kind, identifier in (
        ("ec2_instance", "i-12345678"),
        ("adp_org", "owned-workspace"),
        ("adp_user", "owned-workspace/owned-user"),
    ):
        manifest.record(
            kind,
            identifier,
            account=config["platform_account"],
            region=config["region"],
        )
    matrix = cases.new_matrix(("case:PR03-C01",))
    context = {
        "evaluation_id": evaluation_id,
        "attempt_id": evaluation_id + "-a1",
        "manifest": manifest,
        "matrix": matrix,
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
        "fault": "none",
        "product_profile": {
            "owned_mutations_authorized": True,
            "tenant_id": "owned-workspace",
            "login_user_id": "owned-user",
        },
        "transcript": [],
        "correlation": {},
        "document": {
            "instance_id": "i-12345678",
            "product_run_id": config["product_run_id"],
            "session": {
                "user_id": "foreign-user",
                "org_id": "foreign-workspace",
                "created_username": evaluation_id + "-admin@adp-eval.example",
            },
            "product_actor_session": {
                "user_id": "owned-user",
                "org_id": "owned-workspace",
            },
        },
    }

    class Store:
        def __init__(self):
            self.saved = []

        def save(self, identity, document, snapshot):
            self.saved.append(
                (identity, copy.deepcopy(document), copy.deepcopy(snapshot))
            )

    class Http:
        def __init__(self):
            self.paths = []
            self.visible = None

        def get(self, url, *, token, expect):
            assert token == "run-owner-token" and expect is None
            self.paths.append(url)
            if url.endswith("?limit=100"):
                return 200, {"items": [{"id": "shared-product"}]}
            return (
                (200, {"product_id": "owned-product"})
                if url == self.visible
                else (404, {})
            )

    store, http = Store(), Http()
    handler = product_profile_recovery.build(config, object(), http, context, store)
    assert handler.available is True
    owned = {"product_id": "owned-product", "project_id": "owned-project"}
    stages.journeys_stage(
        config,
        {
            "deleters": {"product_fixture": handler},
            "journey": lambda _purpose: lambda *_args: {
                "success": True,
                "owned_product": owned,
                "cli_events": [
                    {"argv": ["adp", "product", "show"], "exit_code": 0, "output": {}}
                ]
                * 6,
                "api_readback": [
                    {**owned, "revision": revision} for revision in (1, 1, 2, 3, 4)
                ],
                "installed_hashes": {"adp": "a" * 64, "adp-product.py": "b" * 64},
            },
        },
    )(context)
    assert matrix["PR03-C01"]["status"] == cases.PASSED
    manifest.record("adp_connection", "shared-connection", reused=True)
    return config, context, store, http, handler


@pytest.mark.parametrize("lost_response", [False, True])
def test_retirement_uses_original_cli_intent_and_checks_history(
    tmp_path, monkeypatch, lost_response
):
    from tests.e2e.cli_uplift import cleanup, product_profile_recovery
    from tests.e2e.product_onboarding import product_execution, product_sessions

    config, context, store, http, handler = retirement_fixture(tmp_path)
    requests = []
    monkeypatch.setattr(
        product_sessions,
        "restore",
        lambda *_args, owner=False: {
            "user_id": "owned-user",
            "org_id": "owned-workspace",
            "access_token": "run-owner-token",
        },
    )

    def retire(_config, _aws, intent):
        requests.append(copy.deepcopy(intent))
        if lost_response and len(requests) == 1:
            raise product_execution.RetirementConflict("response unknown")
        return {
            "detail": {
                "run_id": context["evaluation_id"],
                "product_id": "owned-product",
                "project_id": "owned-project",
                "cleanup_revision": 6,
                "fixture_removed": True,
            }
        }

    def replay(_config, path, *, token, body, idempotency_key, expected_status):
        assert (
            store.saved[-1][1]["product_recovery_attempts"][0]["state"]
            == "reconcile_started"
        )
        assert path == "/api/v1/products" and expected_status == 201
        assert token == "run-owner-token"
        assert body == product_profile_recovery.create_body(context["evaluation_id"])
        assert idempotency_key == "create_" + context["evaluation_id"]
        return 201, {
            "tenant_id": "owned-workspace",
            "product_id": "owned-product",
            "project_id": "owned-project",
            "product": {"name": body["name"]},
        }

    monkeypatch.setattr(product_execution, "retire_independently", retire)
    monkeypatch.setattr(product_sessions, "request", replay)
    handler(
        context["evaluation_id"],
        account=config["platform_account"],
        region=config["region"],
    )
    assert requests[0] == {
        "run_id": context["evaluation_id"],
        "tenant_id": "owned-workspace",
        "actor_id": "owned-user",
        "source_revision": config["expected_revision"],
        "idempotency_key": "create_" + context["evaluation_id"],
    }
    assert len(requests) == (2 if lost_response else 1)
    assert all("product_id" not in request for request in requests[:1])
    assert [url.rsplit("/", 1)[-1] for url in http.paths] == [
        "owned-project",
        "changes",
        "1",
        "products?limit=100",
    ]
    assert context["document"]["product_retirement_receipt"]["fixture_removed"] is True
    assert any(
        entry["status"] == cleanup.REUSED
        for entry in context["manifest"].read()["resources"]
    )
    assert store.saved[-1][1]["product_retirement_receipt"]["cleanup_revision"] == 6


@pytest.mark.parametrize(
    "failure", ["wrong_actor", "wrong_key", "history_visible", "retire_denied"]
)
def test_retirement_refuses_wrong_scope_and_keeps_fixture_pending(
    tmp_path, monkeypatch, failure
):
    from tests.e2e.cli_uplift import cleanup, ports
    from tests.e2e.product_onboarding import product_execution, product_sessions

    config, context, store, http, handler = retirement_fixture(tmp_path)
    calls = []
    if failure == "wrong_actor":
        context["document"]["product_actor_session"]["user_id"] = "another-user"
    if failure == "wrong_key":
        context["manifest"]._mutate(
            lambda document: next(
                entry
                for entry in document["resources"]
                if entry["kind"] == "product_fixture"
            )["detail"].update(idempotency_key="stale-key"),
            critical=True,
        )
    if failure == "history_visible":
        http.visible = (
            config["gateway_url"]
            + "/api/v1/products/owned-product/projects/owned-project/changes"
        )
    monkeypatch.setattr(
        product_sessions,
        "restore",
        lambda *_args, owner=False: {
            "user_id": "owned-user",
            "org_id": "owned-workspace",
            "access_token": "run-owner-token",
        },
    )

    def retire(*_args):
        calls.append("retirement")
        if failure == "retire_denied":
            raise ports.PortError("protected route unavailable")
        return {
            "detail": {
                "run_id": context["evaluation_id"],
                "fixture_removed": True,
                "product_id": "owned-product",
                "project_id": "owned-project",
                "cleanup_revision": 6,
            }
        }

    monkeypatch.setattr(product_execution, "retire_independently", retire)
    with pytest.raises(ports.PortError):
        handler(
            context["evaluation_id"],
            account=config["platform_account"],
            region=config["region"],
        )
    assert calls == ([] if failure in {"wrong_actor", "wrong_key"} else ["retirement"])
    assert context["manifest"].outstanding()[0]["status"] != "deleted"
    assert "product_retirement_receipt" not in context["document"]
    ok, results = cleanup.sweep(
        context["manifest"],
        {
            "product_fixture": handler,
            **{
                kind: lambda *_args, **_kwargs: pytest.fail(
                    "dependent resource still required"
                )
                for kind in ("ec2_instance", "adp_org", "adp_user")
            },
        },
    )
    assert ok is False
    assert any(
        entry["kind"] == "ec2_instance" for entry in context["manifest"].outstanding()
    )
    assert any(
        result["kind"] == "product_fixture" and result["status"] == cleanup.FAILED
        for result in results
    )
    assert context["manifest"].read()["resources"][-1]["status"] == cleanup.REUSED


def test_product_sweep_retires_before_host_and_preserves_reused_connection(
    tmp_path, monkeypatch
):
    from tests.e2e.cli_uplift import cleanup
    from tests.e2e.product_onboarding import product_execution, product_sessions

    config, context, store, http, handler = retirement_fixture(tmp_path)
    operations = []
    monkeypatch.setattr(
        product_sessions,
        "restore",
        lambda *_args, owner=False: {
            "user_id": "owned-user",
            "org_id": "owned-workspace",
            "access_token": "run-owner-token",
        },
    )

    def retire(_cfg, _aws, intent):
        operations.append(("retire", intent["idempotency_key"]))
        return {
            "detail": {
                "run_id": context["evaluation_id"],
                "fixture_removed": True,
                "product_id": "owned-product",
                "project_id": "owned-project",
                "cleanup_revision": 6,
            }
        }

    monkeypatch.setattr(product_execution, "retire_independently", retire)

    def remove(kind):
        def delete(_identifier, **_location):
            operations.append((kind, "removed"))

        return delete

    ok, _results = cleanup.sweep(
        context["manifest"],
        {
            "product_fixture": handler,
            **{kind: remove(kind) for kind in ("ec2_instance", "adp_org", "adp_user")},
        },
    )
    assert ok is True
    assert operations[0] == ("retire", "create_" + context["evaluation_id"])
    assert operations[1][0] == "ec2_instance"
    assert context["manifest"].outstanding() == []
    assert context["manifest"].read()["resources"][-1]["status"] == cleanup.REUSED
    assert len(http.paths) == 4


def test_terminated_instance_restores_original_private_session_before_cleanup(
    tmp_path, monkeypatch
):
    from tests.e2e.product_onboarding import product_execution, product_sessions

    config, context, store, http, _handler = retirement_fixture(tmp_path)
    context["manifest"].record(
        "adp_user",
        "foreign-workspace/foreign-user",
        account=config["platform_account"],
        region=config["region"],
    )
    recovered = []

    def restore(_cfg, _ctx, supplied_store, *, owner=False):
        assert supplied_store is store
        recovered.append(owner)
        identity = context["document"]["product_actor_session" if owner else "session"]
        return {
            **identity,
            "prefix": context["evaluation_id"],
            "access_token": "run-owner-token",
        }

    monkeypatch.setattr(product_sessions, "restore", restore)
    monkeypatch.setattr(
        live,
        "_vault_token",
        lambda *_args: pytest.fail("terminated instance has no token vault"),
    )
    monkeypatch.setattr(
        product_execution,
        "retire_independently",
        lambda *_args: {
            "detail": {
                "run_id": context["evaluation_id"],
                "fixture_removed": True,
                "product_id": "owned-product",
                "project_id": "owned-project",
                "cleanup_revision": 6,
            }
        },
    )
    deleters = live._deleters(object(), config, http, object(), store)(config, context)
    assert deleters["product_fixture"].available is True
    assert recovered == [False]
    deleters["product_fixture"](
        context["evaluation_id"],
        account=config["platform_account"],
        region=config["region"],
    )
    assert recovered == [False, True]
    assert context["document"]["product_retirement_receipt"]["fixture_removed"] is True


def test_product_retirement_without_private_store_is_not_admitted(tmp_path):
    from tests.e2e.cli_uplift import product_profile_recovery

    config, context, _store, http, _handler = retirement_fixture(tmp_path)
    assert (
        product_profile_recovery.build(config, object(), http, context, None).available
        is False
    )
