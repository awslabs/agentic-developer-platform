"""Offline guards for the #5199 CLI uplift evaluation harness.

No AWS, no gateway, no model calls: the CI job runs these with sockets disabled.
The purpose is narrow and important — prove that the code deciding whether a run
may report success cannot be talked into a false green.

The issue requires three injected failures to each make full acceptance
non-successful: a wrong account, missing usage evidence, and a cleanup failure.
Those are `test_injected_*` below.
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import copy
import json
import io
import os
import pathlib
import re
import time
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from botocore.credentials import Credentials
from tests.e2e.cli_uplift import deployment_provenance as dp
from tests.e2e.cli_uplift.ports import PortError

from tests.e2e.cli_uplift import (
    build_run_config,
    bundle,
    cases,
    cleanup,
    config,
    contracts,
    live,
    personal_aws_worker,
    ports,
    preflight,
    release,
    report,
    runner,
    stages,
    statestore,
)

FULL = ("full",)


@pytest.mark.parametrize("org_id", ["", "fixture-org"])
def test_multi_deployment_accepts_native_admin_usage_attribution(
    tmp_path, monkeypatch, org_id
):
    module, common = shipped_script(tmp_path, "multi_deployment")
    session = {"verified": True, "user_id": "cognito-sub", "org_id": org_id}
    monkeypatch.setattr(common, "fixture_secret", lambda *a: "fixture")
    monkeypatch.setattr(module, "_access_token", lambda *a: "test-token")
    monkeypatch.setattr(common, "api", lambda *a, **k: (200, session))

    class Cli:
        def json(self, *args, **kwargs):
            return {"status": "verified"}

    result = module._login(
        {},
        Cli(),
        {},
        {
            "name": "dev",
            "gateway_url": "https://dev.example.test",
            "credential_secret_name": "fixture",
        },
        {},
    )
    assert result["org_id"] == org_id
    assert result["user_id"] == "cognito-sub"


@pytest.mark.parametrize(
    "session",
    [
        {"verified": True, "user_id": "cognito-sub"},
        {"verified": True, "user_id": "", "org_id": ""},
    ],
)
def test_multi_deployment_refuses_incomplete_usage_attribution(
    tmp_path, monkeypatch, session
):
    module, common = shipped_script(tmp_path, "multi_deployment")
    monkeypatch.setattr(common, "fixture_secret", lambda *a: "fixture")
    monkeypatch.setattr(module, "_access_token", lambda *a: "test-token")
    monkeypatch.setattr(common, "api", lambda *a, **k: (200, session))

    class Cli:
        def json(self, *args, **kwargs):
            return {"status": "verified"}

    with pytest.raises(common.RemoteError, match="incomplete usage attribution"):
        module._login(
            {},
            Cli(),
            {},
            {
                "name": "dev",
                "gateway_url": "https://dev.example.test",
                "credential_secret_name": "fixture",
            },
            {},
        )


def test_multi_deployment_thread_exception_cannot_pass(tmp_path, monkeypatch):
    module, common = shipped_script(tmp_path, "multi_deployment")

    def broken(*args, **kwargs):
        raise ValueError("unexpected tool failure")

    monkeypatch.setattr(module, "_run_tool", broken)
    with pytest.raises(common.RemoteError, match="unexpected tool failure"):
        module._pass(
            {"evaluation_id": "test"},
            {},
            dict.fromkeys(("dev", "int", "preprod")),
            {},
            {"transcript": []},
            label="overlap",
        )


def test_multi_deployment_requires_actual_time_overlap(tmp_path, monkeypatch):
    module, common = shipped_script(tmp_path, "multi_deployment")

    def sequential(config, env, session, tool, marker, transcript):
        start = session["start"]
        return {
            "deployment": session["name"],
            "started": start,
            "finished": start + 1,
            "exit_code": 0,
            "returned_marker": True,
        }

    monkeypatch.setattr(module, "_run_tool", sequential)
    sessions = {
        name: {"name": name, "start": index * 10}
        for index, name in enumerate(("dev", "int", "preprod"))
    }
    with pytest.raises(common.RemoteError, match="did not overlap"):
        module._pass(
            {"evaluation_id": "test"},
            {},
            sessions,
            {},
            {"transcript": []},
            label="overlap",
        )


def test_multi_deployment_matches_usage_request_id_not_prompt_text(
    tmp_path, monkeypatch
):
    module, common = shipped_script(tmp_path, "multi_deployment")
    row = {
        "request_id": "receipt-123",
        "timestamp": "2026-09-18T12:00:00Z",
        "user_id": "fixture",
        "status_code": 200,
    }
    monkeypatch.setattr(
        common, "api", lambda *a, **k: (200, {"items": [row], "has_more": False})
    )
    monkeypatch.setattr(common, "wait_for", lambda check, **k: check())
    session = {
        "gateway_url": "https://dev.example.test/api",
        "org_id": "org",
        "user_id": "fixture",
    }
    assert (
        module._usage_marker(
            {}, session, "test-token", "receipt-123", after="2026-09-18", expect=True
        )
        == row
    )
    assert (
        module._usage_marker(
            {}, session, "test-token", "fixture", after="2026-09-18", expect=True
        )
        is None
    )


def test_multi_deployment_refuses_truncated_absence_evidence(tmp_path, monkeypatch):
    module, common = shipped_script(tmp_path, "multi_deployment")
    monkeypatch.setattr(
        common, "api", lambda *a, **k: (200, {"items": [], "has_more": True})
    )
    monkeypatch.setattr(common, "wait_for", lambda check, **k: check())
    with pytest.raises(common.RemoteError, match="absence cannot be established"):
        module._usage_marker(
            {},
            {
                "gateway_url": "https://dev.example.test",
                "org_id": "org",
                "user_id": "user",
            },
            "fixture",
            "receipt",
            after="2026-09-18",
            expect=True,
        )


def test_multi_deployment_tools_send_usage_correlation_header(tmp_path, monkeypatch):
    module, common = shipped_script(tmp_path, "multi_deployment")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0, "marker", ""

    monkeypatch.setattr(common, "bounded", run)
    config = {"cli_path": "/fixture/adp", "claude_model": "fixture-model"}
    for tool in ("claude", "codex"):
        result = module._run_tool(config, {}, {"name": "dev"}, tool, "marker", [])
        argv, kwargs = calls[-1]
        if tool == "claude":
            assert (
                kwargs["env"]["ANTHROPIC_CUSTOM_HEADERS"]
                == "X-Request-ID: " + result["request_id"]
            )
        else:
            assert any(
                result["request_id"] in arg and "http_headers" in arg for arg in argv
            )


def test_multi_deployment_checks_proxies_after_both_arrangements(tmp_path, monkeypatch):
    module, common = shipped_script(tmp_path, "multi_deployment")
    labels = []

    def run(*args, label):
        labels.append(label)
        return {"receipts": []}

    monkeypatch.setattr(module, "_pass", run)
    monkeypatch.setattr(
        module,
        "_proxy_identities",
        lambda *a: {"dev": {"port": 1111, "attributed_correctly": True}},
    )
    with pytest.raises(common.RemoteError, match="every deployment"):
        module._overlap(
            {},
            {},
            tmp_path,
            {},
            {},
            dict.fromkeys(("dev", "int", "preprod")),
            {"checks": []},
        )
    assert labels == ["overlap", "reverse"]


def test_multi_deployment_cleanup_failure_is_fatal(tmp_path, monkeypatch):
    module, common = shipped_script(tmp_path, "multi_deployment")

    class Cli:
        def run(self, argv, **kwargs):
            return 0, {"deployments": [{"name": "dev"}]} if argv == [
                "deployment",
                "list",
            ] else {}

    monkeypatch.setattr(common, "stop_proxy_runtime", lambda *a: False)
    evidence = {}
    with pytest.raises(common.RemoteError, match="stop every deployment proxy"):
        module._teardown(Cli(), tmp_path, {"dev": "id-dev", "int": "id-int"}, evidence)
    assert evidence["teardown"]["proxies_stopped"] == {"dev": False, "int": False}


def test_multi_deployment_lifecycle_continues_same_tool_sessions(tmp_path, monkeypatch):
    module, common = shipped_script(tmp_path, "multi_deployment")
    state = {"changed": False}

    def run(config, env, session, tool, marker, transcript):
        workspace = pathlib.Path(config["session_cwd"])
        name = session["name"]
        (workspace / f"{name}.ready").touch()
        deadline = time.monotonic() + 5
        while (
            not (workspace / f"{name}.release").exists() and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert state["changed"], "tool continued before lifecycle changes"
        return {
            "tool": tool,
            "exit_code": 1 if name == "dev" else 0,
            "returned_marker": name != "dev",
            "authentication_failed": name == "dev",
        }

    def change(config, cli, sessions, evidence):
        assert all(
            (tmp_path / "lifecycle" / f"{name}.ready").exists() for name in sessions
        )
        state["changed"] = True
        evidence["detail"] = {}

    monkeypatch.setattr(module, "_run_tool", run)
    monkeypatch.setattr(module, "_lifecycle_changes", change)
    monkeypatch.setattr(module, "_access_token", lambda cli, name: name + "-credential")

    def receipts(config, sessions, tokens, runs, *, after):
        assert len(runs) == 2 and all(run["returned_marker"] for run in runs)
        assert state["changed"] and after
        return [{"request_id": "continued-request"}]

    monkeypatch.setattr(module, "_receipts", receipts)
    evidence = {"transcript": [], "checks": []}
    module._lifecycle(
        {"evaluation_id": "test", "inference_timeout_seconds": 5},
        None,
        {},
        tmp_path,
        {name: {"name": name} for name in ("dev", "int", "preprod")},
        evidence,
    )
    assert (
        "existing_model_sessions_continue_after_lifecycle_changes" in evidence["checks"]
    )
    assert evidence["detail"]["continued_usage"] == [
        {"request_id": "continued-request"}
    ]


@pytest.mark.parametrize("primary_failure", [False, True])
def test_multi_deployment_execute_preserves_both_test_and_teardown_failures(
    tmp_path, monkeypatch, primary_failure
):
    module, common = shipped_script(tmp_path, "multi_deployment")
    # Exercise cleanup independently of the unfinished live-limit capability.
    monkeypatch.setattr(module, "_require_model_limits", lambda: None)
    document = dict.fromkeys(module.REQUIRED, "fixture")
    document.update(
        mode="overlap",
        deployments=[
            {
                "name": name,
                "gateway_url": f"https://{name}.example.test",
                "credential_secret_name": name,
            }
            for name in ("dev", "int", "preprod")
        ],
    )
    monkeypatch.setattr(module, "_home", lambda *a: (tmp_path, {}))
    monkeypatch.setattr(
        module,
        "_register",
        lambda cli, entries, evidence, ids: ids.update(
            {entry["name"]: entry["name"] for entry in entries}
        ),
    )
    monkeypatch.setattr(
        module,
        "_login",
        lambda c, cli, env, entry, e: {
            "user_id": "same-id-on-independent-gateways",
            "org_id": "org",
        },
    )
    monkeypatch.setattr(module, "_access_token", lambda cli, name: name + "-credential")
    monkeypatch.setattr(module, "_setup_tools", lambda *a: None)

    def overlap(*args):
        if primary_failure:
            raise common.RemoteError("original inference failure")

    monkeypatch.setattr(module, "_overlap", overlap)

    def fail_cleanup(*args):
        raise common.RemoteError("cleanup failed")

    monkeypatch.setattr(module, "_teardown", fail_cleanup)
    evidence = {"checks": [], "transcript": []}
    expected_error = (
        "original inference failure" if primary_failure else "cleanup failed"
    )
    with pytest.raises(common.RemoteError, match=expected_error):
        module.execute(document, evidence)
    assert evidence["success"] is False
    assert evidence["teardown_error"] == "cleanup failed"


@pytest.mark.parametrize("mode", ["overlap", "lifecycle"])
def test_multi_deployment_direct_execution_blocks_before_setup_or_inference(
    tmp_path, monkeypatch, mode
):
    module, common = shipped_script(tmp_path, "multi_deployment")

    def unexpected(*args, **kwargs):
        pytest.fail("disabled model journey reached setup or inference")

    for name in ("_home", "_register", "_login", "_setup_tools", "_run_tool"):
        monkeypatch.setattr(module, name, unexpected)
    with pytest.raises(common.RemoteError, match="256.*48-request"):
        module.execute({"mode": mode}, {})


def config_fixture(**overrides):
    """A minimal valid config using the approved dev test targets."""
    base = {
        "gateway_url": "https://d1g6cal2ts4iis.cloudfront.net/api",
        "region": "us-east-1",
        "platform_account": "879318057152",
        "destination_account": "605440105851",
        "expected_revision": "91ae8043125c990a349b9acf68b1c604cfdbf18e",
        "vpc_id": "vpc-0d6115bead9301d25",
        "private_subnet_id": "subnet-0860c744097c41a03",
        "cognito_user_pool_id": "us-east-1_JEhv9xSGG",
        # The R3 destination bindings. A run that reaches a mutating suite without
        # these fails on the instance with "No destination role is configured",
        # hours in, so the fixture carries them and a dedicated test below proves
        # their absence is refused up front. These are REFERENCES — a role ARN and
        # a secret NAME — never credential material.
        "destination_role_arn": "arn:aws:iam::605440105851:role/adp-eval-destination",
        "provisioner_role_arn": "arn:aws:iam::605440105851:role/adp-eval-provisioner",
        "credential_secret_name": "adp/cli-uplift-eval/destination-fixture",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Case registry
# --------------------------------------------------------------------------


def test_every_case_present_with_owners():
    """#5199's fifteen, #5413's two, and #5621's capability case.

    Derived from the registry's own numbering rather than a hardcoded range, so
    adding a case to a later story does not have to edit an arithmetic expression
    whose only job was to spell out "consecutive". What the assertion still
    enforces stable, unique, numerically ordered IDs. Gaps are allowed because
    parallel stories reserve their IDs before merging; no case may reuse one.
    """
    identifiers = [case.id for case in cases.CASES]
    assert len(identifiers) == len(set(identifiers))
    assert identifiers == sorted(identifiers, key=lambda value: int(value[1:]))
    assert all(re.fullmatch(r"E[0-9]{2}", value) for value in identifiers)
    assert all(case.owner for case in cases.CASES)
    assert all(case.suite in cases.SUITES for case in cases.CASES)


def test_the_multi_deployment_cases_are_owned_by_5413_and_need_three_deployments():
    """#5413's two cases sit INSIDE the matrix, which is what keeps `full` honest.

    Placed here rather than beside the matrix as C01 is, because BLOCKED is not
    PASSED: with no three-deployment fixture configured these two block, and a
    blocked case keeps full acceptance false until the live evidence is actually
    collected. A checkpoint outside the matrix would have let the epic go green
    with the concurrency requirement never exercised.
    """
    multi = [case for case in cases.CASES if case.suite == "multi-deployment"]

    assert [case.id for case in multi] == ["E16", "E17"]
    assert {case.owner for case in multi} == {"#5413"}
    assert all(cases.THREE_DEPLOYMENTS in case.requires for case in multi)
    assert all(cases.EC2 in case.requires for case in multi)


def test_the_capability_case_cannot_pass_without_a_contrasting_deployment():
    """#5621's E19 is inside the matrix, and its fixture is never assumed.

    The criterion is a CONTRAST — an available operation, a switched-off one and
    an unpermitted one, told apart. A fully-enabled platform with only an admin
    identity would answer "available" to all three, so the case would pass with
    all four capability axes collapsed into one boolean: the exact defect it
    exists to catch. Its own fixture class is therefore required, and while that
    is absent E19 blocks and keeps `full` red.
    """
    case = cases.BY_ID["E19"]

    assert case.owner == "#5621"
    assert cases.CAPABILITY_CONTRAST in case.requires
    assert cases.EC2 in case.requires, "the criterion is live, on a freshly served CLI"

    # Granting every OTHER fixture class must not make it runnable.
    everything_else = {
        cases.PLATFORM,
        cases.EC2,
        cases.COGNITO,
        cases.DESTINATION,
        cases.SECOND_DESTINATION,
        cases.GITHUB_APP,
        cases.GITHUB_REPO,
        cases.HOSTED,
        cases.THREE_DEPLOYMENTS,
        cases.MULTI_DEPLOYMENT_MODEL_LIMITS,
    }
    matrix = cases.new_matrix(FULL)
    blocked = cases.block_missing_fixtures(matrix, everything_else)

    assert blocked["E19"] == [cases.CAPABILITY_CONTRAST]
    assert matrix["E19"]["status"] == cases.BLOCKED


def capability_contrast_config():
    return config.validate(
        config_fixture(
            capability_contrast={
                "disabled_feature": "FEATURE_AGENT_MODELS_ENABLED",
                "enabled_feature": "FEATURE_CONNECTIONS_ENABLED",
                "disabled_operation": "models.catalog.read",
                "enabled_operation": "connections.aws.read",
                "denied_operation": "logs.request.read",
                "foreign_request_id": "request-from-another-tenant",
                "ordinary_fixture_name": "adp/cli-uplift-eval/ordinary-fixture",
            }
        )
    )


def test_capability_contrast_fixture_requires_live_preflight_proof():
    configured = capability_contrast_config()

    assert cases.CAPABILITY_CONTRAST in config.fixture_classes(configured)
    assert cases.CAPABILITY_CONTRAST not in preflight.evaluate_fixtures(configured)
    assert cases.CAPABILITY_CONTRAST in preflight.evaluate_fixtures(
        configured, capability_contrast_available=True
    )


def test_e18_has_a_shipped_remote_driver_and_nightly_purpose():
    assert stages.JOURNEY_DRIVERS["E19"] == "capability_contrast"
    assert "capability_contrast" in bundle.purposes()
    bundle.require_purpose("capability_contrast")


E19_REVISION = "91ae8043125c990a349b9acf68b1c604cfdbf18e"


def test_e18_accepts_only_the_expected_cli_version_and_gateway_revision(tmp_path):
    module, _common = shipped_script(tmp_path, "capability_contrast")
    expected = E19_REVISION
    config_value = {"expected_revision": expected, "expected_cli_version": "1.0.0"}
    capabilities = {"detail": {"gateway": {"state": "yes", "release": expected[:12]}}}

    module._validate_release_evidence(config_value, "adp 1.0.0\n", capabilities)


@pytest.mark.parametrize(
    ("version", "gateway"),
    [
        ("adp 0.9.0", {"state": "yes", "release": E19_REVISION}),
        ("adp 1.0.0", {"state": "unknown", "release": ""}),
        ("adp 1.0.0", {"state": "yes", "release": "deadbee"}),
        ("adp 1.0.0", {"state": "yes", "release": E19_REVISION[:6]}),
    ],
)
def test_e18_rejects_cli_or_gateway_revision_mismatches(tmp_path, version, gateway):
    module, common = shipped_script(tmp_path, "capability_contrast")
    with pytest.raises(common.RemoteError):
        module._validate_release_evidence(
            {"expected_revision": E19_REVISION, "expected_cli_version": "1.0.0"},
            version,
            {"detail": {"gateway": gateway}},
        )


def test_a_blocked_capability_case_keeps_full_acceptance_red():
    """BLOCKED is not PASSED — the rule that makes an honest hold possible.

    Everything else green and E19 blocked must still fail, naming it. Otherwise
    the story could be reported complete with its live criterion never run.
    """
    matrix = cases.new_matrix(FULL)
    # Only E19's own fixture is withheld; every case it does not gate is free to pass.
    cases.block_missing_fixtures(matrix, {cases.EC2, cases.PLATFORM})
    for case_id, entry in matrix.items():
        if entry["status"] == cases.NOT_RUN:
            cases.record(matrix, case_id, cases.PASSED, {})

    status, reasons = cases.accept(matrix, FULL)

    assert status == cases.FAILED
    assert any("E19" in reason for reason in reasons)


def test_the_superplane_case_is_owned_by_5637_and_needs_a_deployed_domain():
    """#5637's live acceptance sits INSIDE the matrix, for #5413's reason.

    The offline contract suite proves every request matches the gateway allowlist
    and the domain's request models. That is a claim about the REQUEST; whether a
    deployed service accepts it is a different claim, and E18 is where the
    difference is recorded. With no domain fixture it blocks, and because BLOCKED
    is not PASSED a `full` run stays red until that evidence exists.

    SUPERPLANE_DOMAIN is its own fixture class rather than part of PLATFORM
    because the gateway is deployed where the domain may not be: folding them
    together would mark E18 runnable whenever the gateway answers, and the case
    would then fail mid-journey on a proxy error, reporting a broken product for
    an absent fixture.
    """
    superplane = [case for case in cases.CASES if case.suite == "superplane"]

    assert [case.id for case in superplane] == ["E18"]
    assert {case.owner for case in superplane} == {"#5637"}
    assert all(cases.SUPERPLANE_DOMAIN in case.requires for case in superplane)
    assert all(cases.EC2 in case.requires for case in superplane)
    assert cases.SUPERPLANE_DOMAIN != cases.PLATFORM


def test_domain_reachability_cannot_enable_e18_without_durable_recovery():
    """A responding gateway cannot supply the missing mutation recovery path."""
    cfg = config.validate(
        config_fixture(
            superplane={
                "base_path": "/superplane/v1",
                "ordinary_session_secret_name": "adp/eval/superplane-ordinary",
                "model_name": "synthetic/e18-model",
                "aws_connection_id": "verified-connection-id",
            }
        )
    )
    assert cases.SUPERPLANE_DOMAIN in config.fixture_classes(cfg)

    calls = []
    for status in (200, 401, 403, 404, 502, 503):
        record = {}
        assert not preflight.check_superplane_domain(
            cfg, record, probe=lambda url: calls.append(url) or status
        )
        assert record["superplane"]["configured"] is True
        assert record["superplane"]["durable_recovery"] is False
        assert (
            record["superplane"]["blocker"]
            == "superplane_durable_recovery_unimplemented"
        )
        assert record["superplane"]["problem"] == cleanup.SUPERPLANE_RECOVERY_BLOCKER
    assert calls == []

    # An unproven result is unavailable, exactly as for the other probed classes.
    assert cases.SUPERPLANE_DOMAIN not in preflight.evaluate_fixtures(cfg)
    assert cases.SUPERPLANE_DOMAIN not in preflight.evaluate_fixtures(
        cfg, superplane_available=True
    )


def test_e18_only_run_blocks_before_allocating_an_instance(tmp_path):
    result = run_live_stages(
        tmp_path,
        extra=("--suite", "superplane"),
        superplane={
            "base_path": "/superplane/v1",
            "ordinary_session_secret_name": "adp/eval/superplane-ordinary",
            "model_name": "synthetic/e18-model",
            "aws_connection_id": "verified-connection-id",
        },
    )
    assert result.code == 1
    assert result.document["matrix"]["E18"]["status"] == cases.BLOCKED
    assert not result.document.get("instance_id")
    assert not any(call[1] == "run_instances" for call in result.ports["aws"].calls)
    assert (
        result.document["preflight"]["superplane"]["blocker"]
        == "superplane_durable_recovery_unimplemented"
    )


def test_direct_e18_dispatch_refuses_before_identity_or_cli_access(
    tmp_path, monkeypatch
):
    module, common = shipped_script(tmp_path, "superplane_domain")

    def unexpected(*args, **kwargs):
        raise AssertionError(
            "an unsupported E18 dispatch reached identity or CLI access"
        )

    monkeypatch.setattr(common, "assert_owned_instance", unexpected)
    monkeypatch.setattr(common, "load_session", unexpected)
    monkeypatch.setattr(common, "Cli", unexpected)
    evidence = {"resources": [["superplane_workspace", "historical-id"]], "removed": []}
    with pytest.raises(common.RemoteError, match="durable recovery is not implemented"):
        module.execute({"superplane": {"durable_recovery": True}}, evidence)

    assert evidence["success"] is False
    assert evidence["stage"] == "recovery_preflight"
    assert evidence["resources"] == [["superplane_workspace", "historical-id"]]
    assert evidence["removed"] == []
    assert evidence["detail"]["message"] == cleanup.SUPERPLANE_RECOVERY_BLOCKER
    assert evidence["detail"]["blocker"] == "superplane_durable_recovery_unimplemented"


def test_historical_e18_resources_remain_outstanding_without_scoped_recovery(tmp_path):
    cfg = config.validate(config_fixture())
    aws = FakeAws()
    deleters = live.wire(cfg, {"aws": aws})["deleters"](cfg)
    manifest = cleanup.Manifest(tmp_path / "manifest.json", "run-e18")
    for kind in cleanup.SUPERPLANE_KINDS:
        manifest.record(kind, kind + "-historical-id")

    ok, results = cleanup.sweep(manifest, deleters)

    assert ok is False
    assert len(results) == len(cleanup.SUPERPLANE_KINDS)
    assert {item["kind"] for item in manifest.outstanding()} == set(
        cleanup.SUPERPLANE_KINDS
    )
    assert all(item["status"] == cleanup.FAILED for item in results)
    assert aws.calls == []


def test_the_superplane_probe_is_refused_a_callable_like_the_others():
    """Every function object is truthy, so a probe passed unrun would claim the
    fixture is available on the strength of never having been checked."""
    cfg = config.validate(config_fixture())
    with pytest.raises(preflight.PreflightError):
        preflight.evaluate_fixtures(cfg, superplane_available=lambda: True)


def test_the_superplane_probe_never_carries_a_token_or_a_secret_name():
    """The probe record goes into the run document, so it holds a status only."""
    cfg = config.validate(
        config_fixture(
            superplane={
                "base_path": "/superplane/v1",
                "ordinary_session_secret_name": "adp/eval/superplane-ordinary",
                "model_name": "synthetic/e18-model",
                "aws_connection_id": "verified-connection-id",
            }
        )
    )
    record = {}
    preflight.check_superplane_domain(cfg, record, probe=lambda _u: 401)

    serialized = json.dumps(record)
    assert "adp/eval/superplane-ordinary" not in serialized
    assert "authorization" not in serialized.lower()
    assert "token" not in serialized.lower()


def test_a_pasted_ordinary_session_is_refused_by_the_config_guard():
    """The fixture is a secret NAME. An ARN or a value must fail offline."""
    for bad in ("arn:aws:secretsmanager:us-east-1:1:secret:x", "https://example/x"):
        with pytest.raises(config.ConfigError):
            config.validate(
                config_fixture(
                    superplane={
                        "base_path": "/superplane/v1",
                        "ordinary_session_secret_name": bad,
                        "model_name": "synthetic/e18-model",
                        "aws_connection_id": "verified-connection-id",
                    }
                )
            )
    # A relative base path would be assembled into a URL that silently resolved
    # against the gateway root.
    with pytest.raises(config.ConfigError):
        config.validate(
            config_fixture(
                superplane={
                    "base_path": "superplane/v1",
                    "ordinary_session_secret_name": "adp/eval/ordinary",
                    "model_name": "synthetic/e18-model",
                    "aws_connection_id": "verified-connection-id",
                }
            )
        )


def test_e18_requires_distinct_non_admin_and_admin_principals(tmp_path):
    import base64

    module, _common = shipped_script(tmp_path, "superplane_domain")

    def token(subject, role, groups=()):
        claims = json.dumps(
            {
                "sub": subject,
                "custom:org_id": "tenant-e18",
                "custom:role": role,
                "cognito:groups": list(groups),
            }
        ).encode()
        encoded = base64.urlsafe_b64encode(claims).decode().rstrip("=")
        return f"header.{encoded}.signature"

    ordinary = module._identity({"id_token": token("ordinary-sub", "member")})
    admin = module._identity(
        {"id_token": token("admin-sub", "platform_admin", ["admins"])}
    )

    assert ordinary["principal_id"] != admin["principal_id"]
    assert ordinary["tenant_id"] == admin["tenant_id"] == "tenant-e18"
    assert ordinary["role"] == "member" and "admins" not in ordinary["groups"]
    assert admin["role"] == "platform_admin" and "admins" in admin["groups"]


def test_e18_reads_the_ordinary_session_before_any_product_mutation(
    tmp_path, monkeypatch
):
    module, common = shipped_script(tmp_path, "superplane_domain")
    values = {
        "access_token": "ordinary-access",
        "id_token": "ordinary-id",
        "refresh_token": "ordinary-refresh",
        "client_id": "ordinary-client",
        "user_pool_id": "us-east-1_ordinary",
        "region": "us-east-1",
        "expires_at": str(int(time.time()) + 3600),
    }
    seen = []

    def fixture_secret(config_value, _env, key):
        seen.append((config_value["credential_secret"], key))
        return values[key]

    monkeypatch.setattr(common, "fixture_secret", fixture_secret)
    fixture = config_fixture(
        superplane={"ordinary_session_secret_name": "adp/eval/ordinary"}
    )
    fixture["sts_endpoint"] = "https://sts-fips.us-east-1.amazonaws.com"
    session = module._ordinary_session(fixture)

    assert session["access_token"] == "ordinary-access"
    assert session["refresh_via"] == "gateway"
    assert {key for _, key in seen} == set(values)
    assert {secret for secret, _ in seen} == {"adp/eval/ordinary"}


def test_e18_workspace_cleanup_waits_for_the_deleted_tombstone(tmp_path, monkeypatch):
    module, common = shipped_script(tmp_path, "superplane_domain")
    responses = iter(
        [
            (200, {"id": "workspace-1", "status": "Teardown"}),
            (200, {"id": "workspace-1", "status": "Deleted"}),
        ]
    )
    calls = []

    def api(config_value, path, token, **kwargs):
        calls.append((config_value, path, token, kwargs))
        return next(responses)

    monkeypatch.setattr(common, "api", api)
    config_value = {"gateway_url": "https://example.test"}
    fixture = {"base_path": "/superplane/v1"}

    assert not module._workspace_deletion_complete(
        config_value, fixture, "token", "workspace-1"
    )
    assert module._workspace_deletion_complete(
        config_value, fixture, "token", "workspace-1"
    )
    assert all(call[3]["expect"] == (200, 404) for call in calls)


def test_e18_runs_a_superplane_command_from_the_rolled_back_copy():
    source = (
        pathlib.Path(__file__).parents[1] / "e2e/cli_uplift/remote/superplane_domain.py"
    ).read_text()

    assert "cli = rollback_cli" in source
    assert 'cli.json(["superplane", "workspace", "list"])' in source


def test_every_case_belongs_to_a_reachable_named_suite():
    """No case may be orphaned: each must be selectable without 'full'."""
    named = set()
    for suite in cases.SUITES:
        if suite != "full":
            named.update(case.id for case in cases.suite_cases(suite))
    assert named == set(cases.BY_ID)


def test_new_matrix_starts_every_case_not_run():
    matrix = cases.new_matrix(FULL)
    # Every registered case, counted from the registry rather than restated as a
    # literal: a `full` matrix that silently omitted a case is the failure worth
    # catching, and a hardcoded number only catches it until someone updates the
    # number instead of the code.
    assert len(matrix) == len(cases.CASES)
    # `CASES`, not `BY_ID`: the latter also carries C01, the login checkpoint that
    # is deliberately NOT a matrix row.
    assert set(matrix) == {case.id for case in cases.CASES}
    assert {entry["status"] for entry in matrix.values()} == {cases.NOT_RUN}


def test_record_rejects_unknown_case_status_and_out_of_matrix():
    matrix = cases.new_matrix(("install",))
    with pytest.raises(ValueError):
        cases.record(matrix, "E99", cases.PASSED)
    with pytest.raises(ValueError):
        cases.record(matrix, "E01", "green")
    with pytest.raises(ValueError):
        # E04 is a personal-aws case, not selected by the install suite.
        cases.record(matrix, "E04", cases.PASSED)


def test_unknown_suite_is_rejected():
    with pytest.raises(ValueError):
        cases.suite_cases("everything")
    with pytest.raises(ValueError):
        cases.resolve_suites(())


# --------------------------------------------------------------------------
# Acceptance aggregation — the false-green guards
# --------------------------------------------------------------------------


def all_passed():
    matrix = cases.new_matrix(FULL)
    for case_id in matrix:
        cases.record(matrix, case_id, cases.PASSED)
    return matrix


def test_full_run_all_passed_accepts():
    status, reasons = cases.accept(all_passed(), FULL)
    assert status == cases.PASSED
    assert reasons == []


def test_single_failed_case_blocks_acceptance():
    matrix = all_passed()
    cases.record(matrix, "E08", cases.FAILED, {"why": "no usage row"})
    status, reasons = cases.accept(matrix, FULL)
    assert status == cases.FAILED
    assert any("failed: E08" in reason for reason in reasons)


def test_blocked_case_cannot_go_green():
    """A blocked case is honest reporting, not a pass."""
    matrix = all_passed()
    cases.record(matrix, "E11", cases.BLOCKED, {"missing_fixtures": ["github_repo"]})
    status, reasons = cases.accept(matrix, FULL)
    assert status == cases.FAILED
    assert any("blocked: E11" in reason for reason in reasons)


def test_not_run_case_cannot_go_green():
    matrix = all_passed()
    matrix["E15"]["status"] = cases.NOT_RUN
    status, reasons = cases.accept(matrix, FULL)
    assert status == cases.FAILED
    assert any("did not run: E15" in reason for reason in reasons)


def test_partial_suite_never_satisfies_full_acceptance():
    """Every case in a named suite passing is still not full acceptance."""
    names = ("install",)
    matrix = cases.new_matrix(names)
    for case_id in matrix:
        cases.record(matrix, case_id, cases.PASSED)
    status, reasons = cases.accept(matrix, names)
    assert status == cases.PASSED
    assert reasons == []
    assert published_report(matrix=matrix, suites=names)["full_acceptance"] is False


def test_union_of_named_suites_covering_everything_is_still_partial():
    """Assembling every named suite is not the same as one full run (E15)."""
    names = tuple(suite for suite in cases.SUITES if suite != "full")
    matrix = cases.new_matrix(names)
    assert set(matrix) == set(cases.BY_ID)  # coverage is complete...
    for case_id in matrix:
        cases.record(matrix, case_id, cases.PASSED)
    status, _ = cases.accept(matrix, names)
    assert status == cases.PASSED
    assert published_report(matrix=matrix, suites=names)["full_acceptance"] is False


def test_empty_matrix_cannot_pass():
    status, reasons = cases.accept({}, FULL)
    assert status == cases.FAILED
    assert any("missing from the matrix" in reason for reason in reasons)


def test_case_dropped_from_matrix_is_detected():
    matrix = all_passed()
    del matrix["E12"]
    status, reasons = cases.accept(matrix, FULL)
    assert status == cases.FAILED
    assert any("E12" in reason for reason in reasons)


def test_block_missing_fixtures_only_blocks_dependent_cases():
    matrix = cases.new_matrix(FULL)
    available = {cases.EC2, cases.PLATFORM, cases.DESTINATION, cases.COGNITO}
    blocked = cases.block_missing_fixtures(matrix, available)
    # GitHub, hosted and multi-deployment cases block; install/admin/routing do not.
    # #5637 adds E18 on the same footing: a domain service that is not deployed is
    # an absent fixture, so it blocks here alongside the GitHub and hosted cases.
    assert set(blocked) == {
        "E07",
        "E09",
        "E10",
        "E11",
        "E12",
        "E16",
        "E17",
        "E18",
        "E19",
        "E39",
        "E42",
        "E25",
        "E27",
    }
    assert matrix["E01"]["status"] == cases.NOT_RUN
    assert matrix["E28"]["status"] == cases.NOT_RUN
    assert matrix["E10"]["status"] == cases.BLOCKED
    assert blocked["E11"] == ["github_app", "github_repo"]
    # #5413: three real deployments are a fixture like any other, so their absence
    # blocks E16/E17 by the same mechanism rather than by a special case — and says
    # which fixture is missing, so an operator knows what to go and create.
    assert blocked["E16"] == [
        cases.MULTI_DEPLOYMENT_MODEL_LIMITS,
        cases.THREE_DEPLOYMENTS,
    ]
    assert matrix["E17"]["status"] == cases.BLOCKED


def test_block_missing_fixtures_does_not_overwrite_a_result():
    matrix = cases.new_matrix(FULL)
    cases.record(matrix, "E10", cases.FAILED, {"why": "real failure"})
    cases.block_missing_fixtures(matrix, {cases.EC2, cases.PLATFORM})
    assert matrix["E10"]["status"] == cases.FAILED


def test_tally_always_reports_every_status_key():
    counts = cases.tally(cases.new_matrix(FULL))
    assert set(counts) == set(cases.STATUSES)
    assert counts[cases.NOT_RUN] == len(cases.CASES)


# --------------------------------------------------------------------------
# Injected failures required by the issue
# --------------------------------------------------------------------------


def test_injected_wrong_account_makes_acceptance_non_successful():
    """A destination-account mismatch must fail config validation outright."""
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(destination_account="879318057152"))


def test_injected_missing_usage_makes_acceptance_non_successful():
    """Inference without a matching ADP usage row is a failed case, not a pass."""
    matrix = all_passed()
    cases.record(
        matrix, "E08", cases.FAILED, {"reasons": ["no ADP usage row for the marker"]}
    )
    status, _ = cases.accept(matrix, FULL)
    assert status == cases.FAILED


def test_injected_cleanup_failure_makes_acceptance_non_successful():
    """Every case can pass and the run still must not be green if cleanup failed."""
    status, reasons = cases.accept(all_passed(), FULL, cleanup_ok=False)
    assert status == cases.FAILED
    assert any("cleanup" in reason for reason in reasons)


# --------------------------------------------------------------------------
# Config validation / EC2-only and ownership guards
# --------------------------------------------------------------------------


def test_valid_config_defaults_are_applied():
    result = config.validate(config_fixture())
    assert result["timeout_seconds"] == 240
    assert result["max_instances"] == 1
    assert result["sts_endpoint"] == "https://sts-fips.us-east-1.amazonaws.com"
    assert (
        result["secrets_endpoint"]
        == "https://secretsmanager-fips.us-east-1.amazonaws.com"
    )


def test_fips_endpoints_are_overridable_but_must_be_https():
    result = config.validate(
        config_fixture(sts_endpoint="https://sts.us-east-1.amazonaws.com")
    )
    assert result["sts_endpoint"] == "https://sts.us-east-1.amazonaws.com"
    with pytest.raises(config.ConfigError):
        config.validate(
            config_fixture(
                secrets_endpoint="http://secretsmanager.us-east-1.amazonaws.com"
            )
        )


@pytest.mark.parametrize("key", config.REQUIRED)
def test_every_required_key_is_enforced(key):
    payload = config_fixture()
    del payload[key]
    with pytest.raises(config.ConfigError):
        config.validate(payload)


def test_secrets_are_refused_anywhere_in_the_config():
    for payload in (
        config_fixture(password="hunter2"),
        config_fixture(github={"org": "x", "app_fixture": "y", "webhook_secret": "z"}),
        config_fixture(nested={"deep": [{"external_id": "abc"}]}),
    ):
        with pytest.raises(config.ConfigError):
            config.validate(payload)


def test_gateway_url_must_be_https_without_credentials():
    for bad in (
        "http://example.com/api",
        "https://user:pw@example.com/api",
        "https://example.com/api?x=1",
    ):
        with pytest.raises(config.ConfigError):
            config.validate(config_fixture(gateway_url=bad))


def test_expected_revision_must_be_a_full_sha():
    for bad in ("91ae804", "main", "z" * 40):
        with pytest.raises(config.ConfigError):
            config.validate(config_fixture(expected_revision=bad))


def test_account_and_region_shapes_are_enforced():
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(platform_account="87931805715"))
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(region="us-east"))
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(vpc_id="notavpc"))
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(private_subnet_id="notasubnet"))


def test_second_destination_must_be_genuinely_distinct():
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(second_destination_account="605440105851"))
    result = config.validate(config_fixture(second_destination_account="111122223333"))
    assert cases.SECOND_DESTINATION in config.fixture_classes(result)


def test_bounds_are_enforced_so_a_stuck_run_cannot_burn_spend():
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(max_instances=99))
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(max_run_minutes=10000))
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(daily_budget_usd=1000))
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(timeout_seconds="240"))
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(daily_budget_usd=True))


def test_instance_ttl_must_outlast_the_run_budget():
    """Otherwise the recovery sweep could terminate a live run's instance."""
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(max_run_minutes=300, instance_ttl_minutes=60))


def test_github_fixtures_absent_blocks_only_github_cases():
    available = config.fixture_classes(config.validate(config_fixture()))
    assert cases.GITHUB_APP not in available
    assert cases.GITHUB_REPO not in available
    assert {cases.EC2, cases.PLATFORM, cases.DESTINATION, cases.COGNITO} <= available
    # Maintenance reads/previews discover the selected deployment's App. Only
    # the separate mutating journeys require a disposable external App fixture.
    assert set(cases.BY_ID["E28"].requires) <= available
    assert cases.GITHUB_APP in cases.BY_ID["E10"].requires


def test_github_fixtures_present_enables_github_cases():
    result = config.validate(
        config_fixture(
            github={
                "org": "adp-eval",
                "app_fixture": "eval-app",
                "repo": "adp-eval/sandbox",
            }
        )
    )
    available = config.fixture_classes(result)
    assert {cases.GITHUB_APP, cases.GITHUB_REPO} <= available


# --------------------------------------------------------------------------
# #5413: the three-deployment fixture
# --------------------------------------------------------------------------
#
# These guards all protect one property: a binding set that COULD NOT prove
# isolation whatever it observed must be refused while the config is being
# validated, not discovered an hour into a live run. Each rule below corresponds
# to a way three bindings can look like three deployments and not be.


def deployment_bindings(**overrides):
    """Three well-formed bindings: distinct names, URLs and credential references."""
    bindings = [
        {
            "name": name,
            "gateway_url": f"https://{name}.example-adp.invalid/api",
            "credential_secret_name": f"adp/cli-uplift-eval/{name}",
        }
        for name in ("development", "integration", "preprod")
    ]
    for index, changes in (overrides.get("entries") or {}).items():
        bindings[index].update(changes)
    return bindings


def test_three_well_formed_deployment_bindings_make_the_fixture_available():
    result = config.validate(config_fixture(deployments=deployment_bindings()))
    assert [entry["name"] for entry in result["deployments"]] == [
        "development",
        "integration",
        "preprod",
    ]
    assert cases.THREE_DEPLOYMENTS in config.fixture_classes(result)


def test_two_deployments_cannot_stand_in_for_three():
    """Two cannot separate "each reached its own" from "they alternated".

    With two deployments and two sessions, a CLI that mixed them up produces the
    same observation as one that did not on half the orderings. Three is the
    smallest set where a crossed request has a wrong destination that is not just
    "the other one".
    """
    with pytest.raises(config.ConfigError, match="exactly 3"):
        config.validate(config_fixture(deployments=deployment_bindings()[:2]))


def test_two_names_for_one_gateway_url_are_refused_as_aliases():
    """This is the defect this guard exists for, and it looks like a valid fixture.

    `adp deployment add` treats a second name for an already-registered URL as an
    ALIAS: one canonical URL, one stable id, one session. Three names over two
    URLs would therefore register, list as three, and satisfy any count — while
    two of them shared the session whose independence is the whole subject of the
    case. Nothing observed later in the run could distinguish that from a pass.
    """
    shared = deployment_bindings()
    shared[2]["gateway_url"] = shared[0]["gateway_url"]
    with pytest.raises(config.ConfigError, match="aliases"):
        config.validate(config_fixture(deployments=shared))
    # A trailing slash is the same URL, so normalisation must happen before the
    # comparison rather than letting punctuation defeat it.
    slashed = deployment_bindings()
    slashed[1]["gateway_url"] = slashed[0]["gateway_url"] + "/"
    with pytest.raises(config.ConfigError, match="aliases"):
        config.validate(config_fixture(deployments=slashed))


def test_a_shared_credential_reference_is_refused():
    """AC-03/AC-11 are about three INDEPENDENT logins.

    One identity signed in three times could not show that logging out of one
    deployment leaves the other two signed in — the logout would either take all
    three or none, and either outcome would be reported as the product's
    behaviour.
    """
    shared = deployment_bindings()
    shared[1]["credential_secret_name"] = shared[0]["credential_secret_name"]
    with pytest.raises(config.ConfigError, match="credential reference"):
        config.validate(config_fixture(deployments=shared))


def test_a_deployment_binding_carries_a_reference_never_a_credential():
    """The fixture password lives in Secrets Manager; this file holds its NAME."""
    for bad in ("arn:aws:secretsmanager:us-east-1:879318057152:secret:x", "https://x"):
        entries = deployment_bindings()
        entries[0]["credential_secret_name"] = bad
        with pytest.raises(config.ConfigError, match="secret NAME"):
            config.validate(config_fixture(deployments=entries))
    # And a credential-shaped KEY anywhere inside a binding is refused outright,
    # by the same structural guard that protects the rest of the tree.
    entries = deployment_bindings()
    entries[0]["password"] = "hunter2"
    with pytest.raises(config.ConfigError, match="looks like a secret"):
        config.validate(config_fixture(deployments=entries))


@pytest.mark.parametrize("bad", ["Development", "1st", "has space", "", "a" * 33])
def test_a_name_the_cli_would_reject_is_refused_before_the_run(bad):
    """Refused here, not by a failing `adp deployment add` an hour in."""
    entries = deployment_bindings()
    entries[0]["name"] = bad
    with pytest.raises(config.ConfigError, match="deployment name"):
        config.validate(config_fixture(deployments=entries))


@pytest.mark.parametrize(
    "url",
    [
        "http://development.example-adp.invalid/api",
        "https://user:pw@development.example-adp.invalid/api",
        "https://development.example-adp.invalid/api?token=x",
    ],
)
def test_a_gateway_url_must_be_plain_https_with_no_credentials(url):
    entries = deployment_bindings()
    entries[0]["gateway_url"] = url
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(deployments=entries))


def test_absent_deployment_bindings_are_absent_not_approximated():
    """No bindings is a legitimate state: E16/E17 block and the rest runs."""
    result = config.validate(config_fixture())
    assert result["deployments"] == []
    assert cases.THREE_DEPLOYMENTS not in config.fixture_classes(result)


def test_the_deployments_overlay_is_parsed_from_its_own_variable():
    """A JSON array needs its own variable; the scalar overlay cannot carry one.

    `OVERLAY` maps one environment variable to one scalar config key, so routing
    a JSON array through it would have written the literal string as a value and
    failed validation with "deployments must be a list" — pointing at the config
    rather than at the variable that was malformed.
    """
    resolved = config.from_environment(
        {config.DEPLOYMENTS_VARIABLE: json.dumps(deployment_bindings())}
    )
    assert [entry["name"] for entry in resolved["deployments"]] == [
        "development",
        "integration",
        "preprod",
    ]
    assert cases.THREE_DEPLOYMENTS in config.fixture_classes(resolved)


def test_a_malformed_deployments_variable_says_so_rather_than_blaming_the_config():
    with pytest.raises(config.ConfigError, match="not valid JSON"):
        config.from_environment(
            {config.DEPLOYMENTS_VARIABLE: "development,integration"}
        )
    with pytest.raises(config.ConfigError, match="JSON array"):
        config.from_environment(
            {config.DEPLOYMENTS_VARIABLE: '{"name": "development"}'}
        )


def test_a_credential_pasted_into_the_deployments_variable_is_refused():
    """Guarded before the merge, so it never reaches the written run config."""
    leaked = deployment_bindings()
    leaked[0]["password"] = "hunter2"
    with pytest.raises(config.ConfigError) as raised:
        config.from_environment({config.DEPLOYMENTS_VARIABLE: json.dumps(leaked)})
    assert "hunter2" not in str(raised.value)


def test_an_empty_deployments_variable_leaves_the_fixture_absent():
    resolved = config.from_environment({config.DEPLOYMENTS_VARIABLE: "   "})
    assert resolved["deployments"] == []


def test_the_multi_deployment_suite_refuses_rather_than_reporting_only_blocks():
    """An operator who asked for E16/E17 by name wants to be told, not handed blocks.

    The mirror of the destination-suite rule: `full` must always produce a graded
    report, so absent deployment bindings block two cases and the rest still runs.
    A dispatch of exactly `multi-deployment` has nothing left to grade, so the
    useful answer is a refusal naming the binding to create.
    """
    cfg = config.validate(config_fixture())
    with pytest.raises(config.ConfigError, match="deployments"):
        config.require_bindings(cfg, ("multi-deployment",))
    # With the fixture bound it starts, and still requires the login credential.
    bound = config.validate(config_fixture(deployments=deployment_bindings()))
    assert "deployments" in config.require_bindings(bound, ("multi-deployment",))


def test_the_per_request_output_bound_cannot_be_raised_past_what_was_authorised():
    """#5413's live run is authorised for 256 output tokens per request."""
    assert config.validate(config_fixture())["max_output_length"] == 256
    with pytest.raises(config.ConfigError, match="capped at 256"):
        config.validate(config_fixture(max_output_length=4096))


def test_an_absence_window_longer_than_the_presence_window_is_refused():
    """An absence proven in longer than a presence takes to appear proves nothing.

    The crossed-request check reads "the marker appeared at its own deployment"
    and "it did not appear at the other two". If the second wait were the longer
    of the two, a request that HAD crossed but was written slowly would be read as
    a clean miss — a false green on the one property E16 exists to test.
    """
    with pytest.raises(config.ConfigError, match="absence_wait_seconds"):
        config.validate(
            config_fixture(absence_wait_seconds=300, usage_wait_seconds=180)
        )


def test_deployment_bindings_are_proven_reachable_before_the_fixture_counts():
    """Configured is not reachable, and an unreachable URL must BLOCK not abort.

    A URL that does not serve the CLI's own discovery document cannot be logged
    in to, so a journey against it would fail inside `adp login` and be recorded
    as a product defect. Checking it read-only in preflight turns that into a
    blocked case naming the binding at fault.
    """
    cfg = config.validate(config_fixture(deployments=deployment_bindings()))
    discovery = {
        "user_pool_id": "us-east-1_JEhv9xSGG",
        "client_id": "c",
        "cli_client_id": "cli",
        "region": "us-east-1",
    }
    record = {}
    assert preflight.check_deployment_bindings(cfg, record, fetch=lambda url: discovery)
    assert len(record["deployments"]["reachable"]) == 3
    assert not record["deployments"]["problems"]

    # One unreachable deployment is enough to block, and the record says which.
    def one_down(url):
        if url.startswith("https://integration."):
            raise preflight.PreflightError(f"{url} is unreachable: URLError")
        return discovery

    record = {}
    assert not preflight.check_deployment_bindings(cfg, record, fetch=one_down)
    assert "integration" in record["deployments"]["problems"]
    assert [entry["name"] for entry in record["deployments"]["reachable"]] == [
        "development",
        "preprod",
    ]


def test_a_url_that_answers_but_is_not_an_adp_gateway_blocks_too():
    """HTTP 200 from something else is the failure a bare reachability check misses."""
    cfg = config.validate(config_fixture(deployments=deployment_bindings()))
    record = {}
    assert not preflight.check_deployment_bindings(
        cfg, record, fetch=lambda url: {"status": "healthy"}
    )
    assert len(record["deployments"]["problems"]) == 3
    assert all(
        "Cognito" in reason for reason in record["deployments"]["problems"].values()
    )


def test_deployments_sharing_an_identity_provider_are_still_a_valid_fixture():
    """Three gateways MAY share a Cognito pool; what must differ is the gateway.

    Requiring distinct pools would refuse the most likely real binding set — one
    organisation's three environments — for no gain, since `validate()` has
    already refused a reused URL and the isolation under test is the CLI's, not
    the identity provider's.
    """
    cfg = config.validate(config_fixture(deployments=deployment_bindings()))
    record = {}
    shared = {
        "user_pool_id": "us-east-1_JEhv9xSGG",
        "client_id": "c",
        "cli_client_id": "cli",
        "region": "us-east-1",
    }
    assert preflight.check_deployment_bindings(cfg, record, fetch=lambda url: shared)
    # Recorded and reported, so a reviewer can see it, but not required to differ.
    assert record["deployments"]["distinct_pools"] == 1


def test_an_unproven_deployment_fixture_is_treated_as_absent():
    """`None` means "not proven", which must never read as available."""
    cfg = config.validate(config_fixture(deployments=deployment_bindings()))
    assert cases.THREE_DEPLOYMENTS not in preflight.evaluate_fixtures(cfg)
    assert cases.THREE_DEPLOYMENTS not in preflight.evaluate_fixtures(
        cfg, deployments_available=False
    )
    assert cases.THREE_DEPLOYMENTS in preflight.evaluate_fixtures(
        cfg, deployments_available=True
    )


def test_a_probe_passed_where_its_result_belongs_is_refused_not_believed():
    """The one direction this must never fail in, closed by construction.

    `live.py` supplies these checks as callables and `stages.py` calls them, so
    handing over the callable itself is a plausible slip — and it would not fail
    loudly. Every function object is truthy, so the fixture would be marked
    AVAILABLE on the strength of never having been checked, and E16/E17 would run
    against bindings nobody had proven reachable. Found by writing the
    False/True guard above with `lambda:` out of habit and watching False pass.
    """
    cfg = config.validate(config_fixture(deployments=deployment_bindings()))
    with pytest.raises(preflight.PreflightError, match="rather than its result"):
        preflight.evaluate_fixtures(cfg, deployments_available=lambda: False)
    with pytest.raises(preflight.PreflightError, match="github_available"):
        preflight.evaluate_fixtures(cfg, github_available=lambda: True)


def test_the_missing_fixture_report_says_what_to_create():
    """ "Blocked" without "on what" makes an operator read the harness source."""
    cfg = config.validate(config_fixture())
    absent = preflight.missing_fixture_report(cfg, preflight.evaluate_fixtures(cfg))
    entry = absent[cases.THREE_DEPLOYMENTS]
    assert "deployments" in entry["needs"]
    assert "E16" in entry["blocks"] and "E17" in entry["blocks"]


def test_reachable_deployments_do_not_enable_unbounded_model_execution():
    cfg = config.validate(config_fixture(deployments=deployment_bindings()))
    available = preflight.evaluate_fixtures(cfg, deployments_available=True)
    assert cases.THREE_DEPLOYMENTS in available
    matrix = cases.new_matrix(("multi-deployment",))
    cases.block_missing_fixtures(matrix, available)
    assert {row["status"] for row in matrix.values()} == {cases.BLOCKED}
    report = preflight.missing_fixture_report(cfg, available)
    missing = report[cases.MULTI_DEPLOYMENT_MODEL_LIMITS]
    assert missing["blocks"] == ["E16", "E17"]
    assert "256" in missing["needs"] and "48-request" in missing["needs"]


def test_harness_pin_is_an_immutable_full_sha():
    """A branch or tag here would break the 'reviewed immutable revision' rule."""
    assert config.REVISION.match(config.HARNESS_COMMIT)


def test_required_cli_config_dir_line_is_the_isolation_contract():
    """BG_CONFIG_DIR is what keeps the worker out of the real config directory."""
    assert "BG_CONFIG_DIR" in config.REQUIRED_CLI_CONFIG_DIR_LINE


def test_load_reports_invalid_json_clearly(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json")
    with pytest.raises(config.ConfigError):
        config.load(str(path))


def test_load_accepts_a_valid_file(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(config_fixture()))
    assert config.load(str(path))["region"] == "us-east-1"


# --------------------------------------------------------------------------
# Reporting, redaction and aggregation
# --------------------------------------------------------------------------


def test_redaction_removes_credential_shaped_values():
    payload = {
        "account_id": "605440105851",
        "password": "hunter2",
        "access_token": "eyJhbGciOiJIUzI1NiJ9.abc.def",
        "external_id": "secret-external",
        "nested": [{"refresh_token": "abc", "input_tokens": 12}],
        "cookie": "session=1",
    }
    clean = report.redact(payload)
    assert clean["account_id"] == "605440105851"
    assert clean["nested"][0]["input_tokens"] == 12  # token COUNTS are evidence
    for banned in ("password", "access_token", "external_id", "cookie"):
        assert banned not in clean
    assert "refresh_token" not in clean["nested"][0]


def test_redaction_scrubs_embedded_jwts_in_free_text():
    text = (
        "authorization failed for eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig here"
    )
    clean = report.redact({"note": text})
    assert "eyJ" not in clean["note"]
    assert "[REDACTED]" in clean["note"]


def test_report_is_serializable_and_carries_revisions_and_correlation():
    matrix = all_passed()
    document = report.build(
        matrix=matrix,
        suites=FULL,
        config=config.validate(config_fixture()),
        evaluation_id="eval-001",
        attempt_id="eval-001-a2",
        cleanup_ok=True,
        timing={
            "started_at": "2026-09-15T00:00:00Z",
            "ended_at": "2026-09-15T01:00:00Z",
        },
        correlation={
            "aws_account": "879318057152",
            "adp_org": "adp-e2e-x",
            "github_repo": "adp-eval/sandbox",
        },
    )
    json.dumps(document)  # must never raise
    assert document["status"] == cases.PASSED
    assert document["expected_revision"] == "91ae8043125c990a349b9acf68b1c604cfdbf18e"
    assert document["evaluation_id"] == "eval-001"
    assert document["attempt_id"] == "eval-001-a2"
    assert document["counts"][cases.PASSED] == len(cases.CASES)
    assert document["correlation"]["adp_org"] == "adp-e2e-x"


def test_report_never_emits_a_secret_even_if_a_case_detail_leaks_one():
    matrix = all_passed()
    cases.record(
        matrix, "E02", cases.PASSED, {"password": "hunter2", "id_token": "eyJa.b.c"}
    )
    document = report.build(
        matrix=matrix,
        suites=FULL,
        config=config.validate(config_fixture()),
        evaluation_id="e",
        attempt_id="a",
        cleanup_ok=True,
        timing={},
        correlation={},
    )
    text = json.dumps(document)
    assert "hunter2" not in text
    assert "eyJa" not in text


def test_report_status_reflects_blocked_and_cleanup():
    matrix = all_passed()
    cases.record(matrix, "E09", cases.BLOCKED, {"missing_fixtures": ["hosted"]})
    document = report.build(
        matrix=matrix,
        suites=FULL,
        config=config.validate(config_fixture()),
        evaluation_id="e",
        attempt_id="a",
        cleanup_ok=False,
        timing={},
        correlation={},
    )
    assert document["status"] == cases.FAILED
    assert document["counts"][cases.BLOCKED] == 1
    assert any("cleanup" in reason for reason in document["reasons"])


def test_junit_marks_blocked_and_not_run_distinctly_and_fails_the_suite():
    matrix = all_passed()
    cases.record(matrix, "E10", cases.BLOCKED, {"missing_fixtures": ["github_app"]})
    cases.record(matrix, "E11", cases.FAILED, {"why": "wrong repo returned"})
    matrix["E12"]["status"] = cases.NOT_RUN
    xml = report.junit(matrix, "eval-001")
    assert f'tests="{len(cases.CASES)}"' in xml
    assert 'failures="1"' in xml
    # Blocked and not-run are skipped-with-reason, never silent passes.
    assert xml.count("<skipped") == 2
    assert "E10" in xml and "github_app" in xml


def test_junit_escapes_xml_metacharacters():
    matrix = cases.new_matrix(("install",))
    cases.record(matrix, "E01", cases.FAILED, {"why": 'a <b> & "c"'})
    xml = report.junit(matrix, "eval-001")
    assert "<b>" not in xml.replace("<b>&", "")
    assert "&lt;b&gt;" in xml or "&amp;" in xml


def test_summary_lists_every_case_with_owner_and_status():
    matrix = all_passed()
    cases.record(matrix, "E09", cases.BLOCKED, {"missing_fixtures": ["hosted"]})
    text = report.summary(matrix, FULL, "eval-001", cleanup_ok=True)
    for case_id in matrix:
        assert case_id in text
    assert "#5185" in text
    assert "hosted" in text


def test_summary_states_partial_scope_explicitly():
    names = ("install",)
    matrix = cases.new_matrix(names)
    for case_id in matrix:
        cases.record(matrix, case_id, cases.PASSED)
    text = report.summary(matrix, names, "eval-001", cleanup_ok=True)
    assert "partial" in text.lower()


def test_sanitize_command_keeps_shape_but_drops_argument_values():
    """Transcripts must show which command ran without leaking its secrets."""
    line = report.sanitize_command(
        [
            "adp",
            "aws",
            "connect",
            "--account",
            "605440105851",
            "--external-id-file",
            "/tmp/x",
            "--yes",
        ]
    )
    assert "adp aws connect" in line
    assert "--account" in line
    assert "/tmp/x" not in line


def test_sanitize_command_rejects_non_list_input():
    with pytest.raises(TypeError):
        report.sanitize_command("adp aws connect --yes")


# --------------------------------------------------------------------------
# The published result schema
# --------------------------------------------------------------------------
#
# report.schema.json is a deliverable other people validate against, so the
# risk is that it drifts from report.build() and quietly becomes fiction. These
# tests make the schema and the builder verify each other in both directions.


def published_report(**overrides):
    """A report assembled the way the runner assembles it, with real-shaped IDs."""
    matrix = overrides.pop("matrix", None) or all_passed()
    payload = {
        "matrix": matrix,
        "suites": FULL,
        "config": config.validate(config_fixture()),
        "evaluation_id": "adp-e2e-20260915-143022-a1b2c3",
        "attempt_id": "adp-e2e-20260915-143022-a1b2c3-a1",
        "cleanup_ok": True,
        "timing": {
            "started_at": "2026-09-15T14:30:22Z",
            "ended_at": "2026-09-15T15:10:00Z",
            "duration_seconds": 2378,
        },
        "correlation": {"aws_account": "879318057152"},
    }
    payload.update(overrides)
    return report.build(**payload)


def test_a_real_report_satisfies_the_published_schema():
    assert report.validate(published_report()) == []


@pytest.mark.parametrize("suite", cases.SUITES)
@pytest.mark.parametrize(
    "status", (cases.PASSED, cases.FAILED, cases.BLOCKED, cases.NOT_RUN)
)
def test_every_supported_suite_and_case_can_be_published(tmp_path, suite, status):
    """Partial diagnostics must publish the same JSON/JUnit contract as full runs."""
    import xml.etree.ElementTree as ET

    matrix = cases.new_matrix((suite,))
    for case_id in matrix:
        cases.record(matrix, case_id, status)
    document = published_report(matrix=matrix, suites=(suite,))
    assert report.validate(document) == []
    paths = report.write(tmp_path, document, matrix, document["evaluation_id"])
    assert json.loads(Path(paths["report"]).read_text()) == document
    assert len(ET.parse(paths["junit"]).findall(".//testcase")) == len(matrix)


@pytest.mark.parametrize("diagnostic", ("D01", "D02", "D03", "D04", "D05", "D06"))
def test_schema_accepts_owned_diagnostic_namespace(diagnostic):
    # D04's guarded source is separately reviewed; schema support must land
    # before its harness so completed remote mutations remain reportable.
    document = published_report()
    document["suites"] = ["knowledge-lifecycle"]
    document["cases"][0]["id"] = diagnostic
    assert report.validate(document) == []


@pytest.mark.parametrize("invalid_id", ("D00", "D07", "E43", "C02", "diagnostic"))
def test_schema_still_rejects_unknown_case_identifiers(invalid_id):
    document = published_report()
    document["cases"][0]["id"] = invalid_id
    assert report.validate(document)


def test_schema_accepts_every_status_the_harness_can_emit():
    """Each of the four verdicts must round-trip; a rejected one is unreportable."""
    for status in (cases.PASSED, cases.FAILED, cases.BLOCKED, cases.NOT_RUN):
        matrix = all_passed()
        matrix["E07"]["status"] = status
        assert report.validate(published_report(matrix=matrix)) == [], status


def test_schema_rejects_a_report_missing_a_required_field():
    document = published_report()
    del document["cleanup"]
    errors = report.validate(document)
    assert any("cleanup" in error for error in errors)


def test_schema_rejects_an_unknown_top_level_field():
    """additionalProperties:false is the guard against a new field carrying a secret."""
    document = published_report()
    document["session_token"] = "should-never-ship"
    errors = report.validate(document)
    assert any("session_token" in error for error in errors)


def test_schema_rejects_a_malformed_case_status_and_id():
    document = published_report()
    document["cases"][0]["status"] = "probably_fine"
    document["cases"][1]["id"] = "E99"
    errors = report.validate(document)
    assert any("probably_fine" in error for error in errors)
    assert any("cases[1].id" in error for error in errors)


def test_schema_rejects_a_short_revision():
    """A 7-char SHA would make results ambiguous about what was tested."""
    document = published_report()
    document["expected_revision"] = "91ae804"
    assert report.validate(document)


def test_schema_rejects_a_boolean_where_a_count_belongs():
    document = published_report()
    document["counts"]["passed"] = True
    assert report.validate(document)


def test_schema_violation_does_not_echo_the_offending_value():
    """Error text is published in logs; a bad value may itself be a credential."""
    document = published_report()
    document["evaluation_id"] = "eyJhbGciOiJIUzI1NiJ9.secret.sig"
    errors = report.validate(document)
    assert errors
    assert not any("eyJ" in error for error in errors)


def test_write_refuses_to_publish_a_report_that_violates_the_schema(tmp_path):
    document = published_report()
    document["status"] = "green-ish"
    with pytest.raises(ValueError, match="report.schema.json"):
        report.write(tmp_path, document, all_passed(), "adp-e2e-20260915-143022-a1b2c3")
    assert not (tmp_path / "report.json").exists()


def test_write_emits_both_artifacts_privately_when_valid(tmp_path):
    document = published_report()
    paths = report.write(
        tmp_path, document, all_passed(), "adp-e2e-20260915-143022-a1b2c3"
    )
    for path in paths.values():
        assert pathlib.Path(path).stat().st_mode & 0o777 == 0o600
    assert json.loads(pathlib.Path(paths["report"]).read_text())["status"] == "passed"


def test_report_schema_uses_only_supported_keywords():
    """The validator covers a subset; if the schema outgrows it, fail loudly here.

    Otherwise an unsupported keyword would be silently ignored and the schema
    would advertise a constraint nothing enforces.
    """
    supported = {
        "$schema",
        "$id",
        "title",
        "description",
        "type",
        "enum",
        "pattern",
        "required",
        "additionalProperties",
        "properties",
        "items",
        "minItems",
        "minimum",
    }
    seen = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties":
                    for sub in value.values():
                        walk(sub)
                    seen.add(key)
                    continue
                seen.add(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(report.load_schema())
    assert seen <= supported, f"unenforced schema keywords: {sorted(seen - supported)}"


def test_schema_documents_that_state_is_never_published():
    text = report.SCHEMA_PATH.read_text()
    assert "never uploaded" in text or "never published" in text


# --------------------------------------------------------------------------
# Preflight — the checks that must abort before anything is mutated
# --------------------------------------------------------------------------

VALID = config.validate(config_fixture())


def test_revision_mismatch_aborts_before_any_mutation(monkeypatch):
    """A green run against the wrong deployment is the failure this prevents."""
    monkeypatch.setattr(preflight, "http_json", lambda *a, **k: {"revision": "0" * 40})
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.check_revision(VALID, {})
    assert "expected revision" in str(excinfo.value)


def test_revision_absent_aborts_rather_than_assuming(monkeypatch):
    monkeypatch.setattr(preflight, "http_json", lambda *a, **k: {"status": "ok"})
    with pytest.raises(preflight.PreflightError):
        preflight.check_revision(VALID, {})


def test_revision_accepts_a_short_prefix_of_the_expected_sha(monkeypatch):
    """Gateways commonly report an abbreviated SHA; that is still a match."""
    monkeypatch.setattr(preflight, "http_json", lambda *a, **k: {"git_sha": "91ae8043"})
    record = {}
    assert preflight.check_revision(VALID, record) == "91ae8043"
    assert record["expected_revision"] == VALID["expected_revision"]


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_served_cli_hash_mismatch_aborts(monkeypatch):
    monkeypatch.setattr(
        preflight.urllib.request,
        "urlopen",
        lambda *a, **k: FakeResponse(b"#!/bin/sh\nreal\n"),
    )
    record = {}
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.check_served_cli_hashes(VALID, {"adp": "0" * 64}, record)
    assert "not serving the release under test" in str(excinfo.value)
    # The mismatched file is NAMED, so an operator knows which artifact is stale
    # rather than only that something is.
    assert "adp" in str(excinfo.value)
    assert record["release_comparison"]["mismatched"] == ["adp"]


def test_served_cli_hash_matches_when_bytes_agree(monkeypatch):
    import hashlib

    payload = b"#!/bin/sh\nreal\n"
    monkeypatch.setattr(
        preflight.urllib.request, "urlopen", lambda *a, **k: FakeResponse(payload)
    )
    digest = hashlib.sha256(payload).hexdigest()
    record = {}
    assert preflight.check_served_cli_hashes(VALID, {"adp": digest}, record) == {
        "adp": digest
    }
    assert record["served_cli_hashes"]["adp"] == digest


def test_misrouted_release_returning_the_spa_is_rejected(monkeypatch):
    """The SPA answers 200 with index.html, which would otherwise hash cleanly."""
    monkeypatch.setattr(
        preflight.urllib.request,
        "urlopen",
        lambda *a, **k: FakeResponse(b"<!doctype html><html>"),
    )
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.check_served_cli_hashes(VALID, {"adp": "0" * 64}, {})
    assert "did not return a script" in str(excinfo.value)


def test_release_manifest_accepts_the_checked_json_contract_as_a_data_artifact():
    revision = "a" * 40
    blobs = {
        f"{release.CLI_DIR}/install.sh": b'#!/bin/sh\nCLI_FILES="adp command-manifest.json"\n',
        f"{release.CLI_DIR}/adp": b'#!/bin/sh\nreadonly ADP_VERSION="1.2.3"\n',
        f"{release.CLI_DIR}/command-manifest.json": b'{"schema_version":"test","commands":[]}',
    }

    def read(_revision, path, **_kwargs):
        return blobs[path]

    result = release.manifest(revision, read=read)

    assert set(result) == {"adp", "command-manifest.json", "install.sh"}
    assert release.cli_version(revision, read=read) == "1.2.3"


def test_served_json_release_artifact_must_be_valid_json(monkeypatch):
    payload = b"not-json"
    monkeypatch.setattr(
        preflight.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(payload),
    )

    with pytest.raises(preflight.PreflightError, match="JSON release artifact"):
        preflight.check_served_cli_hashes(
            VALID,
            {"command-manifest.json": hashlib.sha256(payload).hexdigest()},
            {},
        )


def test_contaminated_config_dir_aborts_the_run():
    """Without BG_CONFIG_DIR the worker can pick up another identity's session."""
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.check_harness_isolation('CONFIG_DIR="${HOME}/.bedrock-gateway"\n', {})
    assert "BG_CONFIG_DIR" in str(excinfo.value)


def test_isolated_config_dir_passes():
    record = {}
    text = "set -euo pipefail\n" + config.REQUIRED_CLI_CONFIG_DIR_LINE + "\n"
    assert preflight.check_harness_isolation(text, record) is True
    assert record["cli_config_dir_isolation"] is True


def test_wrong_account_credentials_abort():
    identities = {
        "platform": {
            "Account": "879318057152",
            "Arn": "arn:aws:sts::879318057152:assumed-role/eval/x",
        },
        "destination": {
            "Account": "999988887777",
            "Arn": "arn:aws:sts::999988887777:assumed-role/eval/x",
        },
    }
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.check_accounts(identities, VALID, {})
    assert "999988887777" in str(excinfo.value)


def test_unresolved_credentials_abort():
    with pytest.raises(preflight.PreflightError):
        preflight.check_accounts({"platform": {"Account": "879318057152"}}, VALID, {})


def test_matching_accounts_are_recorded_for_correlation():
    record = {}
    identities = {
        "platform": {
            "Account": "879318057152",
            "Arn": "arn:aws:sts::879318057152:assumed-role/eval/p",
        },
        "destination": {
            "Account": "605440105851",
            "Arn": "arn:aws:sts::605440105851:assumed-role/eval/d",
        },
    }
    assert preflight.check_accounts(identities, VALID, record) is True
    assert record["destination_account"] == "605440105851"
    assert "assumed-role" in record["platform_arn"]


def test_public_subnet_is_rejected():
    subnet = {
        "VpcId": VALID["vpc_id"],
        "OwnerId": VALID["platform_account"],
        "MapPublicIpOnLaunch": True,
    }
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.check_subnet(subnet, VALID, {})
    assert "private subnet" in str(excinfo.value)


def test_subnet_in_the_wrong_vpc_or_account_is_rejected():
    with pytest.raises(preflight.PreflightError):
        preflight.check_subnet(
            {"VpcId": "vpc-other", "OwnerId": VALID["platform_account"]}, VALID, {}
        )
    with pytest.raises(preflight.PreflightError):
        preflight.check_subnet(
            {"VpcId": VALID["vpc_id"], "OwnerId": "999988887777"}, VALID, {}
        )


def test_private_subnet_in_the_configured_vpc_passes():
    record = {}
    subnet = {
        "VpcId": VALID["vpc_id"],
        "OwnerId": VALID["platform_account"],
        "MapPublicIpOnLaunch": False,
        "AvailabilityZone": "us-east-1a",
    }
    assert preflight.check_subnet(subnet, VALID, record) is True
    assert record["subnet"]["az"] == "us-east-1a"


def test_cognito_pool_mismatch_is_rejected():
    with pytest.raises(preflight.PreflightError):
        preflight.check_cognito(
            {"Id": "us-east-1_OTHERPOOL"}, {"ClientId": "abc"}, VALID, {}
        )


def test_cognito_pool_without_a_client_is_rejected():
    with pytest.raises(preflight.PreflightError) as excinfo:
        preflight.check_cognito({"Id": VALID["cognito_user_pool_id"]}, {}, VALID, {})
    assert "app client" in str(excinfo.value)


def test_cognito_pool_owned_by_another_account_is_rejected():
    pool = {
        "Id": VALID["cognito_user_pool_id"],
        "Arn": "arn:aws:cognito-idp:us-east-1:999988887777:userpool/us-east-1_JEhv9xSGG",
    }
    with pytest.raises(preflight.PreflightError):
        preflight.check_cognito(pool, {"ClientId": "abc"}, VALID, {})


def test_matching_cognito_pool_is_recorded():
    record = {}
    pool = {
        "Id": VALID["cognito_user_pool_id"],
        "Arn": f"arn:aws:cognito-idp:us-east-1:{VALID['platform_account']}:userpool/x",
    }
    assert (
        preflight.check_cognito(pool, {"ClientId": "cli-client"}, VALID, record) is True
    )
    assert record["cognito"] == {
        "user_pool_id": VALID["cognito_user_pool_id"],
        "client_id": "cli-client",
    }


def test_unproven_fixtures_are_treated_as_unavailable():
    """`None` means 'not proven'. Assuming availability would be a false green."""
    configured = config.validate(
        config_fixture(
            github={
                "org": "adp-eval",
                "app_fixture": "eval-app",
                "repo": "adp-eval/sandbox",
            }
        )
    )
    available = preflight.evaluate_fixtures(configured)
    assert cases.GITHUB_APP not in available
    assert cases.GITHUB_REPO not in available


def test_proven_github_fixture_stays_available():
    configured = config.validate(
        config_fixture(
            github={
                "org": "adp-eval",
                "app_fixture": "eval-app",
                "repo": "adp-eval/sandbox",
            }
        )
    )
    available = preflight.evaluate_fixtures(configured, github_available=True)
    assert {cases.GITHUB_APP, cases.GITHUB_REPO} <= available


def test_missing_fixture_report_names_what_to_create_and_what_it_blocks():
    available = preflight.evaluate_fixtures(VALID)
    absent = preflight.missing_fixture_report(VALID, available)
    assert cases.GITHUB_APP in absent
    assert "github.org" in absent[cases.GITHUB_APP]["needs"]
    assert absent[cases.GITHUB_APP]["blocks"]
    # Every blocked case must be a real case ID an operator can look up.
    for entry in absent.values():
        assert set(entry["blocks"]) <= set(cases.BY_ID)


def test_preflight_gate_and_fixture_gate_agree_on_the_matrix():
    """The classes preflight withholds are exactly the ones cases blocks on."""
    available = preflight.evaluate_fixtures(VALID)
    matrix = cases.new_matrix(FULL)
    blocked = cases.block_missing_fixtures(matrix, available)
    absent = preflight.missing_fixture_report(VALID, available)
    expected = {case_id for entry in absent.values() for case_id in entry["blocks"]}
    assert set(blocked) == expected


# --------------------------------------------------------------------------
# EC2 personal-AWS worker (E04/E05) — offline surface only
# --------------------------------------------------------------------------


def test_worker_transcript_keeps_flags_and_drops_values():
    line = personal_aws_worker.sanitize(
        [
            "/tmp/bin/adp",
            "aws",
            "connect",
            "--account",
            "605440105851",
            "--profile",
            "destination",
            "--yes",
        ]
    )
    assert "aws connect" in line
    assert "--account" in line and "--profile" in line
    assert "605440105851" not in line
    assert "destination" not in line


def test_worker_transcript_splits_inline_flag_values():
    line = personal_aws_worker.sanitize(
        ["adp", "aws", "connect", "--external-id=super-secret"]
    )
    assert "--external-id" in line
    assert "super-secret" not in line


def test_worker_refuses_to_run_off_the_owned_instance(monkeypatch):
    """The issue forbids substituting a preconfigured worker for clean EC2."""
    monkeypatch.setattr(
        personal_aws_worker,
        "instance_identity",
        lambda: {"instanceId": "i-someoneelse", "accountId": "879318057152"},
    )
    evidence = {"checks": [], "transcript": []}
    with pytest.raises(RuntimeError) as excinfo:
        personal_aws_worker.execute(
            {"instance_id": "i-ours", "platform_account": "879318057152"}, evidence
        )
    assert "owned EC2 instance" in str(excinfo.value)


def test_worker_main_reports_failure_without_leaking_details(
    monkeypatch, capsys, tmp_path
):
    """A crashed worker must emit a machine-readable, non-successful envelope."""
    path = tmp_path / "worker.json"
    path.write_text(
        json.dumps({"instance_id": "i-ours", "platform_account": "879318057152"})
    )
    monkeypatch.setattr(
        personal_aws_worker,
        "instance_identity",
        lambda: {"instanceId": "i-other", "accountId": "879318057152"},
    )
    monkeypatch.setattr(personal_aws_worker.sys, "argv", ["worker", str(path)])
    assert personal_aws_worker.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["success"] is False
    assert envelope["stage"] == "ec2_identity"


# --------------------------------------------------------------------------
# Cleanup — must survive cancellation and runner loss
# --------------------------------------------------------------------------

PREFIX = "adp-e2e-20260915-120000-ab12cd"

# The two accounts a resource can live in. `cleanup.record()` demands the location
# for any kind that could be in either (R7), so the tests must say which too —
# that is the point: a location is part of the resource's identity, not context.
PLATFORM_ACCOUNT = "879318057152"
DESTINATION_ACCOUNT = "605440105851"
REGION = "us-east-1"


def manifest_fixture(tmp_path, prefix=PREFIX):
    return cleanup.Manifest(tmp_path / "state" / "manifest.json", prefix)


def test_manifest_is_private_on_disk(tmp_path):
    """It names disposable resources; it is never an artifact."""
    manifest = manifest_fixture(tmp_path)
    manifest.record("ec2_instance", "i-1")
    assert manifest.path.stat().st_mode & 0o777 == 0o600
    assert manifest.path.parent.stat().st_mode & 0o077 == 0


def test_manifest_survives_process_loss_and_still_lists_work(tmp_path):
    """The cancellation case: a fresh reader must see what the dead run created."""
    path = tmp_path / "state" / "manifest.json"
    first = cleanup.Manifest(path, PREFIX)
    first.record("ec2_instance", "i-abc")
    first.record("iam_role", "adp-eval-role", account=PLATFORM_ACCOUNT)
    reopened = cleanup.Manifest(path, PREFIX)
    assert [entry["id"] for entry in reopened.outstanding()] == [
        "i-abc",
        "adp-eval-role",
    ]


def test_manifest_record_is_idempotent(tmp_path):
    manifest = manifest_fixture(tmp_path)
    manifest.record("ec2_instance", "i-abc")
    manifest.record("ec2_instance", "i-abc")
    assert len(manifest.read()["resources"]) == 1


def test_manifest_rejects_unknown_kinds_and_empty_ids(tmp_path):
    manifest = manifest_fixture(tmp_path)
    with pytest.raises(ValueError):
        manifest.record("mystery_resource", "x")
    with pytest.raises(ValueError):
        manifest.record("ec2_instance", "")


def test_a_cross_account_resource_cannot_be_recorded_without_its_account(tmp_path):
    """R7: defaulting the location is how the destination stack got platform creds.

    A stack or role can be in either account, so the manifest refuses to record one
    without saying which. The alternative — silently assuming the platform account —
    produces either an AccessDenied the sweep reports as a failure, or a stack left
    standing in the destination account while the run reports clean.
    """
    manifest = manifest_fixture(tmp_path)
    for kind in ("cloudformation_stack", "iam_role", "security_group"):
        with pytest.raises(ValueError) as excinfo:
            manifest.record(kind, f"{kind}-1")
        assert "account it lives in" in str(excinfo.value)
    # An instance is always the harness's own, so it needs no explicit location.
    manifest.record("ec2_instance", "i-1")


def test_a_recorded_location_is_never_silently_discarded(tmp_path):
    """A deleter that cannot take a location must not receive a cross-account one.

    The positional fallback exists so a trivial double stays one lambda. It must
    not become a hole: for a resource with a recorded account, dropping the keyword
    would delete in the wrong account, so the sweep re-raises instead.
    """
    manifest = manifest_fixture(tmp_path)
    manifest.record(
        "cloudformation_stack", "stack-1", account=DESTINATION_ACCOUNT, region=REGION
    )
    ok, results = cleanup.sweep(
        manifest, {"cloudformation_stack": lambda identifier: None}
    )
    assert ok is False
    assert results[0]["status"] == cleanup.FAILED
    assert results[0]["error"] == "TypeError"


def test_sweep_passes_each_resource_its_own_account(tmp_path):
    """Two resources, two accounts, two sessions — not one session used twice."""
    manifest = manifest_fixture(tmp_path)
    manifest.record("iam_role", "platform-role", account=PLATFORM_ACCOUNT)
    manifest.record(
        "cloudformation_stack", "dest-stack", account=DESTINATION_ACCOUNT, region=REGION
    )
    seen = {}

    def note(identifier, *, account=None, region=None):
        seen[identifier] = account

    ok, _ = cleanup.sweep(manifest, {"iam_role": note, "cloudformation_stack": note})
    assert ok is True
    assert seen == {
        "platform-role": PLATFORM_ACCOUNT,
        "dest-stack": DESTINATION_ACCOUNT,
    }


def test_sweep_deletes_in_dependency_order(tmp_path):
    """The instance holds the ENI, so it must go before the role and group."""
    manifest = manifest_fixture(tmp_path)
    manifest.record("iam_role", "role-1", account=PLATFORM_ACCOUNT)
    manifest.record("security_group", "sg-1", account=PLATFORM_ACCOUNT)
    manifest.record("ec2_instance", "i-1")
    order = []
    deleters = {
        kind: (lambda identifier, kind=kind, **location: order.append((kind, location)))
        for kind in ("ec2_instance", "iam_role", "security_group")
    }
    ok, _ = cleanup.sweep(manifest, deleters)
    assert ok is True
    assert [kind for kind, _ in order] == ["ec2_instance", "iam_role", "security_group"]
    # The recorded location reaches the deleter, so it can use that account's
    # session rather than whichever one the sweep happens to be running under.
    assert dict(order)["iam_role"]["account"] == PLATFORM_ACCOUNT
    assert manifest.outstanding() == []


def test_sweep_preserves_reused_resources(tmp_path):
    """Deleting a fixture someone lent us is worse than leaking one we made."""
    manifest = manifest_fixture(tmp_path)
    manifest.record("github_app", "shared-app", reused=True)
    manifest.record("ec2_instance", "i-1")
    deleted = []
    ok, _ = cleanup.sweep(
        manifest, {"ec2_instance": deleted.append, "github_app": deleted.append}
    )
    assert ok is True
    assert deleted == ["i-1"]
    reused = next(
        entry for entry in manifest.read()["resources"] if entry["kind"] == "github_app"
    )
    assert reused["status"] == cleanup.REUSED


def test_sweep_never_touches_another_runs_resources(tmp_path):
    manifest = manifest_fixture(tmp_path)
    manifest.record("ec2_instance", "i-ours")
    document = manifest.read()
    document["resources"].append(
        {
            "kind": "ec2_instance",
            "id": "i-theirs",
            "prefix": "adp-e2e-other",
            "status": cleanup.PENDING,
        }
    )
    manifest._write(document)
    deleted = []
    cleanup.sweep(manifest, {"ec2_instance": deleted.append})
    assert deleted == ["i-ours"]


def test_sweep_failure_makes_cleanup_not_ok_but_still_clears_the_rest(tmp_path):
    manifest = manifest_fixture(tmp_path)
    manifest.record("ec2_instance", "i-1")
    manifest.record("iam_role", "role-1", account=PLATFORM_ACCOUNT)

    def explode(_identifier, **_location):
        raise RuntimeError("stuck")

    def note(identifier, **_location):
        deleted.append(identifier)

    deleted = []
    ok, results = cleanup.sweep(manifest, {"ec2_instance": explode, "iam_role": note})
    assert ok is False  # feeds cases.accept(cleanup_ok=False)
    assert deleted == ["role-1"]  # one stuck resource must not strand the others
    assert any(row["status"] == cleanup.FAILED for row in results)


def test_sweep_error_records_only_the_exception_type(tmp_path):
    """A provider message can quote a token or an ExternalId."""
    manifest = manifest_fixture(tmp_path)
    manifest.record("secret", "adp/eval/session")

    def explode(_identifier):
        raise RuntimeError("AccessDenied for token eyJhbGciOiJIUzI1NiJ9.abc.def")

    cleanup.sweep(manifest, {"secret": explode})
    text = manifest.path.read_text()
    assert "eyJ" not in text
    assert "RuntimeError" in text


def test_sweep_treats_an_unhandled_kind_as_a_failure_not_a_skip(tmp_path):
    """A leak that reports clean is the outcome the issue forbids."""
    manifest = manifest_fixture(tmp_path)
    manifest.record(
        "cloudformation_stack", "adp-eval-stack", account=DESTINATION_ACCOUNT
    )
    ok, results = cleanup.sweep(manifest, {})
    assert ok is False
    assert results[0]["error"] == "no deleter registered"


def test_failed_resources_are_retried_by_a_later_sweep(tmp_path):
    manifest = manifest_fixture(tmp_path)
    manifest.record("ec2_instance", "i-1")
    attempts = []

    def flaky(identifier):
        attempts.append(identifier)
        if len(attempts) == 1:
            raise RuntimeError("throttled")

    assert cleanup.sweep(manifest, {"ec2_instance": flaky})[0] is False
    assert cleanup.sweep(manifest, {"ec2_instance": flaky})[0] is True
    assert attempts == ["i-1", "i-1"]


def test_cleanup_module_cannot_purge_a_queue():
    """Structural enforcement of 'never purge shared queues'."""
    assert not any("purge" in name.lower() for name in dir(cleanup))
    assert "sqs_queue" not in cleanup.ORDER


def test_ttl_recovery_finds_an_abandoned_instance_without_a_manifest():
    """The runner-loss case: no manifest entry ever got written."""
    now = 1_000_000
    instances = [
        {
            "InstanceId": "i-abandoned",
            "State": {"Name": "running"},
            "LaunchTime": now - 5 * 3600,
            "Tags": [{"Key": cleanup.OWNER_TAG, "Value": PREFIX}],
        }
    ]
    expired = cleanup.expired_instances(instances, now=now, ttl_minutes=240)
    assert [item["id"] for item in expired] == ["i-abandoned"]


def test_ttl_recovery_leaves_a_live_run_alone():
    now = 1_000_000
    instances = [
        {
            "InstanceId": "i-live",
            "State": {"Name": "running"},
            "LaunchTime": now - 600,
            "Tags": [{"Key": cleanup.OWNER_TAG, "Value": PREFIX}],
        }
    ]
    assert cleanup.expired_instances(instances, now=now, ttl_minutes=240) == []


def test_ttl_recovery_ignores_untagged_and_foreign_instances():
    now = 1_000_000
    old = now - 99 * 3600
    instances = [
        {
            "InstanceId": "i-someone-elses-prod",
            "State": {"Name": "running"},
            "LaunchTime": old,
            "Tags": [],
        },
        {
            "InstanceId": "i-other-service",
            "State": {"Name": "running"},
            "LaunchTime": old,
            "Tags": [{"Key": "Name", "Value": "web"}],
        },
        {
            "InstanceId": "i-other-eval",
            "State": {"Name": "running"},
            "LaunchTime": old,
            "Tags": [{"Key": cleanup.OWNER_TAG, "Value": "adp-e2e-zzz"}],
        },
    ]
    assert (
        cleanup.expired_instances(instances, now=now, ttl_minutes=240, prefix=PREFIX)
        == []
    )
    # Unscoped (the scheduled sweep) still only takes tagged evaluation instances.
    assert [
        item["id"]
        for item in cleanup.expired_instances(instances, now=now, ttl_minutes=240)
    ] == ["i-other-eval"]


def test_ttl_recovery_is_idempotent_over_terminated_instances():
    now = 1_000_000
    instances = [
        {
            "InstanceId": "i-gone",
            "State": {"Name": "terminated"},
            "LaunchTime": now - 99 * 3600,
            "Tags": [{"Key": cleanup.OWNER_TAG, "Value": PREFIX}],
        }
    ]
    assert cleanup.expired_instances(instances, now=now, ttl_minutes=240) == []


def test_ttl_recovery_will_not_terminate_on_an_unknown_launch_time():
    now = 1_000_000
    instances = [
        {
            "InstanceId": "i-unknown",
            "State": {"Name": "running"},
            "Tags": [{"Key": cleanup.OWNER_TAG, "Value": PREFIX}],
        }
    ]
    assert cleanup.expired_instances(instances, now=now, ttl_minutes=240) == []


def test_recovery_report_flags_an_instance_it_could_not_terminate():
    expired = [
        {"id": "i-1", "prefix": PREFIX, "age_minutes": 300},
        {"id": "i-2", "prefix": PREFIX, "age_minutes": 300},
    ]
    report_document = cleanup.recovery_report(
        expired, {"i-1": "terminated", "i-2": "running"}, ttl_minutes=240
    )
    assert report_document["outstanding"] == ["i-2"]
    assert report_document["clean"] is False


def test_recovery_report_is_not_clean_when_an_instance_is_still_running():
    """A terminate call that returned 0 does not prove the instance stopped.

    `DisableApiTermination` (and a stuck lifecycle hook) make
    `terminate-instances` exit 0 while the instance keeps running and billing.
    The sweep must decide `clean` from re-read state, so this is the exact
    silent-cost case: every requested termination "succeeded" and the account
    is still dirty.
    """
    expired = [{"id": "i-protected", "prefix": PREFIX, "age_minutes": 300}]

    report_document = cleanup.recovery_report(
        expired, {"i-protected": "running"}, ttl_minutes=240
    )

    assert report_document["clean"] is False
    assert report_document["outstanding"] == ["i-protected"]
    assert report_document["terminated"] == []


def test_recovery_report_does_not_count_shutting_down_as_terminated():
    """`shutting-down` is a direction, not a finished teardown — still billable."""
    expired = [{"id": "i-1", "prefix": PREFIX, "age_minutes": 300}]

    report_document = cleanup.recovery_report(
        expired, {"i-1": "shutting-down"}, ttl_minutes=240
    )

    assert report_document["clean"] is False
    assert report_document["outstanding"] == ["i-1"]


def test_recovery_report_is_clean_only_on_observed_termination():
    expired = [{"id": "i-1", "prefix": PREFIX, "age_minutes": 300}]

    report_document = cleanup.recovery_report(
        expired, {"i-1": "terminated"}, ttl_minutes=240
    )

    assert report_document["clean"] is True
    assert report_document["terminated"] == ["i-1"]
    assert report_document["outstanding"] == []


def test_launch_epoch_reads_ec2_stamps_as_utc_in_any_runner_timezone():
    """EC2 `LaunchTime` is UTC; parsing it as local time skews every age.

    `time.mktime(time.strptime(...))` shifts the stamp by the runner's offset:
    +420 min under US/Pacific, -540 under Asia/Tokyo. Shifted one way a fresh
    instance reports a NEGATIVE age, which no positive TTL expires, so the
    sweep walks past a running instance and calls the account clean. Masked
    today only because ARC runners are UTC and nothing pins TZ.
    """
    stamp = "2026-09-16T06:00:00.000Z"
    expected = calendar.timegm(
        time.strptime("2026-09-16T06:00:00", "%Y-%m-%dT%H:%M:%S")
    )

    original = os.environ.get("TZ")
    try:
        for zone in ("UTC", "America/Los_Angeles", "Asia/Tokyo"):
            os.environ["TZ"] = zone
            time.tzset()
            assert cleanup.launch_epoch(stamp) == expected, zone
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_expired_instances_expires_a_string_stamp_outside_utc():
    """The end-to-end consequence: TTL must hold on a non-UTC runner.

    The pre-existing sweep tests all pass `LaunchTime` as a float, so the
    workflow's string conversion was never exercised. This drives the string
    path under a non-UTC TZ, where the local-time bug reported a negative age
    and expired nothing.
    """
    launched = "2026-09-16T00:00:00.000Z"
    now = calendar.timegm(time.strptime("2026-09-16T06:00:00", "%Y-%m-%dT%H:%M:%S"))
    instances = [
        {
            "InstanceId": "i-old",
            "State": {"Name": "running"},
            "LaunchTime": launched,
            "Tags": [{"Key": cleanup.OWNER_TAG, "Value": PREFIX}],
        }
    ]

    original = os.environ.get("TZ")
    try:
        for zone in ("UTC", "America/Los_Angeles", "Asia/Tokyo"):
            os.environ["TZ"] = zone
            time.tzset()
            expired = cleanup.expired_instances(instances, now=now, ttl_minutes=240)
            assert [item["id"] for item in expired] == ["i-old"], zone
            assert expired[0]["age_minutes"] == 360, zone
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_cleanup_failure_propagates_into_acceptance(tmp_path):
    """End to end: a stuck resource must stop a fully-passing run going green."""
    manifest = manifest_fixture(tmp_path)
    manifest.record("ec2_instance", "i-1")

    def explode(_identifier):
        raise RuntimeError("stuck")

    ok, _ = cleanup.sweep(manifest, {"ec2_instance": explode})
    status, reasons = cases.accept(all_passed(), FULL, cleanup_ok=ok)
    assert status == cases.FAILED
    assert any("cleanup" in reason for reason in reasons)


# --------------------------------------------------------------------------
# Orchestration: attempts, resume, cancellation, fault injection
# --------------------------------------------------------------------------

NOW = 1_800_000_000


def write_config(tmp_path, **overrides):
    path = tmp_path / "eval.json"
    path.write_text(json.dumps(config_fixture(**overrides)))
    return str(path)


def pass_everything(ctx):
    for case_id in ctx["matrix"]:
        ctx["record"](case_id, cases.PASSED, {"proved_by": "offline double"})


def run_cli(tmp_path, stages, *, mode="start", suite=None, extra=(), clock=None):
    """Drive the runner with a COMPLETE stage mapping.

    Stages the caller does not name are filled with no-ops, because a stage absent
    from the mapping is now a hard failure (that is the point of finding 1's fix:
    the workflow supplied nothing and every stage was silently skipped). These
    tests are about orchestration — resume, deadlines, cancellation, fault
    injection — so they want "every other stage was fine", which is what a no-op
    expresses. Tests that care about a MISSING stage build the mapping directly.
    """
    complete = {name: (lambda _ctx: None) for name in runner.STAGES}
    complete["cleanup"] = lambda _ctx: True
    complete.update(stages)
    argv = [
        "--mode",
        mode,
        "--config",
        write_config(tmp_path),
        "--state-dir",
        str(tmp_path / STATE),
        *extra,
    ]
    for name in suite or []:
        argv += ["--suite", name]
    return runner.main(argv, stages=complete, clock=clock or (lambda: NOW))


STATE = "state"


def test_full_run_with_everything_passing_exits_zero(tmp_path, capsys):
    code = run_cli(tmp_path, {"journeys": pass_everything, "cleanup": lambda ctx: True})
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == cases.PASSED
    assert payload["full_acceptance"] is True


def test_report_and_junit_are_written_privately(tmp_path):
    run_cli(tmp_path, {"journeys": pass_everything, "cleanup": lambda ctx: True})
    out = tmp_path / STATE / "out"
    assert (out / "report.json").stat().st_mode & 0o777 == 0o600
    assert (out / "results.xml").exists()


def test_state_file_is_private_and_gitignored(tmp_path):
    """It holds disposable passwords and tokens until cleanup."""
    run_cli(tmp_path, {"journeys": pass_everything, "cleanup": lambda ctx: True})
    state_path = tmp_path / STATE / runner.STATE_FILE
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / STATE).stat().st_mode & 0o077 == 0
    assert (
        ".adp-eval/" in (pathlib.Path(__file__).parents[2] / ".gitignore").read_text()
    )


def test_a_crashed_stage_leaves_unreached_cases_as_not_run_not_passed(tmp_path):
    """Silence must never read as success."""

    def explode(_ctx):
        raise RuntimeError("EC2 launch failed")

    code = run_cli(tmp_path, {"ec2": explode, "cleanup": lambda ctx: True})
    assert code == 1
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert document["stages"]["ec2"] == "failed"
    assert {entry["status"] for entry in document["matrix"].values()} == {cases.NOT_RUN}
    assert document["status"] == cases.FAILED


def test_stage_error_text_from_a_foreign_exception_is_not_stored(tmp_path):
    def explode(_ctx):
        raise ValueError("token eyJhbGciOiJIUzI1NiJ9.abc.def leaked")

    run_cli(tmp_path, {"providers": explode, "cleanup": lambda ctx: True})
    text = (tmp_path / STATE / runner.STATE_FILE).read_text()
    assert "eyJ" not in text
    assert "ValueError" in text


def test_deadline_stops_the_run_before_the_next_stage(tmp_path):
    """Bounded time: an overrunning run must stop, not keep spending."""
    clock = iter(
        [NOW, NOW, NOW + 10**6, NOW + 10**6, NOW + 10**6, NOW + 10**6, NOW + 10**6]
    )
    reached = []
    code = run_cli(
        tmp_path,
        {
            "preflight": lambda ctx: reached.append("preflight"),
            "journeys": pass_everything,
            "cleanup": lambda ctx: True,
        },
        clock=lambda: next(clock),
    )
    assert code == 1
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert "timed_out" in document["stages"].values()


def test_resume_preserves_passed_results_and_retries_the_rest(tmp_path):
    def pass_install_only(ctx):
        for case_id in ("E01", "E02"):
            ctx["record"](case_id, cases.PASSED, {"attempt": ctx["attempt_id"]})
        raise RuntimeError("interrupted")

    run_cli(tmp_path, {"journeys": pass_install_only, "cleanup": lambda ctx: True})
    first = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert first["matrix"]["E01"]["status"] == cases.PASSED

    seen = {}

    def finish(ctx):
        seen.update(ctx["matrix"])
        for case_id in ctx["matrix"]:
            if ctx["matrix"][case_id]["status"] != cases.PASSED:
                ctx["record"](case_id, cases.PASSED, {"attempt": ctx["attempt_id"]})

    code = run_cli(
        tmp_path, {"journeys": finish, "cleanup": lambda ctx: True}, mode="resume"
    )
    assert code == 0
    second = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert (
        second["evaluation_id"] == first["evaluation_id"]
    )  # immutable across attempts
    assert second["attempt_id"] == first["evaluation_id"] + "-a2"
    assert len(second["attempts"]) == 2
    # The prior pass was visible to the resumed attempt, not recomputed.
    assert seen["E01"]["status"] == cases.PASSED
    assert seen["E01"]["detail"]["attempt"].endswith("-a1")


def test_resume_does_not_inherit_a_prior_failure_as_a_verdict(tmp_path):
    """A failed case must be re-proven, not carried forward either way."""

    def fail_one(ctx):
        pass_everything(ctx)
        ctx["record"]("E08", cases.FAILED, {"why": "no usage row"})

    run_cli(tmp_path, {"journeys": fail_one, "cleanup": lambda ctx: True})
    observed = {}

    def observe(ctx):
        observed.update(
            {case_id: entry["status"] for case_id, entry in ctx["matrix"].items()}
        )
        ctx["record"]("E08", cases.PASSED, {"why": "usage row present"})

    code = run_cli(
        tmp_path, {"journeys": observe, "cleanup": lambda ctx: True}, mode="resume"
    )
    assert observed["E08"] == cases.NOT_RUN  # re-proven, not inherited
    assert code == 0


def test_resume_reproves_the_deployed_revision(tmp_path):
    """An attempt hours later must re-read /health, not trust attempt 1's reading."""
    checks = []
    stages = {
        "preflight": lambda ctx: checks.append(ctx["attempt_id"]),
        "journeys": pass_everything,
        "cleanup": lambda ctx: True,
    }
    run_cli(tmp_path, stages)
    run_cli(tmp_path, stages, mode="resume")
    assert [attempt.split("-")[-1] for attempt in checks] == ["a1", "a2"]


def test_resume_refuses_a_different_target_revision(tmp_path):
    """Otherwise results from revision A get reported as acceptance of B."""
    stages = {"journeys": pass_everything, "cleanup": lambda ctx: True}
    run_cli(tmp_path, stages)
    other = tmp_path / "other.json"
    other.write_text(json.dumps(config_fixture(expected_revision="b" * 40)))
    with pytest.raises(runner.RunnerError) as excinfo:
        runner.main(
            [
                "--mode",
                "resume",
                "--config",
                str(other),
                "--state-dir",
                str(tmp_path / STATE),
            ],
            stages=stages,
            clock=lambda: NOW,
        )
    assert "different target or revision" in str(excinfo.value)


def test_resume_refuses_a_different_suite_selection(tmp_path):
    stages = {"journeys": pass_everything, "cleanup": lambda ctx: True}
    run_cli(tmp_path, stages, suite=["install"])
    with pytest.raises(runner.RunnerError):
        run_cli(tmp_path, stages, mode="resume", suite=["admin"])


def test_resume_without_state_is_an_error(tmp_path):
    with pytest.raises(runner.RunnerError):
        run_cli(tmp_path, {}, mode="resume")


def test_start_refuses_to_clobber_an_unfinished_run(tmp_path):
    def fail_one(ctx):
        ctx["record"]("E01", cases.FAILED, {"why": "x"})

    run_cli(tmp_path, {"journeys": fail_one, "cleanup": lambda ctx: True})
    with pytest.raises(runner.RunnerError) as excinfo:
        run_cli(tmp_path, {"journeys": pass_everything, "cleanup": lambda ctx: True})
    assert "use --mode resume" in str(excinfo.value)


def test_concurrent_attempts_on_one_state_dir_are_refused(tmp_path):
    state = runner.State(tmp_path / STATE)
    held = state.lock()
    try:
        with pytest.raises(runner.RunnerError) as excinfo:
            run_cli(tmp_path, {"journeys": pass_everything})
        assert "Another attempt" in str(excinfo.value)
    finally:
        held.close()


def test_partial_suite_can_pass_without_claiming_full_acceptance(tmp_path, capsys):
    code = run_cli(
        tmp_path,
        {"journeys": pass_everything, "cleanup": lambda ctx: True},
        suite=["install"],
    )
    assert code == 0  # checkpoint success is distinct from full acceptance
    payload = json.loads(capsys.readouterr().out)
    assert payload["full_acceptance"] is False


def test_outstanding_manifest_resources_fail_cleanup_even_if_the_stage_says_ok(
    tmp_path,
):
    """A cleanup stage that returns True while resources remain is not trusted."""

    def leak(ctx):
        ctx["manifest"].record("ec2_instance", "i-leaked")
        pass_everything(ctx)

    code = run_cli(tmp_path, {"journeys": leak, "cleanup": lambda ctx: True})
    assert code == 1
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert document["cleanup_ok"] is False
    assert document["cleanup_outstanding"] == ["ec2_instance:i-leaked"]
    assert any("cleanup" in reason for reason in document["reasons"])


def test_cleanup_runs_even_when_an_earlier_stage_failed(tmp_path):
    swept = []

    def explode(_ctx):
        raise RuntimeError("journey crashed")

    run_cli(
        tmp_path,
        {
            "journeys": explode,
            "cleanup": lambda ctx: swept.append(ctx["prefix"]) or True,
        },
    )
    assert len(swept) == 1


def test_cleanup_mode_sweeps_an_abandoned_state_directory(tmp_path, capsys):
    """The operator's recovery path after a cancelled workflow."""

    def leak(ctx):
        ctx["manifest"].record("ec2_instance", "i-abandoned")
        pass_everything(ctx)

    run_cli(tmp_path, {"journeys": leak, "cleanup": lambda ctx: True})
    capsys.readouterr()

    def sweep(ctx):
        return cleanup.sweep(
            ctx["manifest"], {"ec2_instance": lambda identifier: None}
        )[0]

    code = run_cli(tmp_path, {"cleanup": sweep}, mode="cleanup")
    assert code == 0
    assert json.loads(capsys.readouterr().out)["cleanup"] == "complete"


def test_cleanup_mode_on_an_absent_state_dir_is_a_no_op(tmp_path, capsys):
    assert run_cli(tmp_path, {}, mode="cleanup") == 0
    assert json.loads(capsys.readouterr().out)["state"] == "absent"


def test_status_mode_reports_progress_without_publishing_state(tmp_path, capsys):
    def partial(ctx):
        ctx["record"](
            "E01", cases.PASSED, {"session_token": "eyJa.b.c", "password": "hunter2"}
        )

    run_cli(tmp_path, {"journeys": partial, "cleanup": lambda ctx: True})
    capsys.readouterr()
    assert run_cli(tmp_path, {}, mode="status") == 0
    text = capsys.readouterr().out
    assert "hunter2" not in text and "eyJa" not in text
    view = json.loads(text)
    assert view["counts"][cases.PASSED] == 1
    assert view["harness_commit"] == config.HARNESS_COMMIT


def test_status_mode_on_an_absent_run_says_so(tmp_path, capsys):
    assert run_cli(tmp_path, {}, mode="status") == 0
    assert json.loads(capsys.readouterr().out) == {"state": "absent"}


def test_evaluation_id_is_the_ownership_prefix_every_stage_sees(tmp_path):
    seen = {}
    run_cli(
        tmp_path,
        {
            "journeys": lambda ctx: seen.update(ctx) or pass_everything(ctx),
            "cleanup": lambda ctx: True,
        },
    )
    assert seen["prefix"] == seen["evaluation_id"]
    assert seen["prefix"].startswith("adp-e2e-")
    assert seen["manifest"].prefix == seen["prefix"]


def test_malformed_operator_supplied_evaluation_id_is_refused_before_any_mutation(
    tmp_path,
):
    """The ID is the cleanup tag AND a schema-validated field; validate it early.

    If it were only caught at publish time, the run would already have launched
    instances tagged with a value the recovery sweep's filter does not expect,
    and the report for that spend would be lost to a ValueError.
    """
    reached = []
    with pytest.raises(runner.RunnerError, match="adp-e2e-"):
        run_cli(
            tmp_path,
            {"journeys": lambda ctx: reached.append(1) or pass_everything(ctx)},
            extra=["--evaluation-id", "my-test-run"],
        )
    assert reached == []
    assert not (tmp_path / STATE / "state.json").exists()


def test_generated_evaluation_ids_are_accepted_by_their_own_guard():
    """The generator and the guard must agree, or every fresh run would abort."""
    for offset in (0, 1, 61, 3600, 86_400):
        runner.check_evaluation_id(runner.new_evaluation_id(NOW + offset, offset))


def test_unknown_suite_is_rejected_before_state_is_created(tmp_path):
    with pytest.raises(ValueError):
        run_cli(tmp_path, {}, suite=["everything"])
    assert not (tmp_path / STATE / runner.STATE_FILE).exists()


def test_only_bounded_faults_are_accepted():
    assert "none" in runner.FAULTS
    with pytest.raises(SystemExit):  # argparse rejects an invented fault
        runner.parse_args(["--config", "c.json", "--fault", "delete_production"])


def test_injected_fault_is_visible_to_stages_and_recorded(tmp_path):
    """Fault injection must be a first-class, reported input, not a hidden flag."""
    seen = {}

    def stage(ctx):
        seen["fault"] = ctx["fault"]
        pass_everything(ctx)

    run_cli(
        tmp_path,
        {"journeys": stage, "cleanup": lambda ctx: True},
        extra=["--fault", "missing_usage"],
    )
    assert seen["fault"] == "missing_usage"
    assert (
        json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())["fault"]
        == "missing_usage"
    )


def test_new_evaluation_ids_are_unique_and_time_ordered():
    first = runner.new_evaluation_id(NOW, "a")
    second = runner.new_evaluation_id(NOW + 61, "b")
    assert first != second
    assert first < second  # lexical order follows time, so listings sort sensibly


# --------------------------------------------------------------------------
# Stage wiring: the entry point must run the real stages (finding 1)
# --------------------------------------------------------------------------
#
# The regression these close is specific. `main()` used to pass `stages or {}`
# to the Evaluation, so the workflow -- which supplies no mapping -- ran a stage
# loop that skipped all seven stages and published a structurally healthy report
# containing fifteen `not_run` rows and an empty preflight. No fixture, secret or
# merge could have made that invocation provision an instance. The tests below
# drive the SAME entry point Actions drives, with transports doubled, so the
# wiring itself is covered rather than only the pure grading logic.


class FakeAws:
    """Records every AWS call and replies from a canned table.

    Deliberately not a MagicMock: an unstubbed operation must raise, because a
    permissive double is how a stage that never really called AWS passes. The
    `calls` list is the assertion surface for "did the real stage run".
    """

    def __init__(self, replies=None, region="us-east-1"):
        self.replies = dict(replies or {})
        self.region = region
        self.calls = []
        self.assumed = []

    def call(self, service, operation, **kwargs):
        self.calls.append((service, operation, kwargs))
        key = f"{service}.{operation}"
        if key not in self.replies:
            raise ports.PortError(f"{key} was not stubbed")
        reply = self.replies[key]
        return reply(**kwargs) if callable(reply) else reply

    def assume(self, role_arn, session_name, **_kwargs):
        """A session in the ROLE's account, as a real assumed-role session is.

        The account has to change: if an assumed session kept reporting the
        caller's own account, preflight's "both accounts resolve and differ" check
        would be satisfiable without any cross-account access existing — the exact
        substitution `_identity` is written to prevent.
        """
        self.assumed.append((role_arn, session_name))
        parts = role_arn.split(":")
        account = parts[4] if len(parts) > 4 else self.region
        role = role_arn.rsplit("/", 1)[-1]
        replies = dict(self.replies)
        replies["sts.get_caller_identity"] = {
            "Account": account,
            "Arn": f"arn:aws:sts::{account}:assumed-role/{role}/{session_name}",
        }
        assumed = FakeAws(replies, self.region)
        assumed.calls = self.calls  # one call log, so cross-account calls are visible
        return assumed


class FakeHttp:
    """Gateway doubles keyed by URL suffix. An unknown URL is a failure."""

    def __init__(self, json_by_suffix=None, bytes_by_suffix=None, *, gateway=None):
        self.json_by_suffix = dict(json_by_suffix or {})
        self.bytes_by_suffix = dict(bytes_by_suffix or {})
        self.gateway = gateway
        self.urls = []

    def _lookup(self, url, table):
        self.urls.append(url)
        for suffix, value in table.items():
            if url.endswith(suffix):
                return value
        raise ports.PortError(f"{url} was not stubbed")

    def request(self, url, *, method="GET", token=None, body=None, expect=200):
        self.urls.append(url)
        answered = (
            self.gateway.handle(url, method, token=token) if self.gateway else None
        )
        if answered is None:
            raise ports.PortError(f"{method} {url} was not stubbed")
        status, payload = answered
        if expect is not None and status != expect:
            raise ports.PortError(f"{url} returned HTTP {status}, expected {expect}")
        return status, payload

    def get(self, url, *, token=None, expect=200):
        if self.gateway is not None:
            answered = self.gateway.handle(url, "GET", token=token)
            if answered is not None:
                self.urls.append(url)
                status, payload = answered
                if expect is not None and status != expect:
                    raise ports.PortError(
                        f"{url} returned HTTP {status}, expected {expect}"
                    )
                return status, payload
        return expect, self._lookup(url, self.json_by_suffix)

    def get_bytes(self, url, *, expect=200):
        return self._lookup(url, self.bytes_by_suffix)


class FakeGateway:
    """The Bedrock-routing API surface cleanup depends on, with its real semantics.

    Modelled rather than stubbed because the property under test is a sequence:
    a destination cannot be unlinked while a rule names it, so the deleter must
    remove the rules first and then the row. A per-URL stub would answer 204 to
    both calls in any order and prove none of that.

    The one refusal that matters most is faithful to the product: `DELETE
    /connection-links/{id}` serves only rows that have a connection GRANT, i.e.
    ones created by `POST /connection-links`. A destination registered by `adp
    admin bedrock connect` (`POST /destinations`, source `new_account`) has no
    grant, so the gateway answers 404 — and there is no other endpoint that
    deletes it. `grant=False` rows are how that gap is represented here.
    """

    def __init__(self):
        self.destinations = {}
        self.mappings = []
        self.unlinked = []
        # The identity surface, for the ADP account the run registers for its own
        # E02 login. The run's own row is NOT seeded here: it is created by
        # `enroll()` when the install_auth journey runs, because the address the
        # deleter's ownership check reads is derived from the run's evaluation ID
        # and that is not knowable before the run starts. Seeding it with a fixed
        # prefix would make every ownership check fail for the wrong reason.
        self.users = {
            "org-eval": {
                # An account this evaluation did NOT create, in the same org. The
                # deleter must never touch it, and its presence is what makes the
                # ownership check meaningful rather than vacuous.
                "operator-1": {
                    "id": "operator-1",
                    "org_id": "org-eval",
                    "team_id": "org-eval-team",
                    "email": "real.operator@example.com",
                },
            }
        }
        self.deleted_users = []

    def enroll(self, user_id, *, org, email):
        """What `_onboard` leaves behind: the ADP account behind the run's login."""
        self.users.setdefault(org, {})[user_id] = {
            "id": user_id,
            "org_id": org,
            "team_id": f"{org}-team",
            "email": email,
        }

    def register(self, destination_id, *, label, scope=None, grant=False):
        """What a journey's `connect` leaves behind: a row, and a rule naming it."""
        self.destinations[destination_id] = {
            "id": destination_id,
            "label": label,
            "grant": grant,
        }
        if scope:
            self.mappings.append({"scope": scope, "destination_id": destination_id})

    def _path(self, url):
        marker = "/admin/bedrock-routing"
        return url[url.index(marker) + len(marker) :] if marker in url else None

    def _identity(self, url, method, *, token=None):
        """`/api/admin/identity/organizations/{org}/users[/{id}]`.

        Modelled with the product's real semantics for the two that matter: the
        listing is what establishes ownership before a delete, and deleting a row
        that is not there answers 404 rather than succeeding — which is the state
        a re-run of an interrupted sweep meets.
        """
        marker = "/api/admin/identity/organizations/"
        if marker not in url:
            return None
        if not token:
            return 401, None  # the whole router is admin-gated
        remainder = url[url.index(marker) + len(marker) :]
        org, _, tail = remainder.partition("/users")
        rows = self.users.setdefault(org, {})
        if method == "GET" and tail in ("", "/"):
            users = list(rows.values())
            return 200, {"users": users, "total": len(users)}
        if method == "DELETE" and tail.startswith("/"):
            identifier = unquote(tail[1:])
            if identifier not in rows:
                return 404, None
            del rows[identifier]
            self.deleted_users.append(f"{org}/{identifier}")
            return 204, None
        return None

    def handle(self, url, method, *, token=None):
        identity = self._identity(url, method, token=token)
        if identity is not None:
            return identity
        path = self._path(url)
        if path is None:
            return None
        if not token:
            return 401, None  # every route here is platform-admin only
        if method == "GET" and path == "/destinations":
            return 200, [
                {key: row[key] for key in ("id", "label")}
                for row in self.destinations.values()
            ]
        if method == "GET" and path == "/mappings":
            return 200, list(self.mappings)
        if method == "DELETE" and path.startswith("/mappings/"):
            scope = unquote(path[len("/mappings/") :])
            self.mappings = [row for row in self.mappings if row["scope"] != scope]
            return 204, None  # 204 whether or not a rule existed, as the product does
        if method == "DELETE" and path.startswith("/connection-links/"):
            identifier = unquote(path[len("/connection-links/") :])
            row = self.destinations.get(identifier)
            if row is None or not row["grant"]:
                # No grant row, so the product's unlink cannot see it.
                return 404, None
            if any(m["destination_id"] == identifier for m in self.mappings):
                # The product refuses to remove a destination a rule still names.
                return 409, None
            del self.destinations[identifier]
            self.unlinked.append(identifier)
            return 204, None
        return None


CLI_SCRIPT = b"#!/bin/bash\necho adp\n"
GOOD_REVISION = "91ae8043125c990a349b9acf68b1c604cfdbf18e"
# Schema-conformant, because report.write() validates IDs against
# report.schema.json; an ad-hoc "eval-1" would fail there for the wrong reason.
EVAL_ID = "adp-e2e-20260915-120000-abc123"


# The release the gateway is doubled as serving. Derived from the SAME helper
# production uses, so `served == expected` holds for the reason it holds live
# (the deployment serves the revision under test) rather than because the test
# hardcoded one file into both sides — which was the R10 defect in miniature.
def release_fixture(revision=GOOD_REVISION):
    return release.manifest(revision)


def served_release(revision=GOOD_REVISION, *, stale=()):
    """`/cli/<name>` -> bytes, matching the release at `revision`.

    `stale` names files to serve with DIFFERENT bytes, which is how a mixed or
    part-rolled-out deployment looks from outside.
    """
    served = {}
    for name in release.helper_names(revision):
        payload = release.git_blob(revision, f"{release.CLI_DIR}/{name}")
        if name in stale:
            payload = payload + b"\n# drifted\n"
        served[f"/cli/{name}"] = payload
    return served


# --------------------------------------------------------------------------
# Transport-level doubles for the on-instance path
#
# R1 is explicit that overriding `run_worker` or `journey` proves nothing about
# the production boundary: the resolver defaulted to `lambda _purpose: None` and
# the E01-E05 path invoked a `worker.py` nothing shipped, yet every offline test
# passed because the tests replaced exactly the two seams that were broken.
#
# So the doubles stop at SSM. `FakeSsm` behaves like an instance: it will only
# run a dispatcher purpose AFTER the bundle has been installed and verified, it
# refuses a purpose the shipped dispatcher does not register, and it parses the
# purpose out of the real command text. Everything above it -- `_install_bundle`,
# `bundle.archive/digest/install_commands`, `_run_worker`, `_journey`,
# `_journey_payload`, `require_purpose` -- is the production code path.
# --------------------------------------------------------------------------


class FakeSsm:
    """An instance that runs the shipped bundle, and nothing else.

    Deliberately strict, because each refusal corresponds to a real failure this
    harness must not paper over:

    - a dispatcher invocation before a verified install is the R1 defect (a remote
      path that runs a file nothing delivered);
    - a digest that does not match the archive we built is a substituted or
      truncated transfer;
    - a purpose absent from the shipped dispatcher must surface as an
      implementation gap, not as a stage that quietly did nothing.
    """

    def __init__(
        self,
        evidence_by_purpose=None,
        *,
        bucket=None,
        fail_install=False,
        gateway=None,
    ):
        self.evidence = dict(evidence_by_purpose or {})
        self.bucket = bucket or STATE_BUCKET
        self.fail_install = fail_install
        self.gateway = gateway
        self.installed = False
        self.installs = 0
        self.commands = []
        self.purposes_run = []
        self.payloads = {}
        # The vault reference install_auth publishes, and how many times the
        # orchestrator came back for it (production caches: exactly one read).
        self.session_ref = (worker_evidence()["install_auth"]["session"])["session_ref"]
        self.session_reads = 0
        # Flipped by the terminate deleter. After that the vault is unreachable,
        # because the disk holding it is gone with the instance.
        self.instance_terminated = False

    # -- helpers over the real command text --------------------------------

    def _find_purpose(self, joined):
        for line in joined.splitlines():
            if bundle.DISPATCHER in line and "--self-check" not in line:
                after = line.split(bundle.DISPATCHER, 1)[1].split()
                if after:
                    return after[0].strip("'\"")
        return None

    def _install(self, joined):
        self.installs += 1
        if self.fail_install:
            return {"Status": "Failed", "StandardOutputContent": ""}
        # The digest the orchestrator asserted on the instance must be the digest
        # of the archive it actually built from the checked-in tree.
        expected = bundle.digest()
        assert expected in joined, "the install did not verify the bundle it shipped"
        assert f"s3://{self.bucket}/" in joined
        assert bundle.DISPATCHER in joined and "--self-check" in joined
        self.installed = True
        return {
            "Status": "Success",
            "StandardOutputContent": json.dumps(
                {"success": True, "purposes": sorted(bundle.purposes()), "problems": {}}
            ),
        }

    def _session_handoff(self, joined):
        """Read back the private session vault install_auth left on the instance.

        The API deleters run AFTER the instance is terminated, so they cannot
        resolve the session the way a journey does; production reads the vault
        eagerly over this same transport when the deleters are built, while the
        instance is still up. Modelled here because without it the deleters get no
        token, refuse to act, and the run reports resources as outstanding that it
        could in fact have removed.

        A read attempted after termination RAISES, exactly as the real transport
        does — there is no instance left to run a command on. This is what turns
        "the token is fetched too late" from a silent nothing into a failure, and
        it is the live defect: a lazily-fetched token was always fetched after
        `ec2_instance`, the first entry in `cleanup.ORDER`.

        The command really must be a read of THIS run's vault as ec2-user: a
        deleter that shelled something else, or read a path the session document
        did not name, fails here rather than silently working.
        """
        assert self.session_ref and self.session_ref in joined, (
            "the session handoff did not read this run's session vault"
        )
        assert "runuser -l ec2-user" in joined, (
            "the vault was read as the wrong user; it is 0600 and owned by ec2-user"
        )
        self.session_reads += 1
        if self.instance_terminated:
            raise ports.PortError(
                "the session vault was read after the instance was terminated; there is no instance left to read it from"
            )
        return {
            "Status": "Success",
            "StandardOutputContent": json.dumps({"access_token": "<token>"}),
        }

    def _register_side_effects(self, found, evidence):
        """Mirror on the gateway what a journey's CLI commands really did.

        `bedrock_routing` runs `adp admin bedrock connect`, which registers a
        destination and assigns a rule to it server-side. Without that the run's
        cleanup would be sweeping a destination the gateway never heard of, and
        "cleanup succeeded" would only mean "the row was already absent".

        The row is created with NO connection grant, because that is what `POST
        /destinations` with source `new_account` produces — which is precisely why
        the product cannot delete it again.

        `install_auth` is mirrored the same way, for the same reason: its
        `_onboard` registers the ADP account behind the run's own login through the
        product's route, and a cleanup sweep over a row the gateway never heard of
        would report success for a deletion it never performed. The email carries
        the identity the fixtures really created, because that is what the
        deleter's ownership check reads.
        """
        if self.gateway is not None and found == "install_auth":
            payload = self.payloads.get(found) or {}
            for kind, identifier in evidence.get("resources") or []:
                if kind != "adp_user":
                    continue
                org, _, user_id = str(identifier).partition("/")
                self.gateway.enroll(
                    user_id, org=org, email=payload.get("created_username") or ""
                )
        if self.gateway is None or found != "bedrock_routing":
            return evidence
        payload = self.payloads.get(found) or {}
        label = payload.get("destination_label") or ""
        user = payload.get("test_user") or "eval-admin"
        for kind, identifier in evidence.get("resources") or []:
            if kind == "bedrock_destination":
                self.gateway.register(
                    identifier, label=label, scope=f"user:{user}", grant=False
                )
        return evidence

    # -- the SsmPort surface -----------------------------------------------

    def run(self, instance_id, commands, *, purpose, timeout=600):
        joined = "\n".join(commands)
        self.commands.append(joined)
        if purpose == "install-bundle":
            return self._install(joined)
        if purpose == "session_handoff":
            return self._session_handoff(joined)
        found = self._find_purpose(joined)
        assert found, f"no dispatcher invocation in the {purpose!r} command"
        assert self.installed, (
            f"{found!r} was invoked before the bundle was installed; on a real instance this runs a file that does not exist"
        )
        assert found in bundle.purposes(), f"{found!r} is not a shipped purpose"
        self.purposes_run.append(found)
        # The payload travels as a heredoc; recover it so a test can assert on
        # what the production payload builder actually sent.
        heredoc = re.search(r"<<'EOF_PAYLOAD'\n(.*)\nEOF_PAYLOAD", joined, re.S)
        if heredoc:
            try:
                self.payloads[found] = json.loads(heredoc.group(1))
            except ValueError:
                pass
        evidence = self.evidence.get(found)
        if evidence is None:
            # A shipped purpose with no canned evidence: the script ran and
            # reported failure, which is what a broken journey looks like.
            evidence = {"success": False, "stage": "start", "error": "no evidence"}
        evidence = self._register_side_effects(found, evidence)
        return {
            "Status": "Success",
            "StandardOutputContent": json.dumps(evidence, sort_keys=True),
        }

    def json_result(self, instance_id, commands, *, purpose, timeout=600):
        result = self.run(instance_id, commands, purpose=purpose, timeout=timeout)
        for line in reversed((result.get("StandardOutputContent") or "").splitlines()):
            if line.strip().startswith("{"):
                return result, json.loads(line)
        return result, None


STATE_BUCKET = "adp-cli-uplift-eval-state"


def worker_evidence():
    """Evidence a healthy instance would report, per shipped purpose.

    Shaped from what each script actually emits, so the production stage
    assertions are what decide the case. A test that wants a failure removes or
    spoils one field rather than asserting on a stage double.
    """
    return {
        "install_auth": {
            "success": True,
            "stage": "complete",
            "transcript": ["curl -fsSL <gateway>/cli/install.sh | sh -s --"],
            "install": {
                "success": True,
                "hashes_match": True,
                "executable": True,
                "download_status": 200,
                "helpers": sorted(release_fixture()),
                "expected_helper_count": len(release_fixture()),
            },
            "login": {
                "challenge_completed": True,
                "authenticated": True,
                "bad_credentials_rejected": True,
                "non_admin_denied": True,
                "refresh_succeeded": True,
                "challenges": ["NEW_PASSWORD_REQUIRED"],
                # What proves the challenge happened: the pre-challenge password no
                # longer authenticates. `/auth/cli/admin-session` publishes no
                # challenge history, so this is the server-side evidence.
                "prior_password_retired": True,
                "user_id": "user-eval",
                "org_id": "org-eval",
                "role": "platform_admin",
                "username": "eval-admin",
            },
            "setup": {
                "statuses_accurate": True,
                "rerun_completed_missing_only": True,
                "duplicates": [],
                "reported_states": {"bedrock": "configured"},
            },
            # The ADP account the journey registers for the run's OWN login, so
            # every user-scoped route a later case exercises has a `users` row to
            # resolve the Cognito subject to. Reported as a resource because the
            # orchestrator cannot know its id in advance -- the product assigns it.
            "onboard": {
                "org_id": "org-eval",
                "team_id": "org-eval-team",
                "user_id": "adp-user-eval",
                "cognito_sub_bound": True,
            },
            "resources": [["adp_user", "org-eval/adp-user-eval"]],
            "session": {
                # A durable path, not the journey's temp HOME: install_auth's HOME
                # is deleted when it returns, so a later journey needs the CLI to
                # have been preserved outside it.
                "cli_path": "/home/ec2-user/adp-eval/cli/adp",
                # A non-secret reference, which is what the shipped script now
                # exports: the tokens stay in a private on-instance vault because
                # anything credential-shaped in evidence is redacted on its way out,
                # which used to hand later journeys the string "<redacted>".
                "session_ref": "/home/ec2-user/adp-eval/session.json",
                "expires_at": NOW + 3600,
                "username": "eval-admin",
                "created_username": "eval-admin",
                # The gateway's own id for the identity, which the usage log keys
                # on. E08 asserts against this rather than the username.
                "user_id": "user-eval",
                "org_id": "org-eval",
            },
        },
        "personal_aws_provision": {
            "success": True,
            "stage": "complete",
            "stack_id": "arn:aws:cloudformation:us-east-1:605440105851:stack/s/1",
            "connection_id": "conn-provision",
            "provisioner_caller_arn": "arn:aws:sts::605440105851:assumed-role/prov/i",
            "checks": [
                "connect_provisions_role",
                "disconnect_removes_adp_record_preserves_aws_role",
            ],
        },
        "personal_aws_handoff": {
            "success": True,
            "stage": "complete",
            "stack_created": False,
            "connection_id": "conn-handoff",
            "checks": [
                "download_hands_off_without_provisioning",
                "disconnect_removes_adp_record_preserves_aws_role",
            ],
        },
        "bedrock_routing": {
            "success": True,
            "stage": "complete",
            "stack_id": "arn:aws:cloudformation:us-east-1:605440105851:stack/b/2",
            "resources": [
                ["bedrock_destination", "dest-1"],
                [
                    "cloudformation_stack",
                    "arn:aws:cloudformation:us-east-1:605440105851:stack/b/2",
                ],
            ],
            "correlation": {"bedrock_destination_account": "605440105851"},
            "checks": ["verified_destination_becomes_effective_rule"],
        },
        "personal_inference": {
            "success": True,
            "stage": "complete",
            "usage": {"destination_account": "605440105851"},
            "correlation": {"inference_destination_account": "605440105851"},
            "checks": ["claude_returned_marker", "codex_returned_marker"],
        },
        # E13 creates one ADP connection through `adp aws connect --download` and
        # removes it itself, asserting absence through the CLI's own reader. It
        # therefore reports BOTH `resources` (so an interrupted run still leaves a
        # record the sweep can act on) and `removed` (so a SUCCESSFUL run does not
        # leave cleanup chasing a resource the run already proved gone -- R8's
        # failure, in the journeys stage this time).
        "api_parity": {
            "success": True,
            "stage": "complete",
            "resources": [["adp_connection", "conn-parity"]],
            "removed": [["adp_connection", "conn-parity"]],
            "correlation": {
                "parity_connection_name": EVAL_ID + "-parity",
                "parity_connection_id": "conn-parity",
                "parity_connection_removed": True,
                "parity_surfaces_checked": 3,
                "parity_rows_checked": 4,
            },
            "checks": [
                "installed_cli_readers_ran_against_live_responses",
                "live_rows_satisfy_the_browser_declared_wire_types",
                "rows_are_scoped_to_the_calling_identity",
                "cli_created_resource_is_complete_for_both_consumers",
                "created_connection_was_removed_through_the_cli",
            ],
        },
        # E14 creates no AWS resources: it installs, updates, rolls back and
        # configures the provider CLIs inside a per-run temp HOME, so it reports
        # `detail` (which `journeys_stage` prefers) and no `resources`.
        "update_rollback": {
            "success": True,
            "stage": "complete",
            "correlation": {"update_release_files": 10, "update_proxy_port": 9191},
            "checks": [
                "update_lands_release_and_keeps_previous",
                "rollback_restores_then_refuses_without_previous",
                "interrupted_install_preserves_usable_installation",
                "codex_setup_preserves_foreign_config_and_reruns_clean",
                "claude_setup_merges_and_reruns_clean",
                "launchers_forward_arguments_and_refuse_dead_sessions",
            ],
            "detail": {
                "update": {"installed_matches_release": True, "previous_kept": ["adp"]},
                "rollback": {
                    "restored_previous_bytes": True,
                    "second_rollback_exit_code": 5,
                    "second_rollback_refused_with_message": True,
                },
                "interrupted_install": {
                    "refused": True,
                    "installation_unchanged": True,
                    "staged_temporaries_left": [],
                },
                "codex_setup": {
                    "model_provider_is_top_level": True,
                    "base_url_port": 9191,
                    "rerun_byte_identical": True,
                },
                "claude_setup": {
                    "api_key_helper_is_installed_absolute_path": True,
                    "rerun_byte_identical": True,
                },
                "launch": {
                    "claude": {"exit_code": 0, "forwarded_version_reported": True},
                    "codex": {"proxy_started": True, "proxy_stopped": True},
                    "dead_session_refused": True,
                },
            },
        },
    }


def live_doubles(
    cfg,
    *,
    revision=GOOD_REVISION,
    subnet_public=False,
    stale=(),
    evidence=None,
    fail_install=False,
    gateway=None,
):
    """Transports that let the REAL stages and the REAL wiring reach their work.

    Shaped like a healthy dev environment: correct accounts, matching revision,
    the release served, private subnet, the pinned Cognito pool, an instance that
    runs the shipped bundle. Individual tests spoil one value to prove the
    corresponding stage rejects it.

    Only `aws`, `http` and `ssm` are supplied. Every capability above them --
    identity, install_bundle, run_worker, journey, worker_config, deleters -- is
    built by `live.wire()` exactly as production builds it.
    """
    aws = FakeAws(
        {
            "sts.get_caller_identity": lambda **_k: {
                "Account": cfg["platform_account"],
                "Arn": "arn:aws:sts::879318057152:assumed-role/runner/x",
            },
            # An assumed session in the destination account is what makes the
            # cross-account identity real; `FakeAws.assume` records it.
            "sts.assume_role": {
                "Credentials": {
                    "AccessKeyId": "A",
                    "SecretAccessKey": "B",
                    "SessionToken": "C",
                }
            },
            "ec2.describe_subnets": {
                "Subnets": [
                    {
                        "VpcId": cfg["vpc_id"],
                        "OwnerId": cfg["platform_account"],
                        "MapPublicIpOnLaunch": subnet_public,
                        "AvailabilityZone": "us-east-1a",
                    }
                ]
            },
            "cognito-idp.describe_user_pool": {
                "UserPool": {
                    "Id": cfg["cognito_user_pool_id"],
                    "Arn": f"arn:aws:cognito-idp:us-east-1:{cfg['platform_account']}:userpool/x",
                }
            },
            "cognito-idp.list_user_pool_clients": {
                "UserPoolClients": [{"ClientId": "abc123"}]
            },
            # E02's run-owned fixture identities. Stubbed so the provisioning is
            # exercised by every full-run test rather than mocked away: a stage
            # that silently skipped creating them would otherwise still pass.
            "cognito-idp.admin_create_user": {},
            "cognito-idp.admin_add_user_to_group": {},
            "cognito-idp.admin_set_user_password": {},
            "secretsmanager.create_secret": lambda **kwargs: {
                "ARN": (
                    f"arn:aws:secretsmanager:us-east-1:{cfg['platform_account']}:secret:{kwargs['Name']}-AbCdEf"
                )
            },
            "secretsmanager.put_resource_policy": {},
            "iam.get_instance_profile": lambda **kwargs: {
                "InstanceProfile": {
                    "InstanceProfileName": kwargs["InstanceProfileName"]
                }
            },
            "ssm.get_parameter": {"Parameter": {"Value": "ami-0eval"}},
            "ec2.run_instances": {"Instances": [{"InstanceId": "i-0eval"}]},
            # The bundle upload the real `_install_bundle` performs before it
            # can ask the instance to extract anything.
            "s3.put_object": {},
        }
    )
    http = FakeHttp(
        json_by_suffix={
            # R2: the canonical prefix-free discovery document, with the contract
            # the product actually returns (`identity_pool_id` is always empty
            # there, so it must be PRESENT and may be blank).
            preflight.DISCOVERY_PATH: {
                "user_pool_id": cfg["cognito_user_pool_id"],
                "client_id": "abc123",
                "cli_client_id": "cli123",
                "identity_pool_id": "",
                "region": cfg["region"],
            },
            "/health": {"revision": revision},
        },
        # R10: all ten files of the release, so `served == expected` is a real
        # comparison over the whole helper set rather than three of ten.
        bytes_by_suffix=served_release(revision, stale=stale),
        # The routing API cleanup calls. Supplied so the destination deleter runs
        # against something with the product's own ordering semantics.
        gateway=gateway,
    )
    return {
        "aws": aws,
        "http": http,
        "ssm": FakeSsm(
            worker_evidence() if evidence is None else evidence,
            bucket=cfg.get("state_bucket"),
            fail_install=fail_install,
            gateway=gateway,
        ),
        # Waiting for SSM registration is a poll over `describe_instance_information`
        # in production; there is nothing to assert about sleeping, so this is the
        # one capability the doubles replace above the transport.
        "wait_online": lambda _instance: True,
        "harness_auth_helper": lambda: config.REQUIRED_CLI_CONFIG_DIR_LINE,
    }


def run_live_stages(
    tmp_path,
    *,
    mode="start",
    overrides=None,
    extra=(),
    doubles=None,
    store=None,
    **config_overrides,
):
    """Invoke the runner exactly as Actions does, with the TRANSPORTS doubled.

    Crucially this passes a FACTORY, not a stage mapping: `resolve_stages` calls
    it with the loaded config and the factory calls the production
    `stages.build_stages`, which calls the production `live.wire`. So the mapping
    under test is the one production assembles, over the real capability wiring,
    and only `aws`/`http`/`ssm` are substituted.

    A `state_bucket` is supplied because a live run needs one to deliver the remote
    bundle at all — and its durable store is doubled in memory rather than left to
    `default_store`, which would build a boto3 session.
    """
    seen = {}
    settings = {"state_bucket": STATE_BUCKET, **config_overrides}

    def factory(cfg):
        ports_double = (doubles or live_doubles)(cfg)
        ports_double.update(overrides or {})
        seen.update(ports_double)
        return stages.build_stages(cfg, ports_double)

    argv = [
        "--mode",
        mode,
        "--config",
        write_config(tmp_path, **settings),
        "--state-dir",
        str(tmp_path / STATE),
        *extra,
    ]
    if store is None and settings.get("state_bucket"):
        store = statestore.S3StateStore(
            FakeS3(), settings["state_bucket"], kms_key_id="key-1", clock=lambda: NOW
        )
    code = runner.main(argv, stages=factory, clock=lambda: NOW, store=store)
    return RunResult(code, seen, tmp_path)


class RunResult:
    """The exit code, the transports the run actually used, and its state file.

    Returning the transports is the point: an assertion about what the run did is
    an assertion about the calls that reached AWS, the gateway and the instance —
    not about a stage double having been invoked.
    """

    def __init__(self, code, ports_used, tmp_path):
        self.code = code
        self.ports = ports_used
        self._path = tmp_path / STATE / runner.STATE_FILE

    def __eq__(self, other):  # so `run_live_stages(...) == 1` still reads well
        return self.code == other

    def __int__(self):
        return self.code

    @property
    def document(self):
        return json.loads(self._path.read_text())


def test_default_stages_are_the_live_implementations_not_an_empty_mapping():
    """The exact regression: no `stages` argument must mean live, never nothing.

    `resolve_stages(cfg, None)` is what `main()` reaches when Actions invokes it.
    If this ever returns an empty mapping again, every stage silently skips.
    """
    resolved = runner.resolve_stages(config.validate(config_fixture()), None)
    assert set(resolved) >= set(stages.REQUIRED_STAGES)
    assert all(callable(stage) for stage in resolved.values())


def test_build_stages_supplies_every_required_stage():
    cfg = config.validate(config_fixture(state_bucket=STATE_BUCKET))
    assembled = stages.build_stages(cfg, live_doubles(cfg))
    assert sorted(assembled) == sorted(stages.REQUIRED_STAGES)


def test_a_required_stage_with_no_implementation_fails_the_run(tmp_path):
    """A stage absent from the mapping must be loud, not a silent skip.

    This is the guard that makes finding 1 unrepeatable: even if some future
    caller assembles an incomplete mapping, the run cannot report acceptance.
    """
    partial = {"preflight": lambda _ctx: None}
    code = runner.main(
        [
            "--mode",
            "start",
            "--config",
            write_config(tmp_path),
            "--state-dir",
            str(tmp_path / STATE),
        ],
        stages=partial,
        clock=lambda: NOW,
    )
    assert code == 1
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert document["stages"]["ec2"] == "missing"
    assert any(e.get("type") == "MissingStage" for e in document["errors"])
    assert document["status"] == cases.FAILED


def test_empty_stage_mapping_can_never_report_acceptance(tmp_path, capsys):
    """The literal pre-fix invocation, pinned as a failure forever."""
    code = runner.main(
        [
            "--mode",
            "start",
            "--config",
            write_config(tmp_path),
            "--state-dir",
            str(tmp_path / STATE),
        ],
        stages={},
        clock=lambda: NOW,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["full_acceptance"] is False
    assert payload["status"] == cases.FAILED


def test_live_preflight_really_calls_the_gateway_and_aws(tmp_path):
    """Evidence the production preflight ran: the calls it must make happened."""
    result = run_live_stages(tmp_path)
    document = result.document
    # Preflight completed against the doubled target, rather than being skipped.
    assert document["stages"]["preflight"] == "complete"
    assert document["preflight"]["deployed_revision"] == GOOD_REVISION
    # R10: the WHOLE release was compared, not three of ten files, and against
    # hashes derived from the revision rather than from this same download.
    assert set(document["preflight"]["served_cli_hashes"]) == set(release_fixture())
    assert len(release_fixture()) >= 10
    assert (
        document["preflight"]["expected_release"]["files"]
        == document["preflight"]["served_cli_hashes"]
    )
    assert document["preflight"]["cli_config_dir_isolation"] is True
    # The gateway endpoints were genuinely fetched — including the CANONICAL
    # discovery path (R2), not `/cli/discovery`, which the gateway does not serve.
    urls = result.ports["http"].urls
    assert any(url.endswith("/health") for url in urls)
    assert any(url.endswith(preflight.DISCOVERY_PATH) for url in urls)
    assert not any("/cli/discovery" in url for url in urls)
    # And AWS was genuinely consulted for subnet and Cognito ownership.
    operations = {f"{s}.{o}" for s, o, _ in result.ports["aws"].calls}
    assert "ec2.describe_subnets" in operations
    assert "cognito-idp.describe_user_pool" in operations


def test_live_preflight_grades_a_full_run_when_the_destination_roles_are_absent(
    tmp_path,
):
    """A `full` run must reach its cases when only the destination is unbound.

    Preflight decided `destination_required` from matrix membership alone, so it
    demanded a cross-account identity on behalf of E04-E08 — the very cases it
    was about to block for lacking that access. `live._identity` raises PortError
    without a role to assume, so preflight failed and all fifteen cases reported
    NOT_RUN. This is the live-wiring half of the same defect as the config gate:
    a fixture gap has to grade, not abort.
    """
    result = run_live_stages(
        tmp_path,
        destination_role_arn="",
        provisioner_role_arn="",
    )
    document = result.document
    # Preflight now completes rather than dying on an unreachable destination.
    assert document["stages"]["preflight"] == "complete"
    matrix = document["matrix"]

    # Exactly the destination-dependent cases block, and they say why.
    for case_id in ("E04", "E05", "E06", "E07", "E08"):
        assert matrix[case_id]["status"] == cases.BLOCKED
        assert cases.DESTINATION in matrix[case_id]["detail"]["missing_fixtures"]

    # The cases needing no destination were reached instead of being collateral.
    for case_id in ("E01", "E02", "E03", "E13", "E14"):
        assert matrix[case_id]["status"] != cases.NOT_RUN

    # No destination session was ever attempted: asking for one is what broke.
    assert not result.ports["aws"].assumed

    # And acceptance stays closed in the PUBLISHED report, because blocked is
    # not passed. Asserted on the artifact an operator actually reads.
    report_payload = json.loads((tmp_path / STATE / "out" / "report.json").read_text())
    assert report_payload["full_acceptance"] is False
    assert report_payload["status"] == cases.FAILED
    assert any("blocked" in reason for reason in report_payload["reasons"])


def test_live_preflight_rejects_a_revision_that_is_not_under_test(tmp_path):
    """The deployed revision must gate the run, live wiring included."""
    result = run_live_stages(
        tmp_path,
        overrides={
            "http": FakeHttp(
                json_by_suffix={
                    preflight.DISCOVERY_PATH: {
                        "user_pool_id": config_fixture()["cognito_user_pool_id"],
                        "client_id": "abc123",
                        "cli_client_id": "cli123",
                        "identity_pool_id": "",
                        "region": "us-east-1",
                    },
                    "/health": {"revision": "c" * 40},
                },
                bytes_by_suffix=served_release(),
            )
        },
    )
    assert result.code == 1
    assert result.document["stages"]["preflight"] == "failed"
    assert {entry["status"] for entry in result.document["matrix"].values()} == {
        cases.NOT_RUN
    }


def test_live_preflight_refuses_a_contaminated_config_directory(tmp_path):
    """Without the BG_CONFIG_DIR line the worker can pick up another session."""
    result = run_live_stages(
        tmp_path, overrides={"harness_auth_helper": lambda: 'CONFIG_DIR="${HOME}/.x"'}
    )
    assert result.code == 1
    assert result.document["stages"]["preflight"] == "failed"


def test_live_stages_block_github_cases_when_no_isolated_fixture_exists(tmp_path):
    """Absent fixtures are `blocked` -- operator-actionable, never a pass."""
    document = run_live_stages(tmp_path).document
    for case_id in ("E10", "E11", "E12"):
        assert document["matrix"][case_id]["status"] == cases.BLOCKED
        assert document["matrix"][case_id]["detail"]["missing_fixtures"]


def test_a_case_with_no_shipped_script_fails_as_unimplemented_never_blocked(tmp_path):
    """Absent WORK must fail; only an absent FIXTURE may block.

    Conflating the two is how missing implementation reads as "waiting on an
    operator" instead of "not built yet".

    E07 is the case that isolates it. Its one scarce fixture is a second
    destination account, so supplying that leaves it with every fixture it needs
    and still no `remote/bedrock_rungs.py`: it must fail naming the file to write,
    not block waiting on an operator who has nothing left to do. Without the
    override it would block, and a blocked case would make this test pass for the
    wrong reason.

    (E13 held this role until `remote/api_parity.py` shipped. That it no longer
    does is the point — the list of unimplemented purposes is asserted separately,
    so a case leaving it has to be a deliberate edit here.)
    """
    document = run_live_stages(
        tmp_path, second_destination_account="210987654321"
    ).document
    for case_id, purpose in (("E07", "bedrock_rungs"),):
        entry = document["matrix"][case_id]
        assert entry["status"] == cases.FAILED
        assert entry["detail"]["unimplemented"] is True
        # The message names the module a developer must write, not a generic gap.
        assert f"remote/{purpose}.py" in entry["detail"]["detail"]
        assert purpose not in bundle.purposes()
    assert document["status"] == cases.FAILED


# --------------------------------------------------------------------------
# Realistic failure paths through the production entry point
#
# The reviewer's standard: "merely asserting that a stage is callable, or that an
# absent driver fails, does not prove the implementation works." Each of these
# spoils ONE thing a real deployment can get wrong and asserts the run goes red
# for that reason, at the stage that owns the check.
# --------------------------------------------------------------------------


def test_a_deployment_serving_one_stale_helper_aborts_before_any_mutation(tmp_path):
    """R10's failure, at the granularity that matters: one file of ten.

    A part-rolled-out deployment serves most of the release and one older helper.
    `adp-aws.py` is what E04/E05 drive, so installing a stale copy would evaluate
    code that is not under test — and the old preflight could not see it, because
    it hashed three files and compared the download against itself.
    """
    result = run_live_stages(
        tmp_path, doubles=lambda cfg: live_doubles(cfg, stale=("adp-aws.py",))
    )
    assert result.code == 1
    document = result.document
    assert document["stages"]["preflight"] == "failed"
    # Read-only stage, so nothing was launched and no case was attempted.
    assert document["stages"]["ec2"] == "pending"
    assert document.get("instance_id") is None
    assert {entry["status"] for entry in document["matrix"].values()} == {cases.NOT_RUN}
    assert result.ports["ssm"].installs == 0


def test_an_instance_where_the_bundle_will_not_install_runs_no_journey(tmp_path):
    """The R1 defect as a failure path: no scripts, therefore no evidence.

    Previously the remote scripts were never delivered at all and every journey
    "ran" against a file that did not exist. Delivery failing must now stop the run
    at the stage that owns delivery, not surface later as an inexplicable journey
    error — and the upload's intent must already be recorded, so the object is
    swept even though the run died immediately after.
    """
    result = run_live_stages(
        tmp_path, doubles=lambda cfg: live_doubles(cfg, fail_install=True)
    )
    assert result.code == 1
    document = result.document
    assert document["stages"]["ec2"] == "failed"
    assert document["stages"]["install_auth"] == "pending"
    ssm = result.ports["ssm"]
    assert ssm.installs == 1 and ssm.purposes_run == []  # nothing was invoked
    assert document["matrix"]["E01"]["status"] == cases.NOT_RUN
    assert document["status"] == cases.FAILED
    kinds = {entry["kind"] for entry in document["cleanup_results"] or []}
    assert {"ec2_instance", "s3_object"} <= kinds


def test_a_journey_that_crashes_on_the_instance_fails_its_case(tmp_path):
    """A script that dies mid-way must fail, carrying the stage it reached."""
    broken = {
        **worker_evidence(),
        "bedrock_routing": {
            "success": False,
            "stage": "connect",
            "error": "connect reported 'pending'",
        },
    }
    document = run_live_stages(
        tmp_path, doubles=lambda cfg: live_doubles(cfg, evidence=broken)
    ).document
    entry = document["matrix"]["E06"]
    assert entry["status"] == cases.FAILED
    assert entry["detail"]["stage_reached"] == "connect"
    assert document["status"] == cases.FAILED


def test_a_journey_that_reports_nothing_at_all_fails_rather_than_passing(tmp_path):
    """Silence is the dangerous case: no JSON means the script never reported.

    `json_result` returns None, and the stage must read that as a failure. An
    `evidence or {}` that then defaulted to success is how a run goes green on a
    journey that never happened.
    """
    silent = {key: None for key in worker_evidence()}
    document = run_live_stages(
        tmp_path, doubles=lambda cfg: live_doubles(cfg, evidence=silent)
    ).document
    for case_id in ("E01", "E02", "E03", "E04", "E05", "E06", "E08"):
        assert document["matrix"][case_id]["status"] == cases.FAILED
    assert document["status"] == cases.FAILED


def test_inference_without_usage_evidence_cannot_pass(tmp_path):
    """E08's whole point: the model answered AND ADP recorded where it routed.

    A marker in stdout only proves a model replied. Without usage naming the
    destination account, the call could have been served by the platform's own
    ambient credentials — which is the finding this case exists to catch.
    """
    no_usage = {
        **worker_evidence(),
        "personal_inference": {
            "success": False,
            "stage": "usage",
            "usage": None,
            "error": "no usage row named the destination account",
            "checks": ["claude_returned_marker", "codex_returned_marker"],
        },
    }
    document = run_live_stages(
        tmp_path, doubles=lambda cfg: live_doubles(cfg, evidence=no_usage)
    ).document
    assert document["matrix"]["E08"]["status"] == cases.FAILED
    assert document["status"] == cases.FAILED


def test_a_gateway_answering_the_spa_fallback_is_not_accepted_as_discovery(tmp_path):
    """The R2 failure as it would really present: HTTP 200, wrong body.

    CloudFront serves index.html with a 200 for any path the API does not own, so
    a wrong `gateway_url` (or the old `/cli/discovery`, which the download route
    cannot serve) looks reachable. Only validating the response CONTRACT
    distinguishes an ADP gateway from an SPA, and the check must run on whatever
    came back through this run's transport rather than fetching the URL again.
    """
    http = FakeHttp(
        json_by_suffix={
            preflight.DISCOVERY_PATH: "<!doctype html><title>ADP</title>",
            "/health": {"revision": GOOD_REVISION},
        },
        bytes_by_suffix=served_release(),
    )
    result = run_live_stages(tmp_path, overrides={"http": http})
    assert result.code == 1
    assert result.document["stages"]["preflight"] == "failed"
    assert result.document["preflight"]["unauthenticated_discovery_status"] == 200
    # The helper must not have gone around the transport to re-fetch it.
    assert sum(1 for url in http.urls if url.endswith(preflight.DISCOVERY_PATH)) == 1
    assert result.document.get("instance_id") is None


def test_a_deployment_whose_cognito_client_is_unconfigured_aborts(tmp_path):
    """Discovery present but blank: CLI login cannot work, so E01-E03 cannot.

    A gateway deployed without its Cognito client env returns the document with
    empty strings. Every key is present, so a keys-only check passes and the run
    would go on to fail on the instance with an opaque login error.
    """
    result = run_live_stages(
        tmp_path,
        overrides={
            "http": FakeHttp(
                json_by_suffix={
                    preflight.DISCOVERY_PATH: {
                        "user_pool_id": config_fixture()["cognito_user_pool_id"],
                        "client_id": "",
                        "cli_client_id": "",
                        "identity_pool_id": "",
                        "region": "us-east-1",
                    },
                    "/health": {"revision": GOOD_REVISION},
                },
                bytes_by_suffix=served_release(),
            )
        },
    )
    assert result.code == 1
    assert result.document["stages"]["preflight"] == "failed"


def test_a_run_with_no_destination_bindings_is_refused_before_any_state_exists(
    tmp_path,
):
    """R3, at the only point where refusing is free.

    Nothing supplied these and nothing checked, so a run launched an instance and
    then died inside a journey with "No destination role is configured" — after a
    mutation, with the fixture password unreadable. The refusal has to happen
    before the lease, the state write and the instance.

    Asserted on an explicitly named destination suite: the operator asked for
    exactly the cross-account cases, so refusing is the useful answer. `full` is
    deliberately different — see
    test_full_still_runs_and_grades_when_only_the_destination_roles_are_absent.
    """
    state_dir = tmp_path / STATE
    with pytest.raises(config.ConfigError) as raised:
        runner.main(
            [
                "--mode",
                "start",
                "--suite",
                "personal-aws",
                "--config",
                write_config(
                    tmp_path,
                    destination_role_arn="",
                    provisioner_role_arn="",
                    credential_secret_name="",
                ),
                "--state-dir",
                str(state_dir),
            ],
            stages=lambda cfg: (_ for _ in ()).throw(
                AssertionError("stages were built despite missing bindings")
            ),
            clock=lambda: NOW,
            store=None,
        )
    message = str(raised.value)
    for key in config.BINDINGS:
        assert key in message
    # No lease, no state file, nothing to clean up.
    assert not (state_dir / "state.json").exists()


def test_full_still_runs_and_grades_when_only_the_destination_roles_are_absent():
    """A `full` dispatch must always produce a graded report.

    It previously raised at config validation, so the job died before launching
    anything: no report, no matrix, and a fixture gap that looked identical to a
    harness crash. The cases needing no destination — E01/E02/E03/E13/E14/E15 —
    never ran even though nothing stopped them.

    Blocking is not passing, so acceptance stays closed either way; the
    difference is whether the run says which cases are blocked and on what.
    """
    cfg = config.validate(
        config_fixture(destination_role_arn="", provisioner_role_arn="")
    )
    # Does not raise, and does not silently drop the secret requirement. #5413's
    # three deployment records are reported as needed here for the same reason the
    # destination roles are: a `full` run wants them, and saying so is not the same
    # as refusing to start without them.
    assert (
        config.require_bindings(cfg, FULL) == config.BINDINGS + config.FIXTURE_BINDINGS
    )

    # The destination class is withheld, so exactly its cases block...
    available = preflight.evaluate_fixtures(cfg)
    assert cases.DESTINATION not in available
    matrix = cases.new_matrix(FULL)
    blocked = cases.block_missing_fixtures(matrix, available)
    assert {"E04", "E05", "E06", "E07", "E08"} <= set(blocked)
    assert blocked["E04"] == [cases.DESTINATION]

    # ...and the operator is told what to create, not just that it is blocked.
    absent = preflight.missing_fixture_report(cfg, available)
    assert "destination_role_arn" in absent[cases.DESTINATION]["needs"]

    # Acceptance remains closed: a blocked case can never read as passed.
    status, reasons = cases.accept(matrix, FULL, stages={"preflight": "complete"})
    assert status == cases.FAILED
    assert any("blocked" in reason and "E04" in reason for reason in reasons)


def test_a_named_destination_suite_still_refuses_rather_than_reporting_only_blocks():
    """The full-run carve-out must not leak into an explicit selection.

    `--suite personal-aws` with no destination roles can produce nothing but
    blocked rows, so failing at config time is the honest and cheaper answer.
    """
    cfg = config.validate(
        config_fixture(destination_role_arn="", provisioner_role_arn="")
    )
    for suites in (("personal-aws",), ("routing",), ("inference",)):
        with pytest.raises(config.ConfigError) as raised:
            config.require_bindings(cfg, suites)
        assert "destination_role_arn" in str(raised.value)


def test_the_harness_suite_alone_needs_no_destination_bindings(tmp_path):
    """It never reaches the destination account, and demanding plumbing for it
    would push operators toward placeholder ARNs, which is worse than none."""
    assert (
        config.require_bindings(
            config.validate(
                config_fixture(
                    destination_role_arn="",
                    provisioner_role_arn="",
                    credential_secret_name="",
                )
            ),
            ("harness",),
        )
        == ()
    )
    # Authentication and destination suites still require their bindings.
    for suites in (("login",), ("admin",), ("full",)):
        with pytest.raises(config.ConfigError):
            config.require_bindings(
                config.validate(
                    config_fixture(
                        destination_role_arn="",
                        provisioner_role_arn="",
                        credential_secret_name="",
                    )
                ),
                suites,
            )


def test_a_run_with_no_state_bucket_fails_delivery_rather_than_skipping_it(tmp_path):
    """Silently not delivering the scripts is precisely the R1 defect.

    With no bucket there is no way to get the bundle onto the instance, so the run
    must say so at the stage that owns delivery. A delivery step that no-ops when
    unconfigured is how every journey came to invoke a file that did not exist.
    """
    result = run_live_stages(tmp_path, state_bucket="")
    assert result.code == 1
    document = result.document
    assert document["stages"]["preflight"] == "complete"  # the target was fine
    assert document["stages"]["ec2"] == "failed"
    assert result.ports["ssm"].purposes_run == []
    errors = {entry["stage"]: entry for entry in document.get("errors") or []}
    assert errors["ec2"]["type"] == "PortError"
    assert "CLI_UPLIFT_EVAL_STATE_BUCKET" in (errors["ec2"]["message"] or "")


def test_a_foreign_exception_contributes_its_type_and_never_its_text(tmp_path):
    """The redaction boundary on the error list, in both directions.

    Publishing a stage error's message is what makes a failed run actionable, but
    only the harness's own error types are written to carry no provider text. A
    botocore message can quote a bucket policy, an ARN or a presigned URL, so it
    must be dropped even though dropping it costs an operator detail.
    """

    class ClientError(RuntimeError):
        """Shaped like botocore's, which is the exception that really lands here."""

    leak = (
        "An error occurred (AccessDenied): arn:aws:iam::879318057152:role/secret-role"
    )

    def doubles(cfg):
        ports_double = live_doubles(cfg)
        ports_double["harness_auth_helper"] = lambda: (_ for _ in ()).throw(
            ClientError(leak)
        )
        return ports_double

    result = run_live_stages(tmp_path, doubles=doubles)
    assert result.code == 1
    errors = {entry["stage"]: entry for entry in result.document.get("errors") or []}
    assert errors["preflight"]["type"] == "ClientError"
    assert errors["preflight"]["message"] is None
    assert leak not in json.dumps(result.document)
    # And the whitelist is types, not a per-stage exemption: every entry must be
    # one of ours, or a message could be published by adding an error class.
    assert all(issubclass(kind, Exception) for kind in runner.OWN_ERRORS)
    assert ports.PortError in runner.OWN_ERRORS
    assert stages.StageError in runner.OWN_ERRORS


def deployment_evidence(
    revision=GOOD_REVISION, *, tags=None, digest="sha256:" + "a" * 64
):
    """What the deploy workflow leaves behind: an engine Lambda pinned to a digest.

    `gateway-deploy.yml` stamps the EKS deployment with `adp-gateway:<sha>` and
    pins this Lambda to the digest of that same tag, asserting the two are equal
    before it reports success. So resolving the digest back to its tags names the
    deployed revision, under an IAM-authorized read.
    """
    return {
        "lambda.get_function": {
            "Code": {
                "ResolvedImageUri": f"879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@{digest}"
            }
        },
        "ecr.describe_images": {
            "imageDetails": [
                {"imageTags": ["latest", revision] if tags is None else list(tags)}
            ]
        },
    }


def health_without_a_revision(cfg, **kwargs):
    """The doubles, with `/health` answering what the product really answers.

    `modules/gateway/src/app.py` returns `{"status": "healthy"}` — no revision,
    no git_sha, no version. Verified against the live dev gateway.
    """
    ports_double = live_doubles(cfg, **kwargs)
    ports_double["http"].json_by_suffix["/health"] = {"status": "healthy"}
    ports_double["aws"].replies.update(deployment_evidence())
    return ports_double


def test_a_gateway_that_publishes_no_revision_is_bound_by_its_deployment(tmp_path):
    """The product's /health has never reported a revision.

    Requiring one there aborted every live run in preflight with "reports no
    revision" — before launching anything, so no live evidence could ever be
    collected. A new public health-metadata API is not the fix: the deployment
    already publishes this privately, and more trustworthily.
    """
    result = run_live_stages(tmp_path, doubles=health_without_a_revision)
    record = result.document["preflight"]
    assert record["deployed_revision"] == GOOD_REVISION
    assert record["revision_source"] == "deployment_image_tag"
    # Preflight completed, so the run reached the instance it needed.
    assert result.document["stages"]["preflight"] == "complete"
    assert result.document["stages"]["ec2"] == "complete"


def test_the_gateways_own_revision_is_preferred_when_it_reports_one(tmp_path):
    """Cheapest, and it observes the exact process serving the run."""
    result = run_live_stages(tmp_path)  # live_doubles' /health reports the revision
    record = result.document["preflight"]
    assert record["deployed_revision"] == GOOD_REVISION
    assert record["revision_source"] == "gateway_health"
    # And it did not need the deployment evidence to get there: `lambda` and `ecr`
    # are unstubbed in these doubles, so touching them would have raised.
    assert not [
        call for call in result.ports["aws"].calls if call[0] in ("lambda", "ecr")
    ]


def test_a_deployment_running_a_different_revision_stops_the_run(tmp_path):
    """The whole point of binding: results must name the code that produced them."""
    other = "4a91eb369f506ef8bdcf3d6f89e2bef03ab83b71"

    def doubles(cfg):
        ports_double = health_without_a_revision(cfg)
        ports_double["aws"].replies.update(deployment_evidence(other))
        return ports_double

    result = run_live_stages(tmp_path, doubles=doubles)
    assert result.code == 1
    assert result.document["stages"]["preflight"] == "failed"
    assert result.document["preflight"]["deployed_revision"] == other
    assert result.document.get("instance_id") is None


def test_a_moving_tag_alone_cannot_bind_a_run_to_a_revision(tmp_path):
    """`latest` names no commit. An unbindable run must fail, not guess."""

    def doubles(cfg):
        ports_double = health_without_a_revision(cfg)
        ports_double["aws"].replies.update(deployment_evidence(tags=["latest"]))
        return ports_double

    result = run_live_stages(tmp_path, doubles=doubles)
    assert result.code == 1
    assert result.document["stages"]["preflight"] == "failed"
    errors = {entry["stage"]: entry for entry in result.document.get("errors") or []}
    assert "commit-shaped tags" in (errors["preflight"]["message"] or "")


def test_two_commit_tags_on_one_image_are_ambiguous_rather_than_arbitrary(tmp_path):
    """Picking the first would bind the run to whichever tag sorted earlier."""

    def doubles(cfg):
        ports_double = health_without_a_revision(cfg)
        ports_double["aws"].replies.update(
            deployment_evidence(
                tags=[GOOD_REVISION, "4a91eb369f506ef8bdcf3d6f89e2bef03ab83b71"]
            )
        )
        return ports_double

    result = run_live_stages(tmp_path, doubles=doubles)
    assert result.code == 1
    assert result.document["stages"]["preflight"] == "failed"


def test_an_unpinned_deployment_cannot_bind_a_run(tmp_path):
    """A Lambda on a floating tag proves nothing about which code is live."""

    def doubles(cfg):
        ports_double = health_without_a_revision(cfg)
        ports_double["aws"].replies["lambda.get_function"] = {
            "Code": {
                "ResolvedImageUri": "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-gateway:latest"
            }
        }
        return ports_double

    result = run_live_stages(tmp_path, doubles=doubles)
    assert result.code == 1
    errors = {entry["stage"]: entry for entry in result.document.get("errors") or []}
    assert "pinned image digest" in (errors["preflight"]["message"] or "")


def test_a_run_whose_cleanup_cannot_delete_the_instance_is_not_acceptance(tmp_path):
    """A leak that reports clean is the outcome the issue forbids."""

    def doubles(cfg):
        ports_double = live_doubles(cfg)

        def refuse(**_kwargs):
            raise ports.PortError(
                "ec2.terminate_instances failed: UnauthorizedOperation"
            )

        ports_double["aws"].replies["ec2.terminate_instances"] = refuse
        return ports_double

    result = run_live_stages(tmp_path, doubles=doubles)
    assert result.code == 1
    document = result.document
    swept = {
        entry["kind"]: entry["status"] for entry in document["cleanup_results"] or []
    }
    assert swept.get("ec2_instance") == cleanup.FAILED
    assert document["cleanup_ok"] is False


def test_live_ec2_stage_launches_one_tagged_self_terminating_instance(tmp_path):
    """Cost and cleanup guarantees are properties of the launch call itself."""
    launches = []

    def doubles(cfg):
        ports_double = live_doubles(cfg)

        def run_instances(**kwargs):
            launches.append(kwargs)
            return {"Instances": [{"InstanceId": "i-0abc"}]}

        ports_double["aws"].replies["ec2.run_instances"] = run_instances
        return ports_double

    run_live_stages(tmp_path, doubles=doubles)
    assert len(launches) == 1  # exactly one disposable instance
    launch = launches[0]
    assert launch["MinCount"] == launch["MaxCount"] == 1
    assert launch["SubnetId"] == config_fixture()["private_subnet_id"]
    assert launch["InstanceInitiatedShutdownBehavior"] == "terminate"
    assert launch["MetadataOptions"]["HttpTokens"] == "required"
    tags = {t["Key"]: t["Value"] for t in launch["TagSpecifications"][0]["Tags"]}
    assert cleanup.OWNER_TAG in tags  # ownership gating for the recovery sweep
    # The in-guest self-destruct timer, which survives losing the runner entirely.
    assert "shutdown -H" in launch["UserData"]


def test_user_data_arms_the_self_destruct_before_anything_can_fail():
    """Ordering matters: a later failure must not skip arming the timer."""
    text = stages.user_data(config.validate(config_fixture()), "eval-x")
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    shutdown_at = next(i for i, line in enumerate(lines) if "shutdown -H" in line)
    install_at = next(
        (i for i, line in enumerate(lines) if "dnf install" in line), len(lines)
    )
    assert shutdown_at < install_at


def test_worker_payload_travels_by_file_not_argv(tmp_path):
    """A fixture reference in argv is world-readable in a process listing."""
    cfg = config.validate(config_fixture(state_bucket=STATE_BUCKET))
    ssm = FakeSsm(bucket=STATE_BUCKET)
    aws = FakeAws({"s3.put_object": {}})
    wired = live.wire(cfg, {"aws": aws, "ssm": ssm})
    wired["run_worker"](
        "i-1",
        "install_auth",
        {"evaluation_id": EVAL_ID, "secret_ref": "adp/eval/fixture"},
    )
    # The bundle had to be delivered before the purpose could be invoked at all.
    assert ssm.installed and ssm.purposes_run == ["install_auth"]
    joined = ssm.commands[-1]
    assert "EOF_PAYLOAD" in joined  # heredoc, not an argument
    assert "umask 077" in joined
    assert "runuser -l ec2-user" in joined  # never as root
    # The fixture reference reached the worker, but only inside the heredoc file.
    assert ssm.payloads["install_auth"]["secret_ref"] == "adp/eval/fixture"
    argv = joined.split(bundle.DISPATCHER, 1)[1].splitlines()[0]
    assert "adp/eval/fixture" not in argv


def config_keys_in(tree):
    """Every `config["key"]` dereferenced anywhere under an AST node.

    `config["k"]` is a requirement: absent, it raises KeyError. `config.get("k")`
    is not -- the script carries its own default and may legitimately run without
    it. That distinction is the whole signal, so only subscripts count.
    """
    import ast

    return {
        node.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "config"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }


def required_config_keys(source, *, helpers=None):
    """What a shipped script requires, including via the `common` helpers it calls.

    Parsed from the source rather than listed here, because a hand-maintained list
    is exactly what let the gap below exist: a script gained a hard requirement and
    nothing told the orchestrator that assembles its payload.

    `helpers` maps a `common` function name to the keys that function requires.
    Attribution is per-helper, not per-module: `common.install_release` requires
    `source_dir`, but a script that never calls it does not -- unioning all of
    `common`'s keys into every importer would demand `source_dir` from the two
    journeys that install from the served release instead.
    """
    import ast

    tree = ast.parse(source)
    required = config_keys_in(tree)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "common"
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for name in called:
        required |= (helpers or {}).get(name, set())
    return required


def common_helper_requirements(source):
    """Per-function `config[...]` requirements of the shared `common` module."""
    import ast

    return {
        node.name: config_keys_in(node)
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
    }


def test_the_journey_payload_supplies_every_key_a_shipped_script_requires(tmp_path):
    """The payload is complete for every purpose the orchestrator can invoke.

    This is the defect class R1 was an instance of, one layer in. R1 was a remote
    path with no file; this is a remote path whose file exists, is registered, and
    is handed a payload missing a key it dereferences — so the journey dies on a
    KeyError and the ledger records "inference failed" for what is really "the
    orchestrator never assembled this payload".

    It found three: `personal_inference` hard-requires `claude_model`,
    `test_user_id` and `effective_destination_account`, and `_journey_payload`
    supplied none of them. Nothing else in this file could see it, because every
    other test either supplies its own payload or asserts on evidence the worker
    doubles return regardless of what they were sent.

    Derived from a REAL run: the payloads asserted on are the ones the production
    `_journey_payload` produced and SSM carried, and the requirements are parsed
    from the bytes the bundle ships.
    """
    run = run_live_stages(tmp_path, extra=["--suite", "full"])
    ssm = run.ports["ssm"]
    assert ssm.payloads, "no journey was dispatched, so nothing was checked"

    remote = extracted_bundle(tmp_path) / "remote"
    helpers = common_helper_requirements((remote / "common.py").read_text())
    dispatcher = {}
    exec(  # noqa: S102 - the shipped registry, read as the dispatcher reads it
        compile(
            "".join(
                line
                for line in (remote / "dispatcher.py").read_text().splitlines(True)
                if not line.startswith(("import ", "from ", "sys.path"))
            ).split("def load(")[0],
            "dispatcher",
            "exec",
        ),
        dispatcher,
    )

    gaps = {}
    for purpose, payload in sorted(ssm.payloads.items()):
        module, defaults = dispatcher["PURPOSES"][purpose]
        needed = required_config_keys(
            (remote / f"{module}.py").read_text(), helpers=helpers
        )
        # The dispatcher merges its own defaults under the payload, so a key it
        # supplies is not the orchestrator's to supply.
        missing = sorted(needed - set(payload) - set(defaults))
        if missing:
            gaps[purpose] = missing
    assert not gaps, f"journey payloads are missing required keys: {gaps}"


def test_the_journey_payload_carries_the_effective_destination_e06_observed(tmp_path):
    """E08's cross-account assertion must rest on E06's reading, not on config.

    `personal_inference` refuses to grade cross-account inference unless the
    routing rule in force actually points at the destination account. If the
    orchestrator satisfied that by copying `destination_account` out of its own
    config, the check would compare the config against itself and pass on a run
    where ADP was routing somewhere else entirely -- which is the exact failure
    E08 exists to detect.
    """
    run = run_live_stages(tmp_path, extra=["--suite", "full"])
    payload = run.ports["ssm"].payloads["personal_inference"]
    observed = worker_evidence()["bedrock_routing"]["correlation"][
        "bedrock_destination_account"
    ]
    assert payload["effective_destination_account"] == observed
    # It agrees with the configured destination on a healthy run, but it got there
    # by reading the product back.
    assert payload["effective_destination_account"] == payload["destination_account"]


def test_the_session_install_auth_published_is_what_later_journeys_receive(tmp_path):
    """One login, carried forward -- not a second login inside each journey.

    A journey that logged in again would report a login failure as a routing or
    inference failure, so E06/E08/E14 all materialize the session install_auth
    established. That makes install_auth's `session` document load-bearing for
    every later case, and it is asserted here key by key because a silently empty
    one produces a journey that fails for a reason the ledger will misattribute.

    `user_id` is separate from `username` on purpose: the usage log E08 correlates
    against keys on the gateway's id for the identity, which only the gateway can
    tell us.
    """
    run = run_live_stages(tmp_path, extra=["--suite", "full"])
    session = worker_evidence()["install_auth"]["session"]
    for purpose in ("bedrock_routing", "personal_inference", "update_rollback"):
        payload = run.ports["ssm"].payloads[purpose]
        # A reference, not the tokens: evidence leaving the instance is redacted, so
        # forwarding "tokens" here forwarded the literal string "<redacted>". The
        # journey resolves this against its own work directory.
        assert payload["session_ref"] == session["session_ref"]
        assert payload["session_ref"], "no session reference was carried forward"
        assert payload["work_dir"] == stages.WORK_DIR
        # And no token-shaped value travels in the payload at all.
        for key in ("access_token", "id_token", "refresh_token"):
            assert key not in payload
        assert payload["session_expires_at"] == session["expires_at"]
        assert payload["cli_path"] == session["cli_path"]
    inference = run.ports["ssm"].payloads["personal_inference"]
    assert inference["test_user_id"] == session["user_id"] == "user-eval"
    assert inference["test_user"] == session["username"] == "eval-admin"
    # The CLI a later journey is told to run must be outside install_auth's own
    # temporary HOME, which no longer exists by the time that journey starts.
    assert not inference["cli_path"].startswith("/tmp/")


def test_a_routing_rule_pointing_elsewhere_reaches_inference_as_a_mismatch(tmp_path):
    """And when the product reports another account, that is what E08 receives."""
    evidence = worker_evidence()
    evidence["bedrock_routing"] = {
        **evidence["bedrock_routing"],
        "correlation": {
            **evidence["bedrock_routing"]["correlation"],
            "bedrock_destination_account": "210987654321",
        },
    }
    run = run_live_stages(
        tmp_path,
        extra=["--suite", "full"],
        doubles=lambda cfg: live_doubles(cfg, evidence=evidence),
    )
    payload = run.ports["ssm"].payloads["personal_inference"]
    assert payload["effective_destination_account"] == "210987654321"
    assert payload["effective_destination_account"] != payload["destination_account"]


# --------------------------------------------------------------------------
# The shipped script boundary
#
# The other half of the reviewer's requirement: the offline suite must exercise
# "the shipped script/bundle boundary". Everything above drives the orchestrator
# down to SSM; these tests take the bytes that leave for the instance and run
# them, on this machine, in a clean interpreter with only the extracted tree on
# the path. That is what distinguishes "a bundle was uploaded" from "the
# instance can execute these purposes" — the R1 defect was exactly a remote path
# whose file did not exist, and no assertion about the orchestrator could see it.
# --------------------------------------------------------------------------


def extracted_bundle(tmp_path):
    """Unpack the real archive the way the install command does on the instance."""
    import tarfile

    root = tmp_path / "instance"
    if (root / "remote").is_dir():  # already extracted for this tmp_path
        return root
    root.mkdir(parents=True)
    archive = root / "bundle.tar.gz"
    archive.write_bytes(bundle.archive())
    with tarfile.open(archive) as tar:
        tar.extractall(root, filter="data")  # noqa: S202 - our own archive
    return root


def test_the_archive_ships_the_checked_in_bytes_and_nothing_else(tmp_path):
    """What the instance extracts must be what a reviewer read in this repo."""
    root = extracted_bundle(tmp_path)
    shipped = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*.py"))
    }
    assert shipped == dict(bundle.sources())
    # The E04/E05 worker predates remote/ and is imported as a sibling there; if it
    # shipped anywhere else the import would fail only on the instance.
    assert "remote/personal_aws_worker.py" in shipped
    assert (
        shipped["remote/personal_aws_worker.py"]
        == (pathlib.Path(personal_aws_worker.__file__)).read_bytes()
    )


def test_the_archive_is_byte_identical_across_builds():
    """The on-instance `sha256sum -c` is only meaningful if the bytes are stable.

    tar and gzip both embed timestamps by default, so an unpinned archive hashes
    differently every second and the verification the install depends on could
    only ever be a mismatch.
    """
    assert bundle.archive() == bundle.archive()
    assert bundle.digest() == bundle.digest(bundle.archive())


def test_the_extracted_dispatcher_self_checks_in_a_clean_interpreter(tmp_path):
    """The install command's last step, run for real against the shipped tree.

    The instance has no pip step and no ADP package: it receives `remote/` and
    nothing else. So this runs the extracted dispatcher with only that directory
    available, which is the one check that catches a script importing from the
    orchestrator package — an error that would otherwise appear as a journey
    failing three stages later.
    """
    import subprocess
    import sys

    root = extracted_bundle(tmp_path)
    dispatcher = root / "remote" / "dispatcher.py"
    assert dispatcher.is_file()  # `test -f` on the instance checks this same path
    completed = subprocess.run(  # noqa: S603 - our own file, no shell
        [sys.executable, str(dispatcher), "--self-check"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    reported = json.loads(completed.stdout.strip().splitlines()[-1])
    assert reported["problems"] == {}
    assert reported["success"] is True
    # The orchestrator's view of what can run and the instance's own report of
    # what imported must be the same list, or one of them is guessing.
    assert reported["purposes"] == sorted(bundle.purposes())


def test_the_shipped_dispatcher_refuses_a_purpose_it_cannot_run(tmp_path):
    """The unimplemented E09-E13 path as the instance would really answer it."""
    import subprocess
    import sys

    root = extracted_bundle(tmp_path)
    completed = subprocess.run(  # noqa: S603 - our own file, no shell
        [sys.executable, str(root / "remote" / "dispatcher.py"), "hosted_inference"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )
    assert completed.returncode != 0
    reported = json.loads(
        (completed.stdout + completed.stderr).strip().splitlines()[-1]
    )
    assert reported["success"] is False
    assert reported["error_type"] == "UnknownPurpose"
    assert "hosted_inference" in reported["error"]


def test_every_purpose_a_stage_can_ask_for_is_shipped_or_named_as_missing():
    """No third state. A purpose is runnable, or it names the file to write.

    The resolver this replaces returned None, so nine cases reported "no driver"
    while the mapping looked complete and nothing said what was absent.
    """
    asked = set(stages.JOURNEY_DRIVERS.values()) | {
        "install_auth",
        "personal_aws_provision",
        "personal_aws_handoff",
    }
    shipped = set(bundle.purposes())
    for purpose in sorted(asked):
        if purpose in shipped:
            assert bundle.require_purpose(purpose) == purpose
            continue
        with pytest.raises(bundle.BundleError) as raised:
            bundle.require_purpose(purpose)
        assert f"remote/{purpose}.py" in str(raised.value) or purpose in str(
            raised.value
        )
    # Today's honest state, asserted so shipping a script has to update it.
    # E13's `api_parity` has left this list: it is implemented and registered.
    # The five that remain need fixtures that are still BLOCKED (a second
    # destination account, an isolated GitHub App/repo, a hosted queue), and each
    # must be reported as an implementation gap rather than a skip.
    assert sorted(asked - shipped) == [
        "agent_task",
        "bedrock_rungs",
        "github_app",
        "github_login",
        "hosted_inference",
    ]


def test_the_install_verifies_the_digest_before_it_extracts_anything():
    """Order is the property: a substituted object must never be unpacked."""
    commands = bundle.install_commands(
        "bkt", "k/bundle.tar.gz", "d" * 64, region="us-east-1"
    )
    joined = "\n".join(commands)
    verify_at = next(i for i, line in enumerate(commands) if "sha256sum -c" in line)
    extract_at = next(i for i, line in enumerate(commands) if "tar -xzf" in line)
    remove_at = next(i for i, line in enumerate(commands) if line.startswith("rm -rf"))
    assert remove_at < verify_at < extract_at
    assert "set -eu" in commands[0]  # so a failed verification stops the script
    assert "d" * 64 in joined
    # A leftover tree from a previous attempt could otherwise be executed while
    # the digest of what we just shipped still matched.
    assert bundle.REMOTE_DIR + "/remote" in commands[remove_at]


def test_a_registry_entry_with_no_module_is_a_bundle_error(monkeypatch):
    """The failure the registry exists to make loud, forced into existence."""
    monkeypatch.setattr(
        bundle,
        "sources",
        lambda: [("remote/dispatcher.py", b"x"), ("remote/common.py", b"y")],
    )
    with pytest.raises(bundle.BundleError) as raised:
        bundle.purposes()
    assert "not in the tree" in str(raised.value)


# --------------------------------------------------------------------------
# E14 on this machine: the shipped script against the real product CLI
#
# The reviewer's standard for "the shipped script/bundle boundary": one
# SUCCESSFUL path with transport doubles, plus realistic failure paths. So these
# extract the bundle, serve the release under test over loopback HTTP, and run
# `remote/update_rollback.py` — the same bytes the instance receives — against
# the real `install.sh`, the real `adp`, and the real `bg-cognito-auth.sh`.
#
# Only three things are doubled, all of them transports: the gateway's static
# `/cli/<name>` route (a local file server over the git blobs at the revision
# under test), the npm registry (a script that must be asked for the exact pinned
# specs and produces launchers reporting those versions), and the IMDS ownership
# document. Every assertion the case makes is the product's own behaviour --
# `adp update` keeping `*.prev`, `--rollback` refusing on an empty prefix,
# stage-then-commit leaving a failed install untouched, the two setup verbs
# preserving foreign config, and the launchers forwarding `--`.
#
# This is what distinguishes "E14 has a driver registered" from "E14 works": the
# first version of this file passed every orchestrator test while `_alive()`
# reported an already-killed proxy as running, because nothing had ever run it.
# --------------------------------------------------------------------------


SERVER_SOURCE = """
import functools, http.server, socketserver, sys
root, portfile = sys.argv[1], sys.argv[2]
class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass
class Reusable(socketserver.TCPServer):
    allow_reuse_address = True
with Reusable(("127.0.0.1", 0), functools.partial(Quiet, directory=root)) as httpd:
    with open(portfile, "w") as handle:
        handle.write(str(httpd.server_address[1]))
    httpd.serve_forever()
"""


def local_release_server(tmp_path, revision=GOOD_REVISION, *, serve=None):
    """Serve `/api/cli/<name>` from the git blobs at a revision, over loopback.

    A real HTTP origin, not `file://`: the product's proxy refuses a non-http(s)
    `gateway_url`, and `install.sh` fetches with `curl -f`, so a path this does
    not serve produces the genuine 404 the interrupted-install check needs.

    In a SUBPROCESS, deliberately. `--disable-socket` is load-bearing for this
    suite -- it is how a green offline run proves nothing reached AWS or the real
    gateway -- so the guard stays fully armed inside the test process and the only
    thing that ever opens a socket is a child bound to 127.0.0.1 with a directory
    of git blobs behind it. An `allow_hosts` escape would have relaxed the guard
    for every test in the file to serve one of them.
    """
    import subprocess
    import sys
    import time

    files = release.manifest(revision) if serve is None else serve
    root = tmp_path / "gateway"
    directory = root / "api" / "cli"
    directory.mkdir(parents=True)
    for name in files:
        (directory / name).write_bytes(
            release.git_blob(revision, f"{release.CLI_DIR}/{name}")
        )

    portfile = tmp_path / "gateway.port"
    process = subprocess.Popen(  # noqa: S603 - our own source, no shell
        [sys.executable, "-c", SERVER_SOURCE, str(root), str(portfile)],
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if portfile.is_file() and portfile.read_text().strip():
            break
        assert process.poll() is None, "the local release server exited at startup"
        time.sleep(0.05)
    else:  # pragma: no cover - a hung child, reported rather than hanging the run
        process.kill()
        raise AssertionError("the local release server never published its port")
    return process, f"http://127.0.0.1:{portfile.read_text().strip()}/api"


def npm_double(tmp_path):
    """An `npm` that installs no registry package but must be asked correctly.

    It parses `--prefix` and the specs out of the real argv the script builds and
    writes launchers that echo the version in the spec it was given. So "the
    launcher forwarded --version" is still decided by the TOOL's own output, and a
    script that stopped pinning versions -- or asked for a package other than
    @anthropic-ai/claude-code / @openai/codex -- fails here.
    """
    directory = tmp_path / "npm-double"
    directory.mkdir()
    script = directory / "npm"
    script.write_text(
        "#!/bin/sh\n"
        'prefix=""; specs=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    --prefix) prefix="$2"; shift 2 ;;\n'
        "    install|--silent|--global) shift ;;\n"
        '    *) specs="${specs} $1"; shift ;;\n'
        "  esac\n"
        "done\n"
        '[ -n "${prefix}" ] || { echo "npm called with no --prefix" >&2; exit 1; }\n'
        'mkdir -p "${prefix}/bin"\n'
        "for spec in ${specs}; do\n"
        '  version="${spec##*@}"\n'
        '  case "${spec}" in\n'
        "    @anthropic-ai/claude-code@*) name=claude ;;\n"
        "    @openai/codex@*) name=codex ;;\n"
        '    *) echo "unpinned or unexpected spec: ${spec}" >&2; exit 1 ;;\n'
        "  esac\n"
        "  printf '#!/bin/sh\\n"
        '[ "$1" = "--version" ] && echo "%s %s"\\nexit 0\\n\' '
        '"${name}" "${version}" > "${prefix}/bin/${name}"\n'
        '  chmod 755 "${prefix}/bin/${name}"\n'
        "done\n"
    )
    script.chmod(0o755)
    return directory


def shipped_script(tmp_path, name):
    """Import one `remote/<name>.py` from the EXTRACTED bundle, plus its `common`.

    From the archive, not from the repo path, so what runs here is what leaves for
    the instance: a module that is not packaged, or that only imports because this
    repo happens to be on `sys.path`, fails at this line.
    """
    import importlib.util
    import sys

    remote = extracted_bundle(tmp_path) / "remote"
    module = {}
    sys.path.insert(0, str(remote))
    try:
        for target in ("common", name):
            spec = importlib.util.spec_from_file_location(
                f"shipped_{target}", remote / f"{target}.py"
            )
            loaded = importlib.util.module_from_spec(spec)
            # The scripts do `import common`, and the dispatcher puts its own directory
            # first on the path, which is what makes that work on the instance.
            # Mirrored here rather than worked around.
            sys.modules["common" if target == "common" else f"shipped_{target}"] = (
                loaded
            )
            spec.loader.exec_module(loaded)
            module[target] = loaded
    finally:
        sys.path.remove(str(remote))
    return module[name], module["common"]


def seed_session_vault(work_dir, *, tokens=None):
    """Write the private on-instance session vault install_auth would have left.

    E13 and E14 both run AFTER install_auth on the real instance and read their
    session out of that vault rather than out of the payload: the payload only
    carries a reference, because `common.emit()` redacts every credential-shaped
    value on its way off the instance and a payload carrying "tokens" carried the
    literal string "<redacted>". So the offline harness has to establish the same
    precondition — the same path, the same 0600 mode — or it would be testing a
    handoff that production does not perform.

    Returns the reference the payload carries.
    """
    work_dir = pathlib.Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    path = work_dir / "session.json"
    path.write_text(
        json.dumps(
            tokens
            if tokens is not None
            else {
                "access_token": "<token>",
                "id_token": "<id>",
                "refresh_token": "<refresh>",
                "expires_at": NOW + 3600,
            }
        )
    )
    path.chmod(0o600)
    return str(path)


def shipped_update_rollback(tmp_path):
    return shipped_script(tmp_path, "update_rollback")


def run_e14(
    tmp_path, *, revision=GOOD_REVISION, serve=None, overrides=None, session=True
):
    """Run the shipped E14 script end to end through its production entry point.

    `common.run_script` is the entry point the dispatcher calls, so the ownership
    assertion, the payload file and the always-emit-evidence contract are all
    exercised -- not just `execute`.

    `session=False` withholds the vault install_auth would have written, which is
    how the "E13-E14 ran without their dependency" case is expressed now that the
    tokens are not payload fields that can be blanked.
    """
    script, common = shipped_update_rollback(tmp_path)
    server, gateway = local_release_server(tmp_path, revision, serve=serve)
    npm = npm_double(tmp_path)
    previous_path = None
    try:
        import os

        previous_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{npm}:{previous_path}"
        # The instance ownership check reads IMDS; there is none here, and
        # `--disable-socket` would refuse the request anyway. This is a transport
        # double, and the assertion it feeds still runs.
        common.instance_identity = lambda: {
            "instanceId": "i-0eval",
            "accountId": "879318057152",
        }
        payload = tmp_path / "payload.json"
        work_dir = tmp_path / "adp-eval"
        reference = (
            seed_session_vault(work_dir) if session else str(work_dir / "session.json")
        )
        document = {
            "instance_id": "i-0eval",
            "platform_account": "879318057152",
            "gateway_url": gateway,
            "region": "us-east-1",
            "sts_endpoint": "https://sts-fips.us-east-1.amazonaws.com",
            # A reference to the private on-instance vault, exactly as the
            # orchestrator supplies it. The tokens themselves are NOT in the
            # payload: evidence leaving the instance is redacted, so a session
            # carried in the document arrived downstream as "<redacted>".
            "session_ref": reference,
            "work_dir": str(work_dir),
            "session_expires_at": NOW + 3600,
            "evaluation_id": EVAL_ID,
            "expected_hashes": release.manifest(revision),
            "claude_version": "2.1.236",
            "codex_version": "0.154.0",
            # Not 9191: a hardcoded port in the product or the script would pass
            # against the default and fail here.
            "proxy_port": 9273,
        }
        document.update(overrides or {})
        payload.write_text(json.dumps(document))
        code = common.run_script(script.execute, [str(payload)])
        return code, document
    finally:
        server.kill()
        server.wait()
        if previous_path is not None:
            import os

            os.environ["PATH"] = previous_path


def e14_evidence(capsys):
    """The one JSON document the script emits, as the orchestrator reads it."""
    for line in reversed(capsys.readouterr().out.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise AssertionError("the shipped script emitted no JSON document")


def test_the_shipped_e14_script_drives_the_real_cli_through_every_check(
    tmp_path, capsys
):
    """The successful path, with only transports doubled.

    Every one of the six checks is a real product command against a real
    installation: `adp update`, `adp update --rollback` twice, a failing install
    at a live prefix, `adp codex setup`, `adp claude setup`, and both launchers.
    """
    code, document = run_e14(tmp_path)
    evidence = e14_evidence(capsys)
    assert code == 0, evidence.get("error")
    assert evidence["success"] is True and evidence["stage"] == "complete"
    assert evidence["checks"] == [
        "update_lands_release_and_keeps_previous",
        "rollback_restores_then_refuses_without_previous",
        "interrupted_install_preserves_usable_installation",
        "codex_setup_preserves_foreign_config_and_reruns_clean",
        "claude_setup_merges_and_reruns_clean",
        "launchers_forward_arguments_and_refuse_dead_sessions",
    ]

    # `adp update` kept every installed file's outgoing copy and landed the
    # release: nine files install (install.sh installs no copy of itself).
    update = evidence["update"]
    assert update["installed_matches_release"] is True
    assert update["previous_missing"] == [] and update["previous_wrong_bytes"] == []
    assert len(update["previous_kept"]) == len(release_fixture()) - 1

    # `--rollback` restored the exact bytes, then REFUSED with the product's own
    # message once no .prev remained.
    rollback = evidence["rollback"]
    assert rollback["restored_previous_bytes"] is True
    assert rollback["prev_files_left"] == []
    assert rollback["second_rollback_exit_code"] != 0
    assert rollback["second_rollback_refused_with_message"] is True

    # A failing install left the working installation byte-identical and usable,
    # with no staged temporaries: install.sh's stage-then-commit, proved.
    interrupted = evidence["interrupted_install"]
    assert interrupted["refused"] is True
    assert interrupted["installation_unchanged"] is True
    assert interrupted["still_matches_release"] is True
    assert interrupted["staged_temporaries_left"] == []

    # Both setup verbs own only their own keys, and the configured port reaches
    # the Codex provider block.
    assert evidence["codex_setup"]["base_url_port"] == document["proxy_port"]
    assert evidence["codex_setup"]["rerun_byte_identical"] is True
    assert evidence["claude_setup"]["foreign_env_preserved"] is True
    assert evidence["claude_setup"]["rerun_byte_identical"] is True

    # The launchers forwarded to the pinned releases, the proxy `adp codex`
    # started was stopped, and a dead session refused.
    launch = evidence["launch"]
    assert launch["claude"]["forwarded_version_reported"] is True
    assert launch["codex"]["forwarded_version_reported"] is True
    assert launch["codex"]["proxy_started"] and launch["codex"]["proxy_stopped"]
    assert launch["dead_session_refused"] is True

    assert evidence["correlation"] == {
        "update_release_files": len(release_fixture()) - 1,
        "update_proxy_port": document["proxy_port"],
    }
    # Nothing here creates an AWS resource, so nothing may be recorded for sweeping.
    assert "resources" not in evidence


def test_a_stopped_proxy_is_recognised_as_stopped_even_as_a_zombie(tmp_path):
    """The defect this section found, pinned so it cannot come back.

    `adp codex` starts the proxy under `setsid nohup`, so it is reparented to pid
    1 and stays a ZOMBIE between exiting and being reaped -- and a zombie still
    accepts `kill(pid, 0)`. The first version of this probe used only that signal,
    so a proxy it had successfully killed read as running until the 10s wait
    expired and E14 failed with "the proxy could not be stopped". Nothing above
    this line could see it: only running the script does.

    Asserted against `common`, because E08 shares this teardown: `adp codex` there
    starts the same proxy, and the `adp serve --stop` it used to call to stop it
    does not exist as a verb, so its cleanup was a no-op.
    """
    import subprocess
    import sys
    import time

    _script, shared = shipped_update_rollback(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if shared.process_state(child.pid) == "Z":
                break
            time.sleep(0.05)
        assert shared.process_state(child.pid) == "Z", "no zombie to test against"
        # The signal probe alone still says "alive" -- that is the trap.
        import os

        os.kill(child.pid, 0)
        assert shared.process_alive(child.pid) is False
    finally:
        child.wait()
    assert shared.process_alive(child.pid) is False


def test_stop_proxy_reports_failure_when_there_is_no_pidfile_to_stop_by(tmp_path):
    """A proxy that was never recorded is not a proxy that was stopped.

    `adp serve` writes the pidfile before it execs, so no pidfile means either
    nothing started or the start failed -- and both are results a journey must see
    rather than a silent True. This is the distinction the removed
    `adp serve --stop` call could not make: an unknown verb exits non-zero, and the
    old call ignored the exit code entirely.
    """
    _script, shared = shipped_update_rollback(tmp_path)
    assert shared.stop_proxy(tmp_path / "empty-home") is False

    home = tmp_path / "home"
    (home / ".bedrock-gateway").mkdir(parents=True)
    (home / ".bedrock-gateway" / "proxy.pid").write_text("not-a-pid\n")
    assert shared.stop_proxy(home) is False


def test_stop_proxy_kills_a_real_listener_recorded_in_the_pidfile(tmp_path):
    """The path E08 and E14 both take: pidfile -> signal -> process is gone."""
    import subprocess
    import sys

    _script, shared = shipped_update_rollback(tmp_path)
    home = tmp_path / "home"
    (home / ".bedrock-gateway").mkdir(parents=True)
    # A process that will not exit on its own, so a passing result can only mean
    # `stop_proxy` ended it.
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"]
    )
    try:
        (home / ".bedrock-gateway" / "proxy.pid").write_text(f"{child.pid}\n")
        assert shared.process_alive(child.pid) is True
        assert shared.stop_proxy(home) is True
    finally:
        child.kill()
        child.wait()


def test_stop_proxy_runtime_reaches_a_named_deployments_own_runtime_directory(tmp_path):
    """#5413: three deployments mean three proxies, none at the legacy path.

    A named deployment keeps its runtime files in
    `~/.adp/deployments/<id>/runtime`, so the HOME-based `stop_proxy` would look
    in `~/.bedrock-gateway`, find no pidfile, and return False — reported as "the
    proxy could not be stopped" while three real listeners stayed up holding their
    ports against the next attempt on the same instance.

    Both entry points must run the same signal-and-confirm logic, which is why
    this is a split and not a second implementation: `stop_proxy` is now a wrapper
    that supplies the legacy directory.
    """
    import subprocess
    import sys

    _script, shared = shipped_update_rollback(tmp_path)
    runtime = tmp_path / "home" / ".adp" / "deployments" / "dep-abc123" / "runtime"
    runtime.mkdir(parents=True)
    # No pidfile is still "nothing was stopped", at either path.
    assert shared.stop_proxy_runtime(runtime) is False

    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"]
    )
    try:
        (runtime / "proxy.pid").write_text(f"{child.pid}\n")
        assert shared.process_alive(child.pid) is True
        assert shared.stop_proxy_runtime(runtime) is True
        assert shared.process_alive(child.pid) is False
    finally:
        child.kill()
        child.wait()


def test_stop_proxy_still_serves_the_legacy_layout_through_the_new_helper(tmp_path):
    """E08 and E14 call `stop_proxy(home)` and must keep working unchanged."""
    import subprocess
    import sys

    _script, shared = shipped_update_rollback(tmp_path)
    home = tmp_path / "legacy-home"
    (home / ".bedrock-gateway").mkdir(parents=True)
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"]
    )
    try:
        (home / ".bedrock-gateway" / "proxy.pid").write_text(f"{child.pid}\n")
        assert shared.stop_proxy(home) is True
    finally:
        child.kill()
        child.wait()


def test_e14_fails_when_the_installed_bytes_are_not_the_release_under_test(
    tmp_path, capsys
):
    """A part-rolled-out gateway must not be graded as a working update.

    The instance is given an independently derived manifest and the gateway serves
    something else for one helper. `install.sh` and `adp update` both report
    success -- they only check that a download arrived and starts with a shebang
    -- so the ONLY thing between a mixed deployment and a green E14 is this script
    comparing installed bytes against that manifest. R10 in miniature, at the
    boundary where it actually bites.
    """
    drifted = dict(release_fixture())
    drifted["adp-aws.py"] = "0" * 64
    code, _document = run_e14(tmp_path, overrides={"expected_hashes": drifted})
    evidence = e14_evidence(capsys)
    assert code != 0
    assert evidence["success"] is not True
    # It fails at the FIRST comparison, before it has mutated anything, and names
    # the file that drifted rather than reporting "update failed".
    assert evidence["stage"] == "install"
    assert "does not match the release under test" in evidence["error"]
    assert "adp-aws.py" in evidence["error"]


def test_e14_fails_when_the_gateway_serves_no_release_at_all(tmp_path, capsys):
    """Delivery failure at the boundary: nothing installed, nothing claimed.

    `install.sh` is served (so the script gets its installer) but the helpers are
    not, so the install itself fails. The script must report that rather than
    proceed to grade an empty prefix.
    """
    code, _document = run_e14(tmp_path, serve=("install.sh",))
    evidence = e14_evidence(capsys)
    assert code != 0
    assert evidence["success"] is not True
    assert evidence["stage"] == "install"
    assert evidence["error_type"] == "RemoteError"


def test_e14_refuses_to_run_off_the_instance_the_harness_created(tmp_path, capsys):
    """The same EC2-only rule every other remote script obeys.

    Asserted through `run_script`, because that is where the check lives: a
    script invoked with a payload for another instance must emit non-successful
    evidence and touch nothing.
    """
    code, _document = run_e14(tmp_path, overrides={"instance_id": "i-somebodyelse"})
    evidence = e14_evidence(capsys)
    assert code != 0
    assert evidence["success"] is not True
    assert evidence["stage"] == "start"  # never got as far as installing
    assert "not running on the instance" in evidence["error"]


def test_e14_without_a_release_manifest_refuses_rather_than_grading_nothing(
    tmp_path, capsys
):
    """No expected hashes means no way to tell an update from a no-op.

    R10's failure in miniature: without an independently derived manifest the
    only thing left to compare against is what the gateway just served, which
    proves nothing. So the script must refuse.
    """
    code, _document = run_e14(tmp_path, overrides={"expected_hashes": {}})
    evidence = e14_evidence(capsys)
    assert code != 0
    assert "No expected release hashes" in evidence["error"]


def test_e14_without_a_session_refuses_rather_than_reporting_setup_failures(
    tmp_path, capsys
):
    """E14 depends on E01-E03; an absent session is a dependency error, not a bug.

    Both setup verbs and both launchers call `require_session`, so with no token
    they would fail for a reason that has nothing to do with update or rollback.
    Saying so up front is what keeps the report honest about which case broke.
    """
    code, _document = run_e14(tmp_path, session=False)
    evidence = e14_evidence(capsys)
    assert code != 0
    assert "install_auth" in evidence["error"]


# --------------------------------------------------------------------------
# The session install_auth hands to every later journey
#
# E01-E03 run in a temporary HOME that is deleted when the journey returns, and
# everything after them reuses the session it established rather than logging in
# again. So the handoff document is load-bearing: an empty or temp-scoped one
# makes E06/E08/E14 fail for a reason the ledger attributes to routing or
# inference. Driven against a real installed release, because the durability
# property -- the CLI still being there afterwards -- is a filesystem fact.
# --------------------------------------------------------------------------


def install_auth_session(tmp_path, *, tokens=None, config_overrides=None):
    """Run the shipped `_session_document` over a really-installed release."""
    import os

    script, common = shipped_script(tmp_path, "install_auth")
    server, gateway = local_release_server(tmp_path)
    try:
        home = tmp_path / "home"
        prefix = home / ".adp" / "bin"
        prefix.mkdir(parents=True)
        # Installed by the release's own installer, so `cli_path` names a real
        # executable rather than a file this test wrote.
        installer = tmp_path / "install.sh"
        installer.write_bytes(
            release.git_blob(GOOD_REVISION, f"{release.CLI_DIR}/install.sh")
        )
        code, _out, _err = common.bounded(
            [
                "sh",
                str(installer),
                "--prefix",
                str(prefix),
                "--gateway-url",
                gateway,
                "--no-path-edit",
            ],
            env={
                "HOME": str(home),
                "PATH": os.environ["PATH"],
                "ADP_CONFIG_DIR": str(home / ".bedrock-gateway"),
            },
            timeout=300,
        )
        assert code == 0, "the release under test did not install"

        directory = home / ".bedrock-gateway"
        directory.mkdir(mode=0o700, exist_ok=True)
        (directory / "tokens.json").write_text(
            json.dumps(
                tokens
                if tokens is not None
                else {
                    "access_token": "<token>",
                    "id_token": "<id>",
                    "refresh_token": "<refresh>",
                    "expires_at": NOW + 3600,
                }
            )
        )
        evidence = {
            "login": {
                "username": "eval-admin",
                "user_id": "user-eval",
                "org_id": "org-eval",
            }
        }
        # `work_dir`, exactly as `execute` passes it: the run-owned durable
        # directory, NOT its `cli` subdirectory. `_session_document` derives both
        # the preserved CLI and the vault from this one value, so the two must be
        # the same value production uses or the vault lands where nothing reads it.
        script._session_document(
            {"work_dir": str(tmp_path / "adp-eval"), **(config_overrides or {})},
            evidence,
            prefix,
            home,
            str(tmp_path / "adp-eval"),
        )
        return evidence["session"], home
    finally:
        server.kill()
        server.wait()


def test_the_vault_lands_where_the_next_journey_is_told_to_look(tmp_path):
    """The reference published and the path a journey resolves must be one path.

    `_session_document` derives BOTH the preserved CLI (`work_dir/cli`) and the
    vault (`work_dir/session.json`) from a single `work_dir`, and the orchestrator
    tells later journeys to look under `stages.WORK_DIR`. Pass the wrong level here
    — the `cli` subdirectory rather than the work directory — and the CLI lands at
    `work_dir/cli/cli` while the vault lands where nothing reads it. That was a
    real defect in this change, caught only because `load_session` compares the
    whole resolved path; this pins the contract so it cannot come back.
    """
    from pathlib import Path

    _script, common = shipped_script(tmp_path, "install_auth")
    work_dir = tmp_path / "adp-eval"
    session, _home = install_auth_session(tmp_path)

    assert session["session_ref"] == str(work_dir / common.SESSION_VAULT)
    # The CLI is one level down from the vault, not two.
    assert Path(session["cli_path"]) == work_dir / "cli" / "adp"
    # And the reference resolves under the work_dir the payload carries.
    assert common.load_session(
        {"session_ref": session["session_ref"], "work_dir": str(work_dir)}
    )["access_token"]


def test_the_orchestrator_and_the_instance_agree_on_the_work_directory():
    """One directory, named once. Two spellings of it is a silent broken handoff.

    `stages.WORK_DIR` creates the 0700 run directory and is what the journey
    payload advertises, so if it ever diverges from the remote bundle's own idea of
    where it lives, the vault reference resolves to nothing on a real instance
    while every offline test still passes.
    """
    assert stages.WORK_DIR == bundle.REMOTE_DIR


def test_the_handed_over_cli_outlives_the_journey_that_installed_it(tmp_path):
    """The defect this closes: `cli_path` pointing into a deleted temp HOME.

    install_auth's HOME is a `TemporaryDirectory`, so a session document naming the
    binary inside it hands every later journey a path that does not exist by the
    time they run. The CLI is therefore copied somewhere durable, and this asserts
    the copy is real and executable AFTER the installing HOME is gone.
    """
    import os
    import shutil
    from pathlib import Path

    session, home = install_auth_session(tmp_path)
    shutil.rmtree(home)

    handed = Path(session["cli_path"])
    assert handed.is_file() and os.access(handed, os.X_OK)
    assert str(home) not in session["cli_path"]
    # A whole install, not just the entry point: `adp` dispatches to its helpers.
    installed = {path.name for path in handed.parent.iterdir()}
    assert {"adp", "adp_common.py", "bg-cognito-auth.sh"} <= installed


def test_the_session_document_carries_the_tokens_the_product_wrote(tmp_path):
    """Read from the CLI's own token file, not from anything the harness asserts.

    The tokens are no longer IN the exported document — `emit()` redacts every
    credential-shaped value, which turned them into the literal "<redacted>" and
    left later journeys authenticating with a truthy placeholder. The document now
    carries a reference, so this asserts the reference resolves to the material the
    product actually wrote.
    """
    script, common = shipped_script(tmp_path, "install_auth")
    session, _home = install_auth_session(tmp_path)
    stored = common.load_session(
        {"session_ref": session["session_ref"], "work_dir": str(tmp_path / "adp-eval")}
    )
    assert stored["access_token"] == "<token>"
    assert stored["id_token"] == "<id>"
    assert stored["refresh_token"] == "<refresh>"
    assert session["expires_at"] == NOW + 3600
    # The gateway's id for the identity, which is what E08 correlates usage on.
    # Non-secret, so these stay in the exported document.
    assert session["user_id"] == "user-eval"
    assert session["username"] == "eval-admin"
    assert session["org_id"] == "org-eval"
    assert hasattr(script, "_session_document")


def test_the_exported_session_document_carries_no_token_at_all(tmp_path):
    """The defect this closes: tokens in evidence become "<redacted>" downstream.

    `emit()` is the only way evidence leaves the instance and it redacts anything
    matching `SENSITIVE_KEY`. So a token in the session document could never arrive
    intact — it arrived as a ten-character truthy string that passed every
    `require()` and then failed authentication downstream, where the cause was no
    longer visible.

    Asserted both ways round: no token-shaped value is exported, AND the reference
    that IS exported survives redaction unchanged. A reference that were itself
    redacted would be just as broken as the tokens were.
    """
    _script, common = shipped_script(tmp_path, "install_auth")
    session, _home = install_auth_session(tmp_path)

    serialized = json.dumps(session)
    for secret in ("<token>", "<id>", "<refresh>"):
        assert secret not in serialized, "session material is still being exported"
    for key in ("access_token", "id_token", "refresh_token"):
        assert key not in session

    assert common.redact(session) == session, (
        "the exported session reference is itself redacted, so the handoff would carry a placeholder exactly as the tokens did"
    )


def test_a_journey_that_cannot_find_the_session_fails_naming_that(tmp_path):
    """A missing session must be loud, not degraded into a downstream defect.

    Without this, a vault that did not survive the stage boundary would surface as
    a routing or inference failure — attributing a broken handoff to the product
    under test, which is the ambiguity this harness exists to remove.
    """
    _script, common = shipped_script(tmp_path, "install_auth")
    work_dir = tmp_path / "adp-eval"
    work_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(Exception, match="no session_ref"):
        common.load_session({"work_dir": str(work_dir)})

    # A reference that names the vault but whose file is gone.
    with pytest.raises(Exception, match="did not survive the stage boundary"):
        common.load_session(
            {
                "session_ref": str(work_dir / common.SESSION_VAULT),
                "work_dir": str(work_dir),
            }
        )


def test_a_session_reference_cannot_read_an_arbitrary_file(tmp_path):
    """The reference arrives in a payload, so it is input, not a trusted path.

    Checking only the FILENAME would leave the interesting cases open: a vault
    name in another directory, or a traversal out of the work directory and back.
    So each of those is exercised, not just the obvious `/etc/passwd` shape.
    """
    _script, common = shipped_script(tmp_path, "install_auth")
    work_dir = tmp_path / "adp-eval"
    work_dir.mkdir(parents=True, exist_ok=True)
    seed_session_vault(work_dir, tokens={"access_token": "<real>"})

    elsewhere = tmp_path / "id_rsa"
    elsewhere.write_text(json.dumps({"access_token": "stolen"}))
    # Same basename the check looks for, but outside this run's work directory.
    planted = tmp_path / "elsewhere"
    planted.mkdir()
    (planted / common.SESSION_VAULT).write_text(json.dumps({"access_token": "stolen"}))

    for reference in (
        elsewhere,
        planted / common.SESSION_VAULT,
        work_dir / ".." / "elsewhere" / common.SESSION_VAULT,
    ):
        with pytest.raises(Exception, match="does not name this run's session vault"):
            common.load_session(
                {"session_ref": str(reference), "work_dir": str(work_dir)}
            )

    # The run's own vault still resolves, so the guard is not simply refusing all.
    assert (
        common.load_session(
            {
                "session_ref": str(work_dir / common.SESSION_VAULT),
                "work_dir": str(work_dir),
            }
        )["access_token"]
        == "<real>"
    )


def test_the_stored_session_is_private_to_the_run_user(tmp_path):
    """0600: another local user on the instance must not be able to read it."""
    import os

    _script, common = shipped_script(tmp_path, "install_auth")
    session, _home = install_auth_session(tmp_path)
    mode = os.stat(session["session_ref"]).st_mode & 0o777
    assert mode == 0o600, f"session vault is {oct(mode)}, not 0600"


def test_a_login_that_persisted_no_token_is_refused_not_handed_on(tmp_path):
    """An empty session must stop here, where the cause is still visible.

    Handing `access_token: ""` forward would make three later journeys fail on
    their own `require`s, and the ledger would record routing and inference
    failures for what is really a login that did not persist.
    """
    script, _common = shipped_script(tmp_path, "install_auth")
    with pytest.raises(Exception, match="no access token"):
        install_auth_session(tmp_path, tokens={"access_token": "", "expires_at": 0})
    assert hasattr(script, "_session_document")


def test_a_fixture_identity_is_never_marked_as_this_runs_to_delete(tmp_path):
    """`created_username` is what makes a Cognito user this run's to remove.

    The stage records a cleanup entry from it, so defaulting it to the login
    username would schedule the shared fixture administrator for deletion.
    """
    session, _home = install_auth_session(tmp_path)
    assert session["created_username"] == ""
    assert session["username"] == "eval-admin"

    created, _home = install_auth_session(
        tmp_path / "created", config_overrides={"created_username": "eval-run-user"}
    )
    assert created["created_username"] == "eval-run-user"


# --------------------------------------------------------------------------
# Removing what E06 registers
#
# A successful E06 leaves a destination row and a routing rule pointing at it.
# The deleter is production code driven here through the modelled routing API,
# because the property is a SEQUENCE (rules first, then the row) that a per-URL
# stub answering 204 to everything could not distinguish from a broken order.
# --------------------------------------------------------------------------


class VaultSsm:
    """An instance that will read out its session vault, and nothing else.

    The cleanup sweep terminates the instance FIRST (it holds the ENI), so the API
    deleters that run after it cannot resolve the session on-instance the way a
    journey does. Production therefore reads the vault EAGERLY, when the deleters
    are built, which is before the sweep terminates anything. This double is that
    transport, and it is strict about the purpose so a deleter that started running
    journeys would be caught.

    `terminated` models the part that made this a live failure: once the instance
    is gone the read cannot succeed, so a read attempted too late raises exactly as
    the real SSM transport does.
    """

    def __init__(self, *, token="<token>", fail=False):
        self.token = token
        self.fail = fail
        self.reads = []
        self.terminated = False

    def run(self, instance_id, commands, *, purpose, timeout=600):
        raise AssertionError(f"the deleters must not run {purpose!r} on the instance")

    def json_result(self, instance_id, commands, *, purpose, timeout=600):
        assert purpose == "session_handoff", (
            f"the deleters resolved their session under purpose {purpose!r}"
        )
        joined = "\n".join(commands)
        self.reads.append((instance_id, joined))
        if self.fail or self.terminated:
            raise ports.PortError("the instance is gone")
        return {"Status": "Success"}, {"access_token": self.token}


def destination_deleter(
    gateway, *, prefix=EVAL_ID, cfg=None, ssm=None, kind="bedrock_destination"
):
    """One production API deleter, over a modelled gateway.

    `kind` selects which: `bedrock_destination`, `adp_connection` and `adp_user`
    are all built by the same factory and share the same session plumbing, so they
    share this harness rather than each getting a near-copy of it.
    """
    cfg = config.validate(cfg or config_fixture())
    http = FakeHttp(gateway=gateway)
    ssm = ssm if ssm is not None else VaultSsm()
    build = live.wire(cfg, {"aws": FakeAws(), "http": http, "ssm": ssm})["deleters"]
    # What the run document really holds after install_auth: a reference, never a
    # token. The token lives in the on-instance vault and is fetched over `ssm`.
    ctx = {
        "document": {
            "instance_id": "i-0eval",
            "session": {
                "session_ref": "/home/ec2-user/adp-eval/session.json",
                "prefix": prefix,
            },
        }
    }
    return build(cfg, ctx)[kind], http


def test_removing_a_destination_drops_its_routing_rules_first(tmp_path):
    """The gateway refuses to unlink a destination a rule still names.

    So the order is the whole behaviour: a deleter that went straight for the row
    would take a 409 and report a leak on a resource it could have removed.
    """
    gateway = FakeGateway()
    gateway.register("dest-1", label=f"{EVAL_ID}-dest", scope="user:u-1", grant=True)
    delete, http = destination_deleter(gateway)
    delete("dest-1")
    assert gateway.unlinked == ["dest-1"]
    assert gateway.mappings == []  # no rule left pointing at an eval destination
    assert gateway.destinations == {}
    # The rule really was deleted through the API, not just dropped locally.
    assert any("/mappings/user%3Au-1" in url for url in http.urls)


def test_a_destination_with_no_connection_grant_is_reported_as_a_product_gap():
    """`adp admin bedrock connect` registers a row nothing can delete.

    `DELETE /connection-links/{id}` serves only rows with a connection grant, and
    the gateway exposes no other delete for a destination. Treating its 404 as
    "already gone" would report a clean sweep over a row that is still registered
    and still routable, so this must fail — and its routing rules must still be
    gone, because that is the part that affects traffic.
    """
    gateway = FakeGateway()
    gateway.register("dest-1", label=f"{EVAL_ID}-dest", scope="org:o-1", grant=False)
    delete, _http = destination_deleter(gateway)
    with pytest.raises(ports.PortError) as raised:
        delete("dest-1")
    assert "must be removed by a platform admin" in str(raised.value)
    assert gateway.mappings == []  # traffic can no longer reach it
    assert "dest-1" in gateway.destinations  # but the row is honestly still there


def test_a_destination_another_run_created_is_never_removed():
    """Ownership is the label carrying THIS run's evaluation ID, nothing weaker."""
    gateway = FakeGateway()
    gateway.register("dest-9", label="adp-e2e-20260101-000000-zzzzzz-dest", grant=True)
    delete, _http = destination_deleter(gateway)
    with pytest.raises(ports.PortError) as raised:
        delete("dest-9")
    assert "did not create" in str(raised.value)
    assert gateway.unlinked == []


def aws_deleters(replies):
    """The production deleter mapping over a canned AWS double."""
    cfg = config.validate(config_fixture())
    return live.wire(cfg, {"aws": FakeAws(replies), "http": FakeHttp()})["deleters"](
        cfg, None
    )


def test_a_cognito_identity_already_deleted_is_a_successful_cleanup():
    """A retried or resumed sweep must not fail over a user that is gone.

    Verified against the live pool: `AdminDeleteUser` raises UserNotFoundException
    for an absent user rather than succeeding quietly. `sweep()` records a raising
    deleter as FAILED and a single failure makes `cleanup_ok` false, so without
    this a recovery sweep over an already-clean run would DENY acceptance for a
    resource that is not there — while a genuinely undeleted user reported clean
    would be the opposite and worse failure. Hence: absence is success, every other
    error still raises.
    """
    delete = aws_deleters(
        {
            "cognito-idp.admin_delete_user": lambda **_k: (_ for _ in ()).throw(
                ports.PortError("cognito-idp.admin_delete_user failed: UserNotFound")
            )
        }
    )["cognito_user"]
    delete(
        "us-east-1_JEhv9xSGG/adp-e2e-gone", account=PLATFORM_ACCOUNT
    )  # must not raise

    # Any OTHER failure is still a real one: a pool we cannot reach must not be
    # reported as a user successfully removed.
    denied = aws_deleters(
        {
            "cognito-idp.admin_delete_user": lambda **_k: (_ for _ in ()).throw(
                ports.PortError("cognito-idp.admin_delete_user failed: AccessDenied")
            )
        }
    )["cognito_user"]
    with pytest.raises(ports.PortError, match="AccessDenied"):
        denied("us-east-1_JEhv9xSGG/adp-e2e-live", account=PLATFORM_ACCOUNT)


def test_a_run_owned_secret_already_deleted_is_a_successful_cleanup():
    """Same rule for the run-owned fixture secret, for the same reason."""
    delete = aws_deleters(
        {
            "secretsmanager.delete_secret": lambda **_k: (_ for _ in ()).throw(
                ports.PortError("secretsmanager.delete_secret failed: ResourceNotFound")
            )
        }
    )["secret"]
    delete(
        "adp/cli-uplift-eval/adp-e2e-gone", account=PLATFORM_ACCOUNT
    )  # must not raise

    denied = aws_deleters(
        {
            "secretsmanager.delete_secret": lambda **_k: (_ for _ in ()).throw(
                ports.PortError("secretsmanager.delete_secret failed: AccessDenied")
            )
        }
    )["secret"]
    with pytest.raises(ports.PortError, match="AccessDenied"):
        denied("adp/cli-uplift-eval/adp-e2e-live", account=PLATFORM_ACCOUNT)


def test_every_resource_kind_treats_absence_as_a_successful_cleanup():
    """The rule has to hold for the kinds swept FIRST, not just the last two.

    `cleanup.ORDER` deletes the instance, profile, role and security group before
    the secret and the Cognito user, and `sweep()` marks a resource deleted only
    AFTER the delete returns — with a best-effort durable push behind that mark
    (`critical=False`). So a run cancelled mid-sweep leaves the durable manifest
    saying `pending` for exactly these early kinds, and the `always()` recover job
    deletes them a second time. They are the MOST exposed to double deletion, so
    covering only the secret and the user would have left the real gap open.
    """
    absent = {
        "iam_instance_profile": (
            "iam.delete_instance_profile",
            "NoSuchEntity",
            "adp-e2e-gone",
        ),
        "security_group": (
            "ec2.delete_security_group",
            "InvalidGroup.NotFound",
            "sg-0abc",
        ),
        "ec2_instance": (
            "ec2.terminate_instances",
            "InvalidInstanceID.NotFound",
            "i-0abc",
        ),
        "cloudformation_stack": (
            "cloudformation.delete_stack",
            "ValidationError",
            "adp-e2e-stack",
        ),
        "iam_role": (
            "iam.list_attached_role_policies",
            "NoSuchEntity",
            "adp-e2e-role",
        ),
    }
    for kind, (operation, code, identifier) in absent.items():
        deleters = aws_deleters(
            {
                operation: lambda _c=code, _o=operation, **_k: (_ for _ in ()).throw(
                    ports.PortError(f"{_o} failed: {_c}")
                )
            }
        )
        # Must not raise: absence is the end state the deleter exists to reach.
        deleters[kind](identifier, account=PLATFORM_ACCOUNT, region="us-east-1")

        # ...and the same call denied must still raise, so a resource we were
        # forbidden to delete is never recorded as successfully removed.
        denied = aws_deleters(
            {
                operation: lambda _o=operation, **_k: (_ for _ in ()).throw(
                    ports.PortError(f"{_o} failed: AccessDenied")
                )
            }
        )
        with pytest.raises(ports.PortError, match="AccessDenied"):
            denied[kind](identifier, account=PLATFORM_ACCOUNT, region="us-east-1")


def test_a_role_we_are_forbidden_to_read_is_not_reported_as_deleted():
    """The absence poll must not accept AccessDenied as "gone".

    `delete_role` verified removal with a bare `except PortError: return True`, so
    an AccessDenied on `get_role` — a role still very much present — satisfied the
    check and the sweep reported a clean teardown over a live IAM role. Now only
    NoSuchEntity counts as gone and the denial propagates.
    """
    calls = []

    def denied(**_kwargs):
        raise ports.PortError("iam.get_role failed: AccessDenied")

    def observe(operation):
        def reply(**_kwargs):
            calls.append(operation)
            return {}

        return reply

    deleters = aws_deleters(
        {
            "iam.list_attached_role_policies": observe("list_attached"),
            "iam.list_role_policies": observe("list_inline"),
            "iam.delete_role": observe("delete"),
            "iam.get_role": denied,
        }
    )
    # The denial itself surfaces, rather than being converted into a timeout after
    # a full absence poll: it is the more accurate error and it fails fast.
    with pytest.raises(ports.PortError, match="AccessDenied"):
        deleters["iam_role"](
            "adp-e2e-role", account=PLATFORM_ACCOUNT, region="us-east-1"
        )
    assert "delete" in calls


def test_a_stack_we_are_forbidden_to_describe_is_not_reported_as_deleted():
    """Same masking bug on the CloudFormation poll, which is the R7 surface.

    The stack lives in the DESTINATION account, so an AccessDenied on
    `describe_stacks` is the precise symptom of deleting with the wrong account's
    credentials — the failure this deleter's account scoping exists to catch. A
    bare `except PortError: return True` reported it as deleted instead; now the
    denial propagates and the stack is reported as outstanding.
    """
    deleters = aws_deleters(
        {
            "cloudformation.delete_stack": lambda **_k: {},
            "cloudformation.describe_stacks": lambda **_k: (_ for _ in ()).throw(
                ports.PortError("cloudformation.describe_stacks failed: AccessDenied")
            ),
        }
    )
    with pytest.raises(ports.PortError, match="AccessDenied"):
        deleters["cloudformation_stack"](
            "adp-e2e-stack", account=DESTINATION_ACCOUNT, region="us-east-1"
        )


def test_a_destination_that_is_already_gone_is_a_successful_cleanup():
    """Absence is the desired end state, so a retried sweep must not fail."""
    delete, _http = destination_deleter(FakeGateway())
    delete("dest-1")  # must not raise


def test_removing_a_destination_without_a_session_fails_rather_than_skipping():
    """An unauthenticated cleanup cannot prove anything was removed."""
    gateway = FakeGateway()
    gateway.register("dest-1", label=f"{EVAL_ID}-dest", grant=True)
    cfg = config.validate(config_fixture())
    build = live.wire(cfg, {"aws": FakeAws(), "http": FakeHttp(gateway=gateway)})[
        "deleters"
    ]
    delete = build(cfg, None)["bedrock_destination"]  # no run session at all
    with pytest.raises(ports.PortError) as raised:
        delete("dest-1")
    assert "must be removed by its owner" in str(raised.value)
    assert gateway.unlinked == []


def test_a_vault_the_orchestrator_cannot_read_reports_the_resource_outstanding():
    """The deleters run after the instance is gone; an unreadable vault is honest.

    `cleanup.ORDER` terminates the instance first, so the vault read can genuinely
    fail — and the only safe answer is the same as having no session at all: refuse
    to act and report the resource as still present. Degrading to "assume removed"
    would let a live routing rule to an evaluation account be reported as cleaned.
    """
    gateway = FakeGateway()
    gateway.register("dest-1", label=f"{EVAL_ID}-dest", grant=True)
    delete, _http = destination_deleter(gateway, ssm=VaultSsm(fail=True))
    with pytest.raises(ports.PortError) as raised:
        delete("dest-1")
    assert "must be removed by its owner" in str(raised.value)
    assert gateway.unlinked == []


def test_the_session_token_the_deleters_use_never_reaches_the_run_document(tmp_path):
    """The vault read is a side channel, not a write-back into published state.

    The report, the JUnit file and the durable state store are all built from the
    run document, so a token cached back into it would be published — the exact
    failure the on-instance vault exists to prevent. Read once, held in the
    deleters' closure, and absent from everything that leaves the orchestrator.
    """
    gateway = FakeGateway()
    result = run_live_stages(
        tmp_path, doubles=lambda cfg: live_doubles(cfg, gateway=gateway)
    )
    serialized = json.dumps(result.document)
    assert "<token>" not in serialized and "<refresh>" not in serialized
    assert "session.json" in serialized  # the reference is what got published

    # And the orchestrator asked the instance for it exactly once, not per deleter.
    assert result.ports["ssm"].session_reads == 1


def test_a_full_run_removes_the_routing_rule_it_created_and_reports_the_row(tmp_path):
    """End to end, through `runner.main` with only transports doubled.

    The E06 journey's `connect` registers a destination and a rule server-side.
    The run must remove the rule — the part that would otherwise keep routing live
    traffic to an evaluation account — and must report the registry row as NOT
    cleaned, because the gateway has no endpoint that deletes it. A green cleanup
    here would be the harness claiming to have removed something still present.
    """
    gateway = FakeGateway()
    result = run_live_stages(
        tmp_path, doubles=lambda cfg: live_doubles(cfg, gateway=gateway)
    )
    document = result.document
    assert document["matrix"]["E06"]["status"] == cases.PASSED
    # `connect` really did register the row and the rule through the journey.
    assert list(gateway.destinations) == ["dest-1"]
    # The rule is gone: no live traffic can reach the evaluation destination.
    assert gateway.mappings == []
    # The row is not, and the run says so rather than reporting a clean sweep.
    swept = {
        entry["kind"]: entry["status"] for entry in document["cleanup_results"] or []
    }
    assert swept.get("bedrock_destination") == cleanup.FAILED
    assert document["cleanup_ok"] is False
    assert result.code == 1


def test_the_e02_fixture_passwords_never_reach_the_run_document_or_the_payload(
    tmp_path,
):
    """The generated credentials must live only in the secret.

    The run document is written to the durable S3 state store and restored by the
    recovery job, and the SSM command text is visible to anyone who can read the
    command invocation. So the passwords this stage generates must appear in
    NEITHER — only the secret's NAME travels, which is the indirection
    `remote/common.fixture_secret` already implements. This is the same class of
    defect as the session handoff: a credential that leaks into exported state.
    """
    result = run_live_stages(tmp_path)
    aws = result.ports["aws"]

    # The passwords the stage generated, recovered from the create_secret call so
    # the test checks the REAL values rather than a guess at their shape.
    created = [
        kwargs
        for service, operation, kwargs in aws.calls
        if (service, operation) == ("secretsmanager", "create_secret")
    ]
    assert created, "the E02 fixture secret was never created"
    document = json.loads(created[0]["SecretString"])
    passwords = [
        document[key]
        for key in ("admin_password", "admin_new_password", "non_admin_password")
    ]
    assert all(passwords), "a fixture password was empty"
    assert len(set(passwords)) == 3, "the fixture reused one password for two roles"

    # Nowhere in the run document, which is what reaches S3 and the report.
    serialized = json.dumps(result.document)
    for password in passwords:
        assert password not in serialized

    # Nowhere in the SSM command text either.
    for joined in result.ports["ssm"].commands:
        for password in passwords:
            assert password not in joined

    # What DOES travel is the secret name, and the payload carries no password key.
    payload = result.ports["ssm"].payloads["install_auth"]
    assert payload["credential_secret"].endswith("-fixtures")
    assert not [key for key in payload if "password" in key.lower()]


def test_the_e02_identities_are_recorded_before_they_are_created(tmp_path):
    """Record-before-mutate, or an interrupted run leaks an identity nothing finds.

    The manifest is the only route to deletion for a Cognito user — there is no
    tag-and-age sweep behind it, unlike an EC2 instance. So the intent must be
    durable BEFORE the create call, which is the ordering `Manifest.record`
    exists to make possible.
    """
    order = []
    real = cleanup.Manifest.record

    def watched(self, kind, identifier, **kwargs):
        order.append(("record", kind, identifier))
        return real(self, kind, identifier, **kwargs)

    # One shared ordering log across both the manifest and the AWS transport, so
    # the assertion is about their true interleaving rather than two tallies.
    cleanup.Manifest.record = watched

    def doubles(cfg):
        ports_double = live_doubles(cfg)
        aws = ports_double["aws"]
        underlying = aws.call

        def logged(service, operation, **kwargs):
            order.append(("call", operation))
            return underlying(service, operation, **kwargs)

        aws.call = logged
        return ports_double

    try:
        result = run_live_stages(tmp_path, doubles=doubles, extra=["--suite", "full"])
    finally:
        # Restored unconditionally: a leaked patch would silently corrupt every
        # test that runs after this one.
        cleanup.Manifest.record = real

    fixtures = result.document.get("admin_fixtures") or {}
    assert fixtures.get("created_username"), "no challenge identity was provisioned"

    # Every fixture create is preceded by the record naming that same resource.
    for kind, operation in (
        ("cognito_user", "admin_create_user"),
        ("secret", "create_secret"),
    ):
        recorded = next(
            (i for i, item in enumerate(order) if item[:2] == ("record", kind)), None
        )
        created = next(
            (i for i, item in enumerate(order) if item == ("call", operation)), None
        )
        assert recorded is not None, f"{kind} was never recorded"
        assert created is not None, f"{operation} was never called"
        assert recorded < created, f"{kind} was recorded AFTER it was created"


def test_both_e02_identities_and_the_fixture_secret_are_deleted_by_the_sweep(tmp_path):
    """All THREE run-owned resources, not just the one the login used.

    The non-admin identity is the easy one to leak: nothing downstream reads it
    after the denial check, so a missing manifest record would never surface as a
    failure — it would just leave a real Cognito user behind in a shared pool on
    every E02 run. A Cognito user has no tag-and-age sweep behind it, so the
    manifest is its only route to deletion.
    """
    result = run_live_stages(tmp_path, extra=["--suite", "full"])
    fixtures = result.document["admin_fixtures"]
    aws = result.ports["aws"]

    deleted = {
        kwargs.get("Username")
        for service, operation, kwargs in aws.calls
        if (service, operation) == ("cognito-idp", "admin_delete_user")
    }
    assert fixtures["created_username"] in deleted, "the challenge identity leaked"
    assert fixtures["non_admin_username"] in deleted, "the non-admin identity leaked"

    secrets_deleted = {
        kwargs.get("SecretId")
        for service, operation, kwargs in aws.calls
        if (service, operation) == ("secretsmanager", "delete_secret")
    }
    assert any(name.endswith("-fixtures") for name in secrets_deleted), (
        "the fixture secret carrying live credentials was not deleted"
    )


# --------------------------------------------------------------------------
# The ADP account behind the run's own login
#
# A Cognito identity is not an ADP account. E02 must create its own identities —
# the shared fixture is CONFIRMED so it issues no challenge, and it is an admin so
# it cannot be the negative — but every LATER case inherits that identity, and the
# user-scoped routes they exercise resolve a Cognito subject to a `users` row and
# answer 404 `user_not_found` when there is none. That is what made run 10's E13
# report `failed`/exit 5 where it expects `pending`/exit 4: a failure installed two
# stages earlier. Confirmed in the deployed gateway's own logs, and reproduced both
# ways — `pending` for the shared fixture, which has a row, `user_not_found` for a
# run-created identity, which did not.
#
# The product is right to refuse; the harness was wrong. So the run registers the
# account through the product's own onboarding route.
# --------------------------------------------------------------------------


def onboard_gateway(tmp_path, *, listing=None, created=None, status=201):
    """Drive the shipped `_onboard` over a recording double of `common.api`.

    The script, not a copy of it: `shipped_script` imports from the extracted
    bundle, so a module that never packaged fails here rather than on the instance.
    """
    script, common = shipped_script(tmp_path, "install_auth")
    calls = []

    def api(config, path, token, *, method="GET", body=None, expect=(200,)):
        calls.append(
            {
                "path": path,
                "method": method,
                "body": body,
                "token": token,
                "expect": expect,
            }
        )
        if method == "GET":
            answer = (
                listing
                if listing is not None
                else {"users": [{"id": "someone", "team_id": "org-eval-team"}]}
            )
            return 200, answer
        if status not in expect:
            raise common.RemoteError(
                f"{path} returned HTTP {status}, expected {expect}"
            )
        return status, (
            created
            if created is not None
            else {"id": "adp-user-eval", "cognito_sub": "sub-eval"}
        )

    common.api = api
    evidence = {
        "login": {"org_id": "org-eval", "user_id": "sub-eval", "username": "who"},
    }
    config = {
        "gateway_url": "https://gw.example/api",
        "evaluation_id": EVAL_ID,
        "created_username": f"{EVAL_ID}-admin@adp-eval.example",
    }
    return script, common, config, evidence, calls


def test_the_run_registers_an_adp_account_for_the_login_it_created(tmp_path):
    """Through the product's own onboarding route, with the immutable subject.

    A direct database write would prove a shape the product never produces, so the
    fixture must go through the same path an operator uses. `cognito_identity`
    adopts the login that already exists, and the route demands `expected_sub`
    rather than the email precisely because an email is mutable — so the run
    supplies the subject the gateway itself attributed to the session.
    """
    script, _common, config, evidence, calls = onboard_gateway(tmp_path)
    script._onboard(config, evidence, "<token>")

    posted = [call for call in calls if call["method"] == "POST"]
    assert len(posted) == 1, "the account was registered by more or less than one call"
    body = posted[0]["body"]
    assert posted[0]["path"] == "/api/admin/identity/organizations/org-eval/users"
    # The route is org-scoped and platform-admin gated; the bearer token is the
    # session the CLI just earned by completing E02's challenge.
    assert posted[0]["token"] == "<token>"
    assert body["cognito_identity"] == {
        "username": config["created_username"],
        "expected_sub": "sub-eval",
    }
    assert body["email"] == config["created_username"]
    # No mail for an undeliverable reserved domain, matching MessageAction=SUPPRESS
    # on the Cognito create.
    assert body["send_invite"] is False
    assert evidence["onboard"]["user_id"] == "adp-user-eval"
    assert evidence["onboard"]["cognito_sub_bound"] is True


def test_the_registered_account_is_reported_so_the_sweep_removes_it(tmp_path):
    """The orchestrator cannot know this id in advance — the product assigns it.

    So the journey reports it, as `<org>/<id>` because the delete route is
    org-scoped and an id alone would not say which organization to remove it from.
    Without the report the row would outlive the run in a shared environment on
    every E02 pass, with no tag-and-age sweep behind it.
    """
    script, _common, config, evidence, _calls = onboard_gateway(tmp_path)
    script._onboard(config, evidence, "<token>")
    assert evidence["resources"] == [["adp_user", "org-eval/adp-user-eval"]]
    assert "adp_user" in cleanup.ORDER


def test_the_shared_fixture_is_never_re_provisioned(tmp_path):
    """A run that did not create its identity must not touch that identity's account.

    `created_username` is what makes a login this run's, and the shared fixture
    already has its ADP account. Registering one for it would be writing to a
    Terraform-managed fixture other e2e consumers depend on — and would answer 409
    anyway, failing the stage over a precondition that was already satisfied.
    """
    script, _common, config, evidence, calls = onboard_gateway(tmp_path)
    del config["created_username"]
    script._onboard(config, evidence, "<token>")
    assert calls == [], "the shared fixture's account was re-provisioned"
    assert "onboard" not in evidence and "resources" not in evidence


def test_the_team_is_read_from_the_organization_not_constructed(tmp_path):
    """`create_user` refuses a team that does not exist, and the default is a guess.

    The product derives `<org>-team-default` when no team is given, but that row is
    not what every organization actually has — dev's `adp-platform` does not, and
    the constructed name answers 404 "Team does not exist in the requested
    organization" (verified live). So the run reads a real team off the org's own
    listing instead of assuming the naming convention holds.
    """
    script, _common, config, evidence, calls = onboard_gateway(
        tmp_path,
        listing={"users": [{"id": "u1"}, {"id": "u2", "team_id": "acd514aa-real"}]},
    )
    script._onboard(config, evidence, "<token>")
    posted = next(call for call in calls if call["method"] == "POST")
    assert posted["body"]["team_id"] == "acd514aa-real"
    assert "org-eval-team-default" not in json.dumps(posted["body"])


def test_an_organization_with_no_resolvable_team_fails_loudly(tmp_path):
    """Naming the missing precondition beats a 404 from the create three lines later.

    Falling back to the constructed default here would turn "this org has no team
    we can see" into the product's own "Team does not exist" — attributing a
    fixture gap to the gateway, which is exactly the ambiguity this harness exists
    to remove.
    """
    script, common, config, evidence, calls = onboard_gateway(
        tmp_path, listing={"users": []}
    )
    with pytest.raises(common.RemoteError) as raised:
        script._onboard(config, evidence, "<token>")
    assert "no existing team could be resolved" in str(raised.value)
    assert not [call for call in calls if call["method"] == "POST"]


def test_an_account_bound_to_a_different_login_is_a_failure(tmp_path):
    """The bind is the whole point; a row without it resolves to nobody.

    A 201 that came back bound to another subject would leave every later case
    authenticating as an identity the gateway still cannot resolve — the original
    defect, now silent. So the returned subject is checked against the one that
    asked.
    """
    script, common, config, evidence, _calls = onboard_gateway(
        tmp_path, created={"id": "adp-user-eval", "cognito_sub": "sub-somebody-else"}
    )
    with pytest.raises(common.RemoteError) as raised:
        script._onboard(config, evidence, "<token>")
    assert "not bound to the login that created it" in str(raised.value)


def test_the_account_is_registered_before_setup_and_every_later_journey(tmp_path):
    """Ordering is the fix. Registering it late would leave the gap it closes.

    E03's `adp admin setup` and every journey after it authenticate as this
    identity, so the account has to exist before the first of them runs — which is
    why the call sits between the login and `_setup` in `execute`.
    """
    source = (extracted_bundle(tmp_path) / "remote" / "install_auth.py").read_text()
    body = source[source.index("def execute(") :]
    onboard, setup = body.find("_onboard("), body.find("_setup(")
    assert onboard != -1, "execute() never registers the run's ADP account at all"
    assert setup != -1, "execute() no longer runs setup; this test needs rewriting"
    assert onboard < setup, (
        "the ADP account is registered after setup, so setup still runs as an identity the gateway cannot resolve"
    )


def test_the_sweep_deletes_the_adp_account_and_leaves_a_real_operator_alone(tmp_path):
    """Ownership-checked, like the connection deleter and for the same reason.

    The identifier arrives over SSM from a journey. A deleter that trusted it could
    be pointed at a real operator's account in a shared organization — so the row's
    email must carry this run's evaluation ID, a property of the address
    `_admin_fixtures` derives that cannot hold for an account this evaluation did
    not create.
    """
    gateway = FakeGateway()
    transports = {}

    def doubles(cfg):
        wired = live_doubles(cfg, gateway=gateway)
        transports.update(wired)

        def absent(**kwargs):
            raise ports.PortError("s3.head_object failed: 404")

        def terminate(**kwargs):
            # Terminating the instance destroys the vault with it. `cleanup.ORDER`
            # does this FIRST, so any deleter that waits until it needs the token
            # to fetch it will find nothing to fetch it from — the live failure
            # this models.
            wired["ssm"].instance_terminated = True
            return {}

        wired["aws"].replies.update(
            {
                "ec2.terminate_instances": terminate,
                "ec2.describe_instances": {
                    "Reservations": [{"Instances": [{"State": {"Name": "terminated"}}]}]
                },
                "s3.delete_object": {},
                "s3.head_object": absent,
                "cognito-idp.admin_delete_user": {},
                "secretsmanager.delete_secret": {},
            }
        )
        return wired

    result = run_live_stages(
        tmp_path,
        doubles=doubles,
        extra=("--suite", "install", "--suite", "admin", "--suite", "parity"),
    )
    swept = {
        entry["kind"]: entry["status"] for entry in result.document["cleanup_results"]
    }
    assert swept.get("adp_user") == cleanup.DELETED
    assert gateway.deleted_users == ["org-eval/adp-user-eval"]
    # The operator's account, in the same organization, is untouched.
    assert list(gateway.users["org-eval"]) == ["operator-1"]
    assert result.document["cleanup_ok"] is True
    # The token was read while the instance was still up, exactly once.
    # `cleanup.ORDER` terminates it first, so this can only hold if the read
    # happens when the deleters are BUILT rather than when one first needs it.
    assert transports["ssm"].session_reads == 1


def test_the_session_token_is_read_before_the_sweep_terminates_the_instance(tmp_path):
    """The vault dies with the instance, and the instance is deleted FIRST.

    `cleanup.ORDER` begins with `ec2_instance` because it holds the ENI several
    other kinds depend on. The token the API deleters authenticate with lives in a
    0600 vault ON that instance, so a token fetched at the moment a deleter needs
    it is always fetched after the only machine that could serve it is gone.

    This is not hypothetical: it is what made a live run report
    `adp_user:<org>/<id>` outstanding with a PortError while all seven other kinds
    reported deleted. It hid for as long as it did because the two API deleters
    that existed before — `adp_connection` and `bedrock_destination` — cover
    resources a SUCCESSFUL journey removes through the product's own CLI, so they
    only ever ran for an interrupted worker. `adp_user` is the first kind the sweep
    must always delete itself.

    Asserted at the seam rather than through a full run so the guarantee is stated
    once, directly: building the deleters reads the vault; nothing else has to have
    happened yet.
    """
    cfg = config.validate(config_fixture())
    ssm = VaultSsm()
    build = live.wire(cfg, {"aws": FakeAws(), "http": FakeHttp(), "ssm": ssm})[
        "deleters"
    ]
    ctx = {
        "document": {
            "instance_id": "i-0eval",
            "session": {
                "session_ref": "/home/ec2-user/adp-eval/session.json",
                "prefix": EVAL_ID,
            },
        }
    }
    deleters = build(cfg, ctx)
    assert len(ssm.reads) == 1, (
        "building the deleters read the session vault "
        f"{len(ssm.reads)} times; exactly one eager read is required — zero means "
        "the token is fetched later, after ec2_instance has already been "
        "terminated, and more than one means it is not cached"
    )
    # And the captured token survives the instance: terminating it now makes the
    # transport unreadable, yet the deleter still has what it needs. Reading
    # through the same accessor twice must not go back to a machine that is gone.
    ssm.terminated = True
    token = live._vault_token(ssm, "i-0eval", ctx["document"]["session"])
    assert token == "", (
        "the double must model a terminated instance as unreadable, or this test cannot distinguish an eager read from a lucky one"
    )
    # The ADP-account deleter is the kind that always needs it. It must not be
    # refusing to act for want of a token at this point.
    with pytest.raises(ports.PortError) as raised:
        deleters["adp_user"]("not-an-org-slash-id-shape")
    assert "No authenticated session" not in str(raised.value), (
        "the deleter had no token after the instance was terminated, which is the live failure this guards"
    )


def test_the_sweep_refuses_an_account_this_run_did_not_create(tmp_path):
    """A misrecorded id must be reported as outstanding, never deleted.

    Deleting a row cascades to its Cognito login, so a wrong identifier here is an
    operator locked out of the product — the one outcome strictly worse than a
    reported leak.
    """
    gateway = FakeGateway()
    delete, _http = destination_deleter(gateway, kind="adp_user")
    with pytest.raises(ports.PortError) as raised:
        delete("org-eval/operator-1")
    assert "not named for this run" in str(raised.value)
    assert gateway.deleted_users == []
    assert "operator-1" in gateway.users["org-eval"]


def test_an_adp_account_already_gone_is_a_successful_deletion(tmp_path):
    """The end state a deleter exists to reach, met on arrival.

    `sweep()` deletes and THEN marks, and that mark is best-effort — so a run
    cancelled mid-sweep leaves the durable manifest saying `pending` for a row that
    is already gone. The recovery job deletes it again, and raising there would deny
    acceptance over a resource that is not there.
    """
    # Never enrolled, which is exactly the state a second delete meets.
    gateway = FakeGateway()
    delete, _http = destination_deleter(gateway, kind="adp_user")
    delete("org-eval/adp-user-eval")  # must not raise
    assert gateway.deleted_users == []


def test_the_adp_account_is_deleted_after_the_deleters_that_authenticate_as_it(
    tmp_path,
):
    """`cleanup.ORDER` is load-bearing here, in both directions.

    The connection and destination deleters call user-scoped endpoints that resolve
    a Cognito subject to this very row: remove it first and they answer 404
    `user_not_found`, so the run would report a leak for resources it could no
    longer even see. And it comes after `cognito_user` because the product's delete
    cascades to the login — by the time this runs the login is already gone, and the
    cascade is a best-effort no-op rather than a failure.
    """
    order = cleanup.ORDER
    assert order.index("cognito_user") < order.index("adp_user")
    for kind in ("adp_connection", "bedrock_destination"):
        assert order.index(kind) < order.index("adp_user"), (
            f"{kind} is deleted after the account its endpoint resolves through"
        )


def test_the_fixture_secret_is_readable_only_by_the_evaluation_instance_role(tmp_path):
    """The grant must be a resource policy naming one role, and nothing wider.

    The instance role's single GetSecretValue grant is pinned to the one SHARED
    fixture ARN by design, so a run-owned secret is unreadable by identity policy.
    A resource policy on the new secret closes that without touching the shared
    role — verified live with `simulate-principal-policy`, which answered
    `allowed` via SourcePolicyType "Resource Policy" for this role and
    `implicitDeny` for any other secret.

    The alternative — widening the role's identity policy to `adp-e2e-*` — would
    have loosened a shared boundary for every future run, so it is not used.
    """
    result = run_live_stages(tmp_path)
    aws = result.ports["aws"]
    applied = [
        kwargs
        for service, operation, kwargs in aws.calls
        if (service, operation) == ("secretsmanager", "put_resource_policy")
    ]
    assert applied, "the fixture secret was created with no resource policy"
    policy = json.loads(applied[0]["ResourcePolicy"])
    assert applied[0]["BlockPublicPolicy"] is True

    statements = policy["Statement"]
    assert len(statements) == 1, "the grant must be exactly one statement"
    statement = statements[0]
    assert statement["Effect"] == "Allow"
    # One principal, one action. A wildcard in either is the failure this checks.
    assert statement["Principal"]["AWS"].endswith(":role/adp-cli-uplift-eval-instance")
    assert statement["Action"] == "secretsmanager:GetSecretValue"
    assert "*" not in statement["Principal"]["AWS"]
    assert statement["Resource"].startswith("arn:aws:secretsmanager:")
    assert statement["Resource"] != "*"


def test_a_suite_without_e02_provisions_no_identities_at_all(tmp_path):
    """Selection gates the WORK here, because the work is a live mutation.

    `selected()` normally filters only what is recorded — a stage still installs
    and logs in for E01. But creating Cognito identities is a real mutation with a
    real cleanup obligation, so a suite that cannot record E02 must not create
    them. The shared configured fixture stays in use for those runs.
    """
    result = run_live_stages(tmp_path, extra=["--suite", "install"])
    aws = result.ports["aws"]
    operations = {f"{service}.{operation}" for service, operation, _kwargs in aws.calls}
    assert "cognito-idp.admin_create_user" not in operations
    assert "secretsmanager.create_secret" not in operations
    assert "admin_fixtures" not in result.document

    # And the worker still gets the SHARED configured fixture, not an empty name:
    # an install-only run must still be able to log in.
    payload = result.ports["ssm"].payloads["install_auth"]
    assert payload["credential_secret"] == config_fixture()["credential_secret_name"]
    assert payload["admin_challenges_required"] is False


def test_destination_identity_comes_from_an_assumed_role_not_the_runner(tmp_path):
    """Otherwise cross-account access is 'proven' by the runner's own session."""
    cfg = config.validate(
        config_fixture(destination_role_arn="arn:aws:iam::605440105851:role/prov")
    )
    aws = FakeAws(
        {
            "sts.get_caller_identity": {"Account": "879318057152", "Arn": "arn:x"},
            "sts.assume_role": {
                "Credentials": {
                    "AccessKeyId": "A",
                    "SecretAccessKey": "B",
                    "SessionToken": "C",
                }
            },
        }
    )
    wired = live.wire(cfg, {"aws": aws})
    wired["identity"]("destination")
    assert aws.assumed and aws.assumed[0][0].endswith(":role/prov")


def test_destination_identity_refuses_to_substitute_the_runners_session():
    """With no role configured this must raise, not silently self-report."""
    cfg = config.validate(
        config_fixture(destination_role_arn="", provisioner_role_arn="")
    )
    wired = live.wire(cfg, {"aws": FakeAws()})
    with pytest.raises(ports.PortError):
        wired["identity"]("destination")


def test_every_manifest_kind_has_a_live_deleter_or_an_explicit_refusal():
    """An unregistered kind makes cleanup.sweep fail; none may be forgotten."""
    cfg = config.validate(config_fixture())
    deleters = live.wire(cfg, {"aws": FakeAws()})["deleters"](cfg)
    for kind in cleanup.ORDER:
        assert kind in deleters, f"no deleter registered for {kind}"


def test_live_wiring_never_purges_a_shared_queue():
    """Draining a shared queue would destroy other tenants' work."""
    cfg = config.validate(config_fixture())
    deleters = live.wire(cfg, {"aws": FakeAws()})["deleters"](cfg)
    assert "sqs_queue" not in deleters
    assert not any("purge" in name for name in deleters)


# --------------------------------------------------------------------------
# Stage outcomes must veto acceptance (finding 2)
# --------------------------------------------------------------------------
#
# The reported false green: on resume, passed cases are preserved by design, so
# an attempt whose preflight REJECTED the target still saw fifteen passes in the
# matrix and published `passed` / `full_acceptance: true`. The verdict is
# recomputed from persisted state at publish time, so threading the exception
# through main() would not have been enough -- the veto has to be derivable from
# the stage map that `report.build` and `report.summary` also read.


def test_stage_problems_names_every_state_that_is_not_complete():
    problems = cases.stage_problems(
        {
            "preflight": "complete",
            "ec2": "failed",
            "install_auth": "timed_out",
            "providers": "pending",
            "journeys": "running",
            "evidence": "skipped",
            "cleanup": "missing",
        }
    )
    assert problems == [
        "cleanup: missing",
        "ec2: failed",
        "evidence: skipped",
        "install_auth: timed_out",
        "journeys: running",
        "providers: pending",
    ]


def test_stage_problems_treats_an_unknown_future_state_as_a_problem():
    """Allow-listing 'complete' means a new state defaults to vetoing.

    The inverse -- enumerating bad states -- silently accepts whatever state is
    added next, which is exactly the class of bug finding 2 reported.
    """
    assert cases.stage_problems({"ec2": "quarantined"}) == ["ec2: quarantined"]


def test_all_passed_matrix_with_a_failed_stage_is_not_acceptance():
    """The finding in one assertion."""
    status, reasons = cases.accept(
        all_passed(),
        FULL,
        stages={**{n: "complete" for n in runner.STAGES}, "preflight": "failed"},
    )
    assert status == cases.FAILED
    assert any("preflight: failed" in reason for reason in reasons)


def test_all_passed_matrix_with_every_stage_complete_is_acceptance():
    """The veto must not be so broad that a genuinely good run cannot pass."""
    status, reasons = cases.accept(
        all_passed(), FULL, stages={name: "complete" for name in runner.STAGES}
    )
    assert (status, reasons) == (cases.PASSED, [])


def test_report_build_derives_the_veto_from_persisted_stages():
    """report.json is the authority, so the veto must apply there too."""
    document = report.build(
        matrix=all_passed(),
        suites=FULL,
        config=config.validate(config_fixture()),
        evaluation_id="eval-1",
        attempt_id="eval-1-a2",
        cleanup_ok=True,
        timing={},
        correlation={},
        stages={**{n: "complete" for n in runner.STAGES}, "preflight": "failed"},
    )
    assert document["status"] == cases.FAILED
    assert document["full_acceptance"] is False


def test_actions_summary_cannot_contradict_the_report():
    """A summary saying `passed` above a report saying `failed` is its own incident."""
    spoiled = {**{n: "complete" for n in runner.STAGES}, "journeys": "timed_out"}
    text = report.summary(all_passed(), FULL, "eval-1", cleanup_ok=True, stages=spoiled)
    assert "`failed`" in text
    assert "journeys: timed_out" in text


def test_resumed_attempt_with_a_rejecting_preflight_cannot_publish_acceptance(
    tmp_path, capsys
):
    """End to end, the exact reported scenario.

    Attempt 1 passes every case. Attempt 2's preflight rejects the target -- the
    deployment changed under us. Because passed cases are preserved across
    attempts, the matrix still reads fifteen passes; only the stage map records
    that this attempt proved nothing.
    """
    run_cli(tmp_path, {"journeys": pass_everything, "cleanup": lambda ctx: True})
    capsys.readouterr()

    def reject(_ctx):
        raise RuntimeError("deployed revision is not the expected one")

    code = run_cli(
        tmp_path,
        {"preflight": reject, "cleanup": lambda ctx: True},
        mode="resume",
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == cases.FAILED
    assert payload["full_acceptance"] is False
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    # The matrix genuinely still reads all-passed: that is why the stage map,
    # not the matrix, has to carry the veto.
    assert {e["status"] for e in document["matrix"].values()} == {cases.PASSED}
    assert any("preflight: failed" in reason for reason in document["reasons"])
    # And the artifact on disk agrees with the exit code.
    published = json.loads((tmp_path / STATE / "out" / "report.json").read_text())
    assert published["status"] == cases.FAILED
    assert published["full_acceptance"] is False


def test_a_timed_out_stage_vetoes_acceptance_on_resume(tmp_path, capsys):
    """Same class as above, reached by the deadline rather than an exception."""
    run_cli(tmp_path, {"journeys": pass_everything, "cleanup": lambda ctx: True})
    capsys.readouterr()
    clock = iter([NOW, NOW, NOW + 10**6] + [NOW + 10**6] * 8)
    code = run_cli(
        tmp_path,
        {"cleanup": lambda ctx: True},
        mode="resume",
        clock=lambda: next(clock),
    )
    assert code == 1
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert "timed_out" in document["stages"].values()
    assert document["status"] == cases.FAILED


def test_publish_carries_stage_state_into_the_verdict(tmp_path):
    """`publish()` must not regrade without the stages `run()` graded with."""
    document = {
        "evaluation_id": EVAL_ID,
        "attempt_id": EVAL_ID + "-a1",
        "matrix": all_passed(),
        "suites": list(FULL),
        "stages": {**{n: "complete" for n in runner.STAGES}, "evidence": "failed"},
        "cleanup_ok": True,
        "attempts": [],
        "timing": {},
        "correlation": {},
        "preflight": {},
        "transcript": [],
        "expected_revision": GOOD_REVISION,
    }
    payload, _paths, text = runner.publish(
        document, config.validate(config_fixture()), str(tmp_path / "out"), now=NOW
    )
    assert payload["status"] == cases.FAILED
    assert payload["full_acceptance"] is False
    assert "evidence: failed" in text


# --------------------------------------------------------------------------
# Recovery after an early cancellation (finding 4)
# --------------------------------------------------------------------------
#
# The age-based sweep alone could not recover a run cancelled shortly after
# launch: the instance is validly tagged but nowhere near its TTL, so it billed
# for hours and only if some later run happened to sweep. Once a run has ended,
# however it ended, its own tag is sufficient authority to terminate.


def instance(identifier, *, prefix, age_minutes, state="running", now=NOW):
    return {
        "InstanceId": identifier,
        "State": {"Name": state},
        "LaunchTime": now - age_minutes * 60,
        "Tags": [{"Key": cleanup.OWNER_TAG, "Value": prefix}] if prefix else [],
    }


def test_owned_by_run_recovers_a_fresh_instance_from_a_cancelled_run():
    """The exact gap: cancelled after two minutes, TTL is four hours away."""
    found = cleanup.owned_by_run(
        [instance("i-1", prefix="run-a", age_minutes=2)], "run-a"
    )
    assert [item["id"] for item in found] == ["i-1"]
    assert found[0]["reason"] == "run_ended"


def test_owned_by_run_never_touches_another_runs_instance():
    """No age check here, so prefix scoping is the entire safety argument."""
    instances = [
        instance("i-mine", prefix="run-a", age_minutes=1),
        instance("i-theirs", prefix="run-b", age_minutes=1),
        instance("i-untagged", prefix=None, age_minutes=1),
    ]
    assert [item["id"] for item in cleanup.owned_by_run(instances, "run-a")] == [
        "i-mine"
    ]


def test_owned_by_run_refuses_an_empty_prefix():
    """Matching everything without an age check would be catastrophic."""
    with pytest.raises(ValueError):
        cleanup.owned_by_run([instance("i-1", prefix="run-a", age_minutes=1)], "")
    with pytest.raises(ValueError):
        cleanup.owned_by_run([instance("i-1", prefix="run-a", age_minutes=1)], None)


def test_owned_by_run_ignores_already_terminating_instances():
    """Idempotent: a repeated sweep must not report repeat kills."""
    instances = [
        instance("i-1", prefix="run-a", age_minutes=1, state="shutting-down"),
        instance("i-2", prefix="run-a", age_minutes=1, state="terminated"),
    ]
    assert cleanup.owned_by_run(instances, "run-a") == []


def test_recoverable_unions_this_run_with_anything_expired():
    """Two authorities covering two different failures."""
    instances = [
        instance("i-fresh-mine", prefix="run-a", age_minutes=2),
        instance("i-old-other", prefix="run-b", age_minutes=500),
        instance("i-fresh-other", prefix="run-b", age_minutes=2),
    ]
    targets = cleanup.recoverable_instances(
        instances, now=NOW, ttl_minutes=240, prefix="run-a"
    )
    identifiers = {item["id"]: item["reason"] for item in targets}
    assert identifiers == {
        "i-fresh-mine": "run_ended",
        "i-old-other": "ttl_expired",
    }
    # Another evaluation's LIVE instance is untouched by both authorities.
    assert "i-fresh-other" not in identifiers


def test_recoverable_without_a_prefix_falls_back_to_age_only():
    """When evaluate died before reporting its ID, age is all we have."""
    instances = [
        instance("i-fresh", prefix="run-a", age_minutes=2),
        instance("i-old", prefix="run-a", age_minutes=500),
    ]
    targets = cleanup.recoverable_instances(instances, now=NOW, ttl_minutes=240)
    assert [item["id"] for item in targets] == ["i-old"]


def test_recoverable_reports_an_instance_once_when_both_rules_match():
    instances = [instance("i-1", prefix="run-a", age_minutes=500)]
    targets = cleanup.recoverable_instances(
        instances, now=NOW, ttl_minutes=240, prefix="run-a"
    )
    assert [item["id"] for item in targets] == ["i-1"]


# --------------------------------------------------------------------------
# Durable state by evaluation ID (finding 5)
# --------------------------------------------------------------------------
#
# State lived only in the runner's /tmp, so it died with the runner. Resume,
# status and cleanup across Actions runs were therefore impossible, and a
# cancelled run's IAM roles, stacks, secrets and Cognito users had no record
# anywhere -- none of which an instance sweep can find.


class FakeS3:
    """In-memory S3 that records the arguments the store used.

    The encryption and ACL assertions below are the reason this is not a dict:
    what matters is not only that state round-trips but that it was written
    encrypted and owner-only.
    """

    def __init__(self):
        self.objects = {}
        self.puts = []

    def call(self, service, operation, **kwargs):
        assert service == "s3"
        if operation == "put_object":
            self.puts.append(kwargs)
            self.objects[kwargs["Key"]] = kwargs["Body"]
            return {}
        if operation == "get_object":
            if kwargs["Key"] not in self.objects:
                raise ports.PortError("s3.get_object failed: NoSuchKey")
            return {"Body": self.objects[kwargs["Key"]]}
        raise ports.PortError(f"s3.{operation} was not stubbed")


def store_fixture(clock=None):
    aws = FakeS3()
    return aws, statestore.S3StateStore(
        aws, "eval-state-bucket", kms_key_id="key-1", clock=clock or (lambda: NOW)
    )


def test_durable_state_is_written_encrypted_and_owner_only():
    """This state holds live passwords and tokens; it is not an artifact."""
    aws, store = store_fixture()
    store.save(EVAL_ID, {"evaluation_id": EVAL_ID})
    put = aws.puts[0]
    assert put["ServerSideEncryption"] == statestore.ENCRYPTION
    assert put["ACL"] == "bucket-owner-full-control"
    assert put["SSEKMSKeyId"] == "key-1"


def test_durable_state_is_addressed_by_evaluation_id():
    """One evaluation's state can never overwrite another's."""
    aws, store = store_fixture()
    store.save(EVAL_ID, {"evaluation_id": EVAL_ID})
    other = "adp-e2e-20260915-130000-def456"
    store.save(other, {"evaluation_id": other})
    assert statestore.state_key(EVAL_ID) in aws.objects
    assert statestore.state_key(other) in aws.objects
    assert EVAL_ID in statestore.state_key(EVAL_ID)


def test_durable_state_round_trips_with_its_manifest():
    """The manifest is the half that names non-EC2 resources to delete."""
    _aws, store = store_fixture()
    store.save(EVAL_ID, {"evaluation_id": EVAL_ID}, {"entries": [{"kind": "iam_role"}]})
    document, manifest = store.load(EVAL_ID)
    assert document["evaluation_id"] == EVAL_ID
    assert manifest["entries"][0]["kind"] == "iam_role"


def test_loading_an_unknown_evaluation_returns_nothing_rather_than_raising():
    _aws, store = store_fixture()
    assert store.load(EVAL_ID) == (None, None)


def test_restored_state_must_declare_the_evaluation_id_it_was_stored_under():
    """Otherwise a mismatched object could be resumed as this evaluation."""
    with pytest.raises(statestore.StateStoreError):
        statestore.check_restored(
            {"evaluation_id": "adp-e2e-20260915-130000-def456"},
            EVAL_ID,
            {},
            fingerprint="f",
            harness_commit=config.HARNESS_COMMIT,
        )


def test_restored_state_must_match_the_target_and_revision():
    """Same false-green class as the stage veto: revision A evidence, revision B claim."""
    with pytest.raises(statestore.StateStoreError) as excinfo:
        statestore.check_restored(
            {"evaluation_id": EVAL_ID, "config_fingerprint": "other"},
            EVAL_ID,
            {},
            fingerprint="ours",
            harness_commit=config.HARNESS_COMMIT,
        )
    assert "different" in str(excinfo.value)


def test_restored_state_must_match_the_pinned_harness():
    with pytest.raises(statestore.StateStoreError):
        statestore.check_restored(
            {
                "evaluation_id": EVAL_ID,
                "config_fingerprint": "ours",
                "harness_commit": "0" * 40,
            },
            EVAL_ID,
            {},
            fingerprint="ours",
            harness_commit=config.HARNESS_COMMIT,
        )


def test_absent_durable_state_is_an_explicit_error():
    with pytest.raises(statestore.StateStoreError):
        statestore.check_restored(
            None, EVAL_ID, {}, fingerprint="f", harness_commit=config.HARNESS_COMMIT
        )


def test_a_live_lease_prevents_a_second_runner_operating_on_one_evaluation():
    """Two jobs restoring one evaluation would interleave deletions."""
    _aws, store = store_fixture()
    store.claim(EVAL_ID, "run-1")
    with pytest.raises(statestore.StateStoreError) as excinfo:
        store.claim(EVAL_ID, "run-2")
    assert "leased" in str(excinfo.value)


def test_the_same_runner_may_reclaim_its_own_lease():
    """A retried step must not deadlock against itself."""
    _aws, store = store_fixture()
    store.claim(EVAL_ID, "run-1")
    assert store.claim(EVAL_ID, "run-1") is True


def test_an_expired_lease_can_be_taken_over():
    """A lost runner must not strand its evaluation forever."""
    times = iter([NOW, NOW + statestore.LEASE_SECONDS + 60] * 4)
    aws = FakeS3()
    store = statestore.S3StateStore(aws, "b", clock=lambda: next(times))
    store.claim(EVAL_ID, "run-1")
    assert store.claim(EVAL_ID, "run-2") is True


def test_released_lease_frees_the_evaluation():
    _aws, store = store_fixture()
    store.claim(EVAL_ID, "run-1")
    store.release(EVAL_ID, "run-1")
    assert store.claim(EVAL_ID, "run-2") is True


def test_no_store_configured_means_no_durable_state_rather_than_a_fake_one():
    """Silently pretending to persist is worse than saying there is nowhere to."""
    assert statestore.default_store(config.validate(config_fixture())) is None


def test_config_rejects_an_s3_url_as_the_state_bucket():
    with pytest.raises(config.ConfigError):
        config.validate(config_fixture(state_bucket="s3://eval-state"))


def test_a_fresh_runner_can_clean_up_a_prior_run_by_evaluation_id(tmp_path, capsys):
    """The end-to-end recovery finding 4 and 5 exist for.

    Run 1 creates resources and is 'cancelled' -- its local state directory is
    then deleted, exactly as losing a runner does. A fresh invocation restores by
    ID and sweeps the non-EC2 resources an instance sweep cannot see.
    """
    aws = FakeS3()
    store = statestore.S3StateStore(aws, "b", clock=lambda: NOW)

    def leak(ctx):
        ctx["manifest"].record("iam_role", "eval-role", account=DESTINATION_ACCOUNT)
        ctx["manifest"].record("secret", "adp/eval/tmp")

    first_state = tmp_path / "run1"
    complete = {name: (lambda _ctx: None) for name in runner.STAGES}
    complete["providers"] = leak
    complete["cleanup"] = lambda _ctx: True
    runner.main(
        [
            "--mode",
            "start",
            "--config",
            write_config(tmp_path),
            "--state-dir",
            str(first_state),
        ],
        stages=complete,
        clock=lambda: NOW,
        store=store,
    )
    published = json.loads(capsys.readouterr().out)
    evaluation_id = json.loads((first_state / runner.STATE_FILE).read_text())[
        "evaluation_id"
    ]
    assert published  # the run reported; resources were recorded

    # Lose the runner: the local state directory is gone entirely.
    import shutil

    shutil.rmtree(first_state)

    swept = []

    def sweep(ctx):
        return cleanup.sweep(
            ctx["manifest"],
            {
                kind: (
                    lambda identifier, k=kind, **location: swept.append(
                        (k, identifier, location.get("account"))
                    )
                )
                for kind in cleanup.ORDER
            },
        )[0]

    code = runner.main(
        [
            "--mode",
            "cleanup",
            "--config",
            write_config(tmp_path),
            "--state-dir",
            str(tmp_path / "fresh"),
            "--evaluation-id",
            evaluation_id,
            "--restore",
        ],
        stages={**complete, "cleanup": sweep},
        clock=lambda: NOW,
        store=store,
    )
    assert code == 0
    # The resources no instance sweep could have found were deleted by ID — and
    # the cross-account role kept its account across the durable round-trip, so
    # the fresh runner deletes it with the right account's credentials (R7).
    assert ("iam_role", "eval-role", DESTINATION_ACCOUNT) in swept
    assert ("secret", "adp/eval/tmp", None) in swept


def test_restore_refuses_an_evaluation_id_that_is_not_ours(tmp_path):
    """A restored document is untrusted until proven to be for this target."""
    aws = FakeS3()
    store = statestore.S3StateStore(aws, "b", clock=lambda: NOW)
    store.save(
        EVAL_ID,
        {
            "evaluation_id": EVAL_ID,
            "config_fingerprint": "someone-elses",
            "harness_commit": config.HARNESS_COMMIT,
        },
    )
    with pytest.raises(statestore.StateStoreError):
        runner.main(
            [
                "--mode",
                "cleanup",
                "--config",
                write_config(tmp_path),
                "--state-dir",
                str(tmp_path / STATE),
                "--evaluation-id",
                EVAL_ID,
                "--restore",
            ],
            stages={},
            clock=lambda: NOW,
            store=store,
        )


def test_restore_requires_an_evaluation_id(tmp_path):
    with pytest.raises(runner.RunnerError):
        runner.main(
            [
                "--mode",
                "cleanup",
                "--config",
                write_config(tmp_path),
                "--state-dir",
                str(tmp_path / STATE),
                "--restore",
            ],
            stages={},
            clock=lambda: NOW,
            store=statestore.S3StateStore(FakeS3(), "b", clock=lambda: NOW),
        )


def test_restore_without_a_store_is_refused_rather_than_silently_skipped(tmp_path):
    """'--restore did nothing' must never look like 'nothing needed restoring'."""
    with pytest.raises(runner.RunnerError) as excinfo:
        runner.main(
            [
                "--mode",
                "cleanup",
                "--config",
                write_config(tmp_path),
                "--state-dir",
                str(tmp_path / STATE),
                "--evaluation-id",
                EVAL_ID,
                "--restore",
            ],
            stages={},
            clock=lambda: NOW,
            store=None,
        )
    assert "state_bucket" in str(excinfo.value)


def test_state_is_persisted_before_the_stages_run(tmp_path):
    """A run cancelled mid-journey must already be recoverable by ID."""
    aws = FakeS3()
    store = statestore.S3StateStore(aws, "b", clock=lambda: NOW)
    persisted_before_journeys = {}

    def check(_ctx):
        persisted_before_journeys.update(aws.objects)

    complete = {name: (lambda _ctx: None) for name in runner.STAGES}
    complete["preflight"] = check
    complete["cleanup"] = lambda _ctx: True
    runner.main(
        [
            "--mode",
            "start",
            "--config",
            write_config(tmp_path),
            "--state-dir",
            str(tmp_path / STATE),
        ],
        stages=complete,
        clock=lambda: NOW,
        store=store,
    )
    assert any("state.json" in key for key in persisted_before_journeys)


# --------------------------------------------------------------------------
# R6: every intent durable before its mutation, and recovery that does not
# depend on the outer `finally`
#
# The run used to push the manifest exactly twice: once before the stages, when
# it was empty, and once in an outer `finally`. Anything a journey recorded in
# between existed only in the runner's /tmp. A cancellation or a lost runner
# therefore took the record of the IAM roles, stacks, secrets and Cognito users
# it had just created with it — and those kinds have no tag-and-age sweep behind
# them, so the manifest IS their only route to deletion.
# --------------------------------------------------------------------------


class FailingS3(FakeS3):
    """S3 that refuses to write objects whose key matches `refuse`."""

    def __init__(self, refuse):
        super().__init__()
        self.refuse = refuse
        self.refused = []

    def call(self, service, operation, **kwargs):
        if operation == "put_object" and self.refuse in kwargs.get("Key", ""):
            self.refused.append(kwargs["Key"])
            raise ports.PortError("s3.put_object failed: AccessDenied")
        return super().call(service, operation, **kwargs)


def test_the_instance_is_durably_recorded_before_it_is_launched(tmp_path):
    """The intent must be in S3 before the create call, not after the run.

    Asserted at the moment of the mutation, through the production entry point:
    when `ec2.run_instances` is reached, the durable manifest must already name
    the pending instance. That is the property a cancelled run depends on, and
    the only way to hold it is to push on each record.
    """
    aws_s3 = FakeS3()
    store = statestore.S3StateStore(aws_s3, "b", kms_key_id="key-1", clock=lambda: NOW)
    durable_at_launch = {}

    def doubles(cfg):
        ports_double = live_doubles(cfg)

        def run_instances(**_kwargs):
            durable_at_launch.update(aws_s3.objects)
            return {"Instances": [{"InstanceId": "i-0abc"}]}

        ports_double["aws"].replies["ec2.run_instances"] = run_instances
        return ports_double

    run_live_stages(tmp_path, doubles=doubles, store=store)

    manifests = [
        json.loads(body)
        for key, body in durable_at_launch.items()
        if key.endswith("manifest.json")
    ]
    assert manifests, "no durable manifest existed when the instance was launched"
    recorded = {
        (entry["kind"], entry["id"]) for entry in manifests[0].get("resources") or []
    }
    assert any(
        kind == "ec2_instance" and identifier.startswith("pending:")
        for kind, identifier in recorded
    ), recorded


def test_an_intent_that_cannot_be_recorded_durably_creates_nothing(tmp_path):
    """A store outage must stop new mutation, not be absorbed as a warning.

    Creating a resource we could not have recovered is worse than not creating
    it: nothing would ever find it to delete. So the durable push is a
    PRECONDITION of the create call, and its failure fails the stage.
    """
    aws_s3 = FailingS3("manifest.json")
    store = statestore.S3StateStore(aws_s3, "b", clock=lambda: NOW)

    result = run_live_stages(tmp_path, store=store)
    assert result.code == 1
    document = result.document
    assert document["stages"]["ec2"] == "failed"
    # The launch never happened, so there is nothing to leak.
    assert not [
        call for call in result.ports["aws"].calls if call[1] == "run_instances"
    ]
    assert document.get("instance_id") is None
    errors = {entry["stage"]: entry for entry in document.get("errors") or []}
    assert errors["ec2"]["type"] == "RunnerError"
    # The reason names the consequence, and carries no S3 message: a store error
    # can quote a bucket policy or a role ARN.
    assert "durably" in (errors["ec2"]["message"] or "")
    assert "AccessDenied" not in json.dumps(document)
    assert aws_s3.refused, "the manifest push was never attempted"


def test_a_completed_deletion_that_cannot_be_recorded_does_not_abort_the_sweep():
    """The opposite direction: `mark` must never raise.

    `mark` fires during teardown. Raising there because the store is unreachable
    would abandon every resource still to be deleted — the failure the durable
    manifest exists to prevent, caused by the durability itself.
    """

    class Broken:
        def save(self, *_args, **_kwargs):
            raise ports.PortError("s3.put_object failed: AccessDenied")

    publish = runner.manifest_publisher(Broken(), {"evaluation_id": EVAL_ID})
    assert publish({"resources": []}, critical=False) is True  # reported, not raised
    with pytest.raises(runner.RunnerError) as raised:
        publish({"resources": []}, critical=True)
    assert "AccessDenied" not in str(raised.value)
    assert "PortError" in str(raised.value)


def test_no_store_means_no_hook_rather_than_a_hook_that_pretends():
    """Without a bucket there is nowhere to push; say so instead of no-op'ing."""
    assert runner.manifest_publisher(None, {"evaluation_id": EVAL_ID}) is None


def test_a_run_that_loses_its_process_is_still_recoverable_by_id(tmp_path):
    """Recovery must not depend on the outer `finally` (R6).

    Actual process loss — SIGKILL, an OOM kill, a runner that vanishes — runs no
    `finally` block at all. So this run's `persist_state` (the pre-launch save AND
    the `finally` save) is disabled entirely, leaving the per-intent pushes as the
    only durability. A fresh runner then restores by ID and deletes the resources
    no instance sweep could have found, in the accounts they actually live in.
    """
    aws_s3 = FakeS3()
    store = statestore.S3StateStore(aws_s3, "b", clock=lambda: NOW)
    first_state = tmp_path / "run1"

    def journey(ctx):
        ctx["manifest"].record("iam_role", "eval-role", account=DESTINATION_ACCOUNT)
        ctx["manifest"].record("secret", "adp/eval/tmp")
        # ...and then the process is gone. No `finally`, no cleanup stage, no
        # report: this is what a SIGKILL mid-journey leaves behind.
        raise KeyboardInterrupt("runner killed mid-journey")

    complete = {name: (lambda _ctx: None) for name in runner.STAGES}
    complete["providers"] = journey
    saved = runner.persist_state
    runner.persist_state = lambda *_args, **_kwargs: True
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.main(
                [
                    "--mode",
                    "start",
                    "--config",
                    write_config(tmp_path),
                    "--state-dir",
                    str(first_state),
                ],
                stages=complete,
                clock=lambda: NOW,
                store=store,
            )
    finally:
        runner.persist_state = saved

    evaluation_id = json.loads((first_state / runner.STATE_FILE).read_text())[
        "evaluation_id"
    ]
    # Lose the runner outright: nothing local survives.
    import shutil

    shutil.rmtree(first_state)

    swept = []

    def sweep(ctx):
        return cleanup.sweep(
            ctx["manifest"],
            {
                kind: (
                    lambda identifier, k=kind, **location: swept.append(
                        (k, identifier, location.get("account"))
                    )
                )
                for kind in cleanup.ORDER
            },
        )[0]

    code = runner.main(
        [
            "--mode",
            "cleanup",
            "--config",
            write_config(tmp_path),
            "--state-dir",
            str(tmp_path / "fresh"),
            "--evaluation-id",
            evaluation_id,
            "--restore",
        ],
        stages={**complete, "cleanup": sweep},
        clock=lambda: NOW,
        store=store,
    )
    assert code == 0
    # Both accounts: the destination-account role is deleted with the destination
    # account's credentials, the platform secret with the platform session (R7).
    assert ("iam_role", "eval-role", DESTINATION_ACCOUNT) in swept
    assert ("secret", "adp/eval/tmp", None) in swept


def test_durable_state_is_never_uploaded_as_an_ordinary_artifact():
    """It holds credentials until cleanup; an artifact is readable and retained."""
    document, _ = workflow()
    uploads = [
        step
        for job in document["jobs"].values()
        for step in job["steps"]
        if "upload-artifact" in (step.get("uses") or "")
    ]
    for step in uploads:
        paths = str(step.get("with", {}).get("path", ""))
        assert "state.json" not in paths
        assert "manifest.json" not in paths
        assert "run-config.json" not in paths


# --------------------------------------------------------------------------
# Workflow invariants
# --------------------------------------------------------------------------
#
# The workflow is where "runs only on disposable EC2", "never from an untrusted
# ref" and "cleanup survives cancellation" are actually enforced. A future edit
# could quietly drop any of them and every test above would still pass, so the
# guarantees are pinned here.

WORKFLOW_PATH = (
    pathlib.Path(__file__).parents[2] / ".github/workflows/eval-cli-uplift.yml"
)


def workflow():
    # NOT importorskip: these tests pin the workflow's security guarantees --
    # trusted-ref only, protected environment, no schedule, sanitized artifacts.
    # Skipping them where PyYAML happens to be absent silently unenforces exactly
    # the invariants that matter, and CI is the place that must enforce them.
    # (It did: 13 of these skipped on the first PR run until pyyaml was added to
    # the offline job's install.)
    import yaml

    document = yaml.safe_load(WORKFLOW_PATH.read_text())
    # PyYAML resolves the bare `on:` key to the boolean True.
    return document, (document[True] if True in document else document["on"])


def test_the_offline_job_installs_everything_these_guards_need():
    """A guard that skips is a guard that is not enforcing anything.

    The first PR run of this workflow reported "149 passed, 13 skipped": pyyaml
    was missing, so every workflow-invariant test below silently did not run.
    The suite is small and fully offline, so nothing in it has a legitimate
    reason to skip; assert the install line covers each third-party import.
    """
    text = WORKFLOW_PATH.read_text()
    install = next(
        line for line in text.splitlines() if "pip install" in line and "pytest" in line
    )
    for package in ("pytest", "pytest-socket", "pyyaml", "ruff=="):
        assert package in install, f"offline job does not install {package}"


def test_this_suite_contains_no_conditional_skips():
    """No guard here may opt out of running.

    Checked statically rather than by observing the run, because a test that
    inspects results would itself have to skip when its collector is absent --
    the same hole it is meant to close. Every dependency is either stdlib or in
    the offline job's install line, so a skip here can only mean a guard quietly
    stopped enforcing something.
    """
    source = pathlib.Path(__file__).read_text()
    for pattern in ("importorskip", "pytest.skip(", "skipif"):
        occurrences = [
            line.strip()
            for line in source.splitlines()
            if pattern in line and not line.strip().startswith("#")
        ]
        # Allow only this test's own reference to the pattern names.
        assert not [line for line in occurrences if "for pattern in" not in line], (
            f"conditional skip via {pattern}: {occurrences}"
        )


def test_workflow_offers_dispatch_and_call_without_a_duplicate_schedule():
    """The single schedule is owned by nightly-cli-regression.yml."""
    _, triggers = workflow()
    assert "workflow_dispatch" in triggers
    assert "workflow_call" in triggers
    assert "schedule" not in triggers


def test_pull_requests_get_offline_checks_only():
    """A PR ref is untrusted and must never reach the real AWS role."""
    document, _ = workflow()
    assert "if" not in document["jobs"]["offline"]
    assert document["jobs"]["evaluate"]["if"] == "github.event_name != 'pull_request'"
    assert "pull_request" in document["jobs"]["recover"]["if"]


def test_live_job_is_gated_on_a_protected_environment_and_oidc():
    document, _ = workflow()
    evaluate = document["jobs"]["evaluate"]
    assert evaluate["environment"]
    assert evaluate["permissions"]["id-token"] == "write"
    steps = " ".join(str(step) for step in evaluate["steps"])
    assert "configure-aws-credentials" in steps
    # No static credential path: an operator profile is not available credentials.
    assert "aws-access-key-id" not in steps.lower()


def test_live_job_refuses_any_ref_other_than_main():
    document, _ = workflow()
    guard = document["jobs"]["evaluate"]["steps"][0]
    assert "refs/heads/main" in guard["run"]
    assert "exit 1" in guard["run"]


def test_offline_job_disables_sockets():
    """Otherwise the guards could pass because something answered, not because
    the acceptance logic is right."""
    document, _ = workflow()
    steps = " ".join(
        step.get("run", "") for step in document["jobs"]["offline"]["steps"]
    )
    assert "--disable-socket" in steps
    assert "ruff format --check" in steps


def test_recovery_job_runs_even_when_the_evaluation_was_cancelled():
    """The whole point: `always()` inside the live job cannot cover cancellation."""
    document, _ = workflow()
    recover = document["jobs"]["recover"]
    assert "always()" in recover["if"]
    assert recover["needs"] == ["evaluate"]
    body = " ".join(step.get("run", "") for step in recover["steps"])
    # recoverable_instances unions this run's ID (any age) with the age sweep, so
    # a cancellation minutes after launch is recovered rather than left billing
    # until the TTL elapses.
    assert "recoverable_instances" in body
    assert "terminate-instances" in body


def test_recovery_sweep_converts_launch_time_in_the_library_not_inline():
    """Pin the UTC conversion to `launch_epoch`, because reverting it is SILENT.

    The two sweep fixes fail differently when regressed, which is why this guard
    exists for one of them. Passing the old exit-code list to `recovery_report`
    raises `AttributeError` on `.items()`, so that half is self-enforcing: the
    live step dies loudly. Re-inlining `time.mktime` is not. `launch_epoch`
    accepts an already-numeric epoch by design, so a skewed float computed in the
    YAML flows straight through it, every unit test still passes, and a genuinely
    expired instance silently stops expiring — the exact leak F2 fixed.

    So assert on the workflow text: the conversion must go through the tested
    library helper, and `mktime` must not reappear as executable code in the
    sweep. Nothing else in the suite can catch that edit, because unit tests
    cannot reach inline workflow YAML.
    """
    document, _ = workflow()
    steps = document["jobs"]["recover"]["steps"]
    sweep = next(
        step for step in steps if "terminate-instances" in (step.get("run") or "")
    )
    body = sweep["run"]

    assert "cleanup.launch_epoch(" in body, (
        "sweep must convert LaunchTime via the tested library helper"
    )
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert "mktime" not in code, (
        "time.mktime reads EC2's UTC LaunchTime as local time; ages skew by the runner's offset and negative ages never expire"
    )


def test_recovery_sweep_confirms_termination_by_re_reading_state():
    """`clean` must come from observed state, not from a terminate call's exit code.

    `terminate-instances` exits 0 for an instance held by
    `DisableApiTermination` or stuck behind a lifecycle hook. Without the
    re-read the sweep reports a clean account while the instance keeps billing,
    and the operator stops looking. This pins the re-read into the step that
    runs when the cleanup manifest is gone.
    """
    document, _ = workflow()
    steps = document["jobs"]["recover"]["steps"]
    sweep = next(
        step for step in steps if "terminate-instances" in (step.get("run") or "")
    )
    body = sweep["run"]

    assert "describe-instances" in body.split("terminate-instances", 1)[1], (
        "sweep must re-read instance state AFTER requesting termination"
    )
    assert "State.Name" in body
    # The observed mapping is what recovery_report decides `clean` from.
    assert "recovery_report(targets, observed" in body


def test_recovery_sweep_does_not_abort_before_verifying_a_refused_terminate():
    """AWS refuses a protected instance with a NON-ZERO exit.

    `check=True` on the terminate call would raise before the state poll, so the
    termination-protected case -- the headline reason this verification exists --
    would report a bare traceback instead of naming the instance in
    `outstanding`, and would abandon every remaining target unswept.
    """
    document, _ = workflow()
    sweep = next(
        step["run"]
        for step in document["jobs"]["recover"]["steps"]
        if "terminate-instances" in (step.get("run") or "")
    )
    # Comments stripped for the same reason as the test above: this step's
    # comments discuss `check=True` to explain why it is absent.
    sweep = "\n".join(
        line for line in sweep.splitlines() if not line.strip().startswith("#")
    )

    terminate_call = sweep[sweep.index("terminate-instances") :]
    guard = terminate_call[: terminate_call.index("describe-instances")]
    assert "check=True" not in guard, (
        "check=True aborts the sweep before the state re-read; a refused termination must still be observed and reported"
    )


def test_recovery_job_carries_the_same_trusted_ref_gate_as_the_live_job():
    """`always()` makes this job the one an untrusted ref could still reach.

    It holds the same destructive role as `evaluate`, and the code deciding what
    to terminate would come from the dispatched ref, so the gate must be here too
    -- and before checkout, or the untrusted code is already on disk.
    """
    document, _ = workflow()
    steps = document["jobs"]["recover"]["steps"]
    guard_at = next(
        i
        for i, step in enumerate(steps)
        if "refs/heads/main" in (step.get("run") or "")
    )
    checkout_at = next(
        i for i, step in enumerate(steps) if "checkout" in (step.get("uses") or "")
    )
    credentials_at = next(
        i
        for i, step in enumerate(steps)
        if "configure-aws-credentials" in (step.get("uses") or "")
    )
    assert guard_at < checkout_at < credentials_at


def test_recovery_job_verifies_the_account_before_terminating_anything():
    """A misconfigured secret plus a tag sweep could delete another account's resources."""
    document, _ = workflow()
    steps = document["jobs"]["recover"]["steps"]
    account_at = next(
        i
        for i, step in enumerate(steps)
        if "get-caller-identity" in (step.get("run") or "")
    )
    terminate_at = next(
        i
        for i, step in enumerate(steps)
        if "terminate-instances" in (step.get("run") or "")
    )
    assert account_at < terminate_at


def test_recovery_job_is_gated_on_the_protected_environment():
    """Same destructive role, so the same approval scope."""
    document, _ = workflow()
    recover = document["jobs"]["recover"]
    assert "inputs.environment" in str(recover["environment"])
    assert recover["permissions"]["id-token"] == "write"


def test_recovery_sweeps_more_than_instances():
    """Roles, stacks, secrets and Cognito users have no age-based sweep at all."""
    document, _ = workflow()
    body = " ".join(
        step.get("run", "") for step in document["jobs"]["recover"]["steps"]
    )
    assert "--mode cleanup" in body  # manifest-driven sweep of non-EC2 resources
    assert "--restore" in body  # ...which needs the state restored by ID first


def test_recovery_receives_the_evaluation_id_from_the_live_job():
    """Without the ID, recovery falls back to age alone and cannot clean up early."""
    document, _ = workflow()
    assert "evaluation_id" in document["jobs"]["evaluate"]["outputs"]
    recover = document["jobs"]["recover"]
    assert "needs.evaluate.outputs.evaluation_id" in json.dumps(recover)


def test_the_builder_writes_one_validated_config_readable_only_by_its_owner(tmp_path):
    """It names the fixtures a run will touch, so 0600 even though it holds no secret."""
    target = tmp_path / "nested" / "run-config.json"
    build_run_config.main([str(target)])
    written = json.loads(target.read_text())
    # Validated, not merely merged: reloading it must be a no-op.
    assert config.load(str(target)) == written
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_the_overlay_supplies_the_bindings_the_recovery_sweep_needs():
    """R3+R5 together: absent bindings are what "No destination role" really was."""
    resolved = config.from_environment(
        {
            "CLI_UPLIFT_EVAL_STATE_BUCKET": STATE_BUCKET,
            "CLI_UPLIFT_EVAL_DESTINATION_ROLE_ARN": f"arn:aws:iam::{DESTINATION_ACCOUNT}:role/adp-eval-destination",
            "CLI_UPLIFT_EVAL_PROVISIONER_ROLE_ARN": f"arn:aws:iam::{DESTINATION_ACCOUNT}:role/adp-eval-provisioner",
            "CLI_UPLIFT_EVAL_CREDENTIAL_SECRET_NAME": "adp/cli-uplift-eval/fixture",
        }
    )
    assert (
        config.require_bindings(resolved, FULL)
        == config.BINDINGS + config.FIXTURE_BINDINGS
    )
    assert resolved["state_bucket"] == STATE_BUCKET


def test_an_overlaid_role_arn_in_the_wrong_account_is_refused():
    """A destination ARN naming the platform account proves no cross-account access."""
    with pytest.raises(config.ConfigError) as raised:
        config.from_environment(
            {
                "CLI_UPLIFT_EVAL_DESTINATION_ROLE_ARN": f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/adp-eval-destination"
            }
        )
    assert PLATFORM_ACCOUNT in str(raised.value)


def test_an_absent_overlay_value_stays_absent_rather_than_becoming_a_placeholder():
    """A placeholder ARN would make a cross-account test pass against one account."""
    resolved = config.from_environment({"CLI_UPLIFT_EVAL_STATE_BUCKET": "  "})
    for key in config.BINDINGS:
        assert not resolved.get(key)
    assert not resolved.get("state_bucket")
    # ...and the suites that need them then refuse to start.
    with pytest.raises(config.ConfigError):
        config.require_bindings(resolved, FULL)


def test_the_overlay_never_carries_a_credential():
    """Every overlaid name is an identifier; a secret would be refused anyway."""
    for name, key in config.OVERLAY.items():
        assert name.startswith("CLI_UPLIFT_EVAL_")
        leaf = key.split(".")[-1]
        assert leaf in config.SECRET_KEY_ALLOWED or not config.SECRET_KEYS.search(
            leaf
        ), f"{name} overlays {key}, which looks like a credential"


def test_the_evaluation_id_is_published_before_the_run_not_from_the_report():
    """R6: the run that needs recovery by ID is the one that never reports.

    The ID used to be read out of report.json, written by the LAST step. A
    cancelled run never reaches it, so `evaluate.outputs.evaluation_id` was empty
    for exactly the case recovery exists for, and the sweep fell back to age —
    which cannot touch an instance younger than the four-hour TTL.
    """
    document, _ = workflow()
    step = next(
        step
        for step in document["jobs"]["evaluate"]["steps"]
        if step.get("id") == "run"
    )
    # An ARGUMENT, not a mention: the step's own comments talk about the flag, so
    # matching the whole script would pass with the flag itself deleted.
    passed = [
        line.strip()
        for line in step["run"].splitlines()
        if "--announce-id-to" in line and not line.strip().startswith("#")
    ]
    assert passed, "the run step does not pass --announce-id-to"
    assert all("$GITHUB_OUTPUT" in line for line in passed), passed
    # And the report must NOT re-emit it, or the value that survives cancellation
    # is the one a later successful step overwrites.
    assert "evaluation_id=" not in step["run"].split("report.json")[-1]


def test_the_announced_id_reaches_the_output_before_any_stage_runs(tmp_path):
    """Proved through the entry point, not by reading the workflow.

    The file the flag appends to is `$GITHUB_OUTPUT` in Actions. Here it is a tmp
    file, and the assertion is made from INSIDE the first stage: if the ID is not
    there yet, a run cancelled during that stage publishes nothing.
    """
    announced = tmp_path / "outputs.txt"
    announced.write_text("existing=value\n")  # Actions appends; other steps write here
    seen = {}

    complete = {name: (lambda _ctx: None) for name in runner.STAGES}
    complete["preflight"] = lambda _ctx: seen.update(
        {"at_first_stage": announced.read_text()}
    )
    complete["cleanup"] = lambda _ctx: True
    runner.main(
        [
            "--mode",
            "start",
            "--config",
            write_config(tmp_path),
            "--state-dir",
            str(tmp_path / STATE),
            "--announce-id-to",
            str(announced),
        ],
        stages=complete,
        clock=lambda: NOW,
        store=None,
    )
    evaluation_id = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())[
        "evaluation_id"
    ]
    assert f"evaluation_id={evaluation_id}" in seen["at_first_stage"]
    assert "existing=value" in announced.read_text()  # appended, never truncated


def test_both_jobs_build_the_same_config_from_the_same_overlay():
    """R5: the recovery job used to fall back to the checked-in example.

    That example has no `state_bucket`, so `--restore` raised "needs a durable
    state store" on every single run; the step converted it to a warning and the
    gate went green while the run's IAM roles, stacks, secrets and Cognito users
    were still live. The two jobs are now the same config BY CONSTRUCTION — one
    builder, one overlay — and this is the assertion that keeps them that way.
    """
    document, _ = workflow()
    evaluate, recover = document["jobs"]["evaluate"], document["jobs"]["recover"]
    overlay = set(config.OVERLAY)
    assert overlay <= set(evaluate["env"]), overlay - set(evaluate["env"])
    assert overlay <= set(recover["env"]), overlay - set(recover["env"])
    # Same builder, not two copies of the overlay logic.
    for job in (evaluate, recover):
        body = " ".join(step.get("run", "") for step in job["steps"])
        assert "tests.e2e.cli_uplift.build_run_config" in body
        assert "$EVAL_RUN_CONFIG" in body
    # And the sweep reads THAT config, never the example.
    sweep = next(
        step
        for step in recover["steps"]
        if "--mode cleanup" in (step.get("run") or "")
        or "--mode cleanup" in str(step.get("run"))
    )
    assert "config.example.json" not in (sweep.get("run") or "")


def test_the_recovery_job_installs_what_a_durable_store_needs():
    """R5: boto3 was never installed here.

    `statestore.default_store` imports boto3 lazily, so with a bucket configured
    and no boto3 the state-based sweep raised ImportError — which the step then
    swallowed into a warning. The absence was invisible precisely because the
    import is lazy.
    """
    document, _ = workflow()
    steps = document["jobs"]["recover"]["steps"]
    install_at = next(
        i for i, step in enumerate(steps) if "pip install" in (step.get("run") or "")
    )
    assert "boto3" in steps[install_at]["run"]
    sweep_at = next(
        i for i, step in enumerate(steps) if "--mode cleanup" in (step.get("run") or "")
    )
    assert install_at < sweep_at


def test_an_incomplete_state_sweep_fails_the_recovery_gate():
    """A leak reported as a warning is indistinguishable from a clean run."""
    document, _ = workflow()
    sweep = next(
        step
        for step in document["jobs"]["recover"]["steps"]
        if "--mode cleanup" in (step.get("run") or "")
    )
    assert "::error::" in sweep["run"]
    assert "exit 1" in sweep["run"]
    assert "::warning::" not in sweep["run"]


def test_reusable_workflow_has_no_independent_schedule():
    """Evaluation and recovery run under the parent, never on separate crons."""
    document, triggers = workflow()
    assert "schedule" not in triggers
    assert "schedule" not in json.dumps(document.get("jobs", {}))


def test_live_run_is_not_cancelled_in_progress():
    """Cancelling mid-journey is exactly when resources leak."""
    document, _ = workflow()
    assert document["concurrency"]["cancel-in-progress"] is False


def test_workflow_uploads_only_sanitized_artifacts():
    """state.json and the manifest hold live credentials until cleanup."""
    document, _ = workflow()
    upload = next(
        step
        for step in document["jobs"]["evaluate"]["steps"]
        if "upload-artifact" in str(step.get("uses", ""))
    )
    paths = upload["with"]["path"]
    assert "report.json" in paths and "results.xml" in paths
    assert "state.json" not in paths
    assert "manifest.json" not in paths


def test_workflow_configures_durable_state_so_a_run_is_recoverable_by_id():
    """Without a bucket, nothing in the run survives its own runner.

    The bucket now arrives through the JOB-level overlay both jobs declare (R5),
    and the builder is the one place that reads it — so that is where the
    guarantee is asserted, rather than in one job's inline heredoc.
    """
    document, _ = workflow()
    evaluate = document["jobs"]["evaluate"]
    assert "CLI_UPLIFT_EVAL_STATE_BUCKET" in json.dumps(evaluate["env"])
    assert config.OVERLAY["CLI_UPLIFT_EVAL_STATE_BUCKET"] == "state_bucket"
    step = next(
        step
        for step in evaluate["steps"]
        if "build the run config" in (step.get("name") or "").lower()
    )
    assert "build_run_config" in step["run"]
    # And says so loudly when it is absent, rather than leaving the operator to
    # discover it when a cancelled run cannot be cleaned up. The warning is part
    # of the builder's own summary, so BOTH jobs print it.
    printed = " ".join(
        build_run_config.summary(config.validate(config_fixture(state_bucket="")))
    )
    assert "DISABLED" in printed
    assert "CLI_UPLIFT_EVAL_STATE_BUCKET" in printed


def test_both_jobs_carry_the_deployment_bindings_variable():
    """#5413. The recovery job rebuilds this run's config on its own runner.

    If only `evaluate` declared it, a recovery sweep would rebuild a config with
    no deployment bindings — validating fine, since absent is legal — and then
    decline to clean up resources it could not see it had created. That is the R5
    defect class the identical-env-block rule exists to prevent, so the new
    variable has to be in both blocks or in neither.
    """
    document, _ = workflow()
    for name in ("evaluate", "recover"):
        env = json.dumps(document["jobs"][name]["env"])
        assert "CLI_UPLIFT_EVAL_DEPLOYMENTS" in env, f"{name} cannot see the bindings"
    # Its own variable, not a scalar overlay entry: the value is a JSON array, and
    # the scalar path would store the literal string and fail validation pointing
    # at the config rather than at the malformed variable.
    assert config.DEPLOYMENTS_VARIABLE == "CLI_UPLIFT_EVAL_DEPLOYMENTS"
    assert config.DEPLOYMENTS_VARIABLE not in config.OVERLAY


def test_the_operator_is_told_which_deployments_will_run_not_how_many():
    """ "3 deployments" was printable while all three named one gateway.

    The names are what let an operator see at a glance that this is
    dev/integration/preprod and not one URL under three labels — and that a run
    about to grade E16/E17 is bound to the fixture they think it is.
    """
    bound = " ".join(
        build_run_config.summary(
            config.validate(config_fixture(deployments=deployment_bindings()))
        )
    )
    assert "development" in bound and "integration" in bound and "preprod" in bound
    # Absent says which cases that costs, so BLOCKED is never a surprise.
    absent = " ".join(build_run_config.summary(config.validate(config_fixture())))
    assert "E16/E17" in absent and "BLOCK" in absent


def test_the_multi_deployment_suite_is_documented_on_the_dispatch_input():
    """An operator choosing a suite must be told what it needs to run.

    `multi-deployment` is the only suite whose fixture cannot be created from the
    workflow — it needs three real gateways — so the input description is where
    that has to be said, not in a file they would have to go and find.
    """
    _document, triggers = workflow()
    description = triggers["workflow_dispatch"]["inputs"]["suites"]["description"]
    assert "multi-deployment" in description
    assert "CLI_UPLIFT_EVAL_DEPLOYMENTS" in description
    # And it is a real suite, so choosing it is not a silent no-op.
    assert "multi-deployment" in cases.SUITES


def test_workflow_restores_durable_state_when_given_an_evaluation_id():
    """An ID names a run from another runner; its state dir is not here.

    Without --restore, resume/status/cleanup would inspect an empty state
    directory and print "nothing to do" for a run with live resources.
    """
    document, _ = workflow()
    step = next(
        step
        for step in document["jobs"]["evaluate"]["steps"]
        if step.get("id") == "run"
    )
    assert "--restore" in step["run"]
    # Not on a fresh start: there is nothing to restore, and demanding a store
    # would make durable state a hard prerequisite for running at all.
    assert '!= "start"' in step["run"]


def test_workflow_verifies_the_harness_pin_and_isolation_contract():
    document, _ = workflow()
    step = next(
        step
        for step in document["jobs"]["evaluate"]["steps"]
        if "pinned harness" in (step.get("name") or "")
    )
    assert "HARNESS_COMMIT" in step["run"]
    assert "BG_CONFIG_DIR" in step["run"]


def test_workflow_asserts_no_instances_survive_cleanup():
    document, _ = workflow()
    step = next(
        step
        for step in document["jobs"]["evaluate"]["steps"]
        if "left behind" in (step.get("name") or "")
    )
    assert step["if"] == "always()"
    assert cleanup.OWNER_TAG in step["run"]
    assert "exit 1" in step["run"]


def test_dispatch_faults_match_the_runner_and_exclude_anything_destructive():
    _, triggers = workflow()
    offered = triggers["workflow_dispatch"]["inputs"]["inject_fault"]["options"]
    assert set(offered) == set(runner.FAULTS)


def test_dispatch_requires_the_expected_revision():
    """Results must be bound to a deployment, so there is no 'whatever is live'."""
    _, triggers = workflow()
    assert (
        triggers["workflow_dispatch"]["inputs"]["expected_revision"]["required"] is True
    )


def test_dispatch_offers_every_runner_mode():
    _, triggers = workflow()
    modes = triggers["workflow_dispatch"]["inputs"]["mode"]["options"]
    assert set(modes) == {"start", "resume", "status", "cleanup"}


# --------------------------------------------------------------------------
# The checked-in example config
# --------------------------------------------------------------------------

EXAMPLE_PATH = (
    pathlib.Path(__file__).parents[2] / "tests/e2e/cli_uplift/config.example.json"
)


def test_example_config_validates():
    """It is the file an operator copies and the workflow's base layer."""
    resolved = config.load(str(EXAMPLE_PATH))
    assert resolved["region"] == "us-east-1"
    assert resolved["max_instances"] == 1


def test_example_config_carries_no_secret_shaped_keys():
    payload = json.loads(EXAMPLE_PATH.read_text())
    for key in payload:
        assert key in config.SECRET_KEY_ALLOWED or not config.SECRET_KEYS.search(key)


def test_example_config_leaves_unestablished_fixtures_absent():
    """An absent fixture must block its cases, not be approximated."""
    resolved = config.load(str(EXAMPLE_PATH))
    available = preflight.evaluate_fixtures(resolved)
    assert cases.GITHUB_APP not in available
    assert cases.HOSTED not in available
    # The example config binds no destination roles either, so the cross-account
    # cases block alongside them rather than attempting a destination account
    # they hold no session for.
    assert cases.DESTINATION not in available
    # #5413: likewise the three named deployments. The example file is checked in
    # and describes no real environment, so it cannot name three reachable
    # gateways; E16/E17 therefore block here exactly as the GitHub cases do.
    assert cases.THREE_DEPLOYMENTS not in available
    # #5637: and the Superplane domain. The example config names no deployed
    # domain service, so E18 blocks for the same reason and by the same mechanism.
    assert cases.SUPERPLANE_DOMAIN not in available
    matrix = cases.new_matrix(FULL)
    blocked = cases.block_missing_fixtures(matrix, available)
    assert set(blocked) == {
        "E04",
        "E05",
        "E06",
        "E07",
        "E08",
        "E09",
        "E10",
        "E11",
        "E12",
        "E16",
        "E17",
        # #5621: E19 needs a deployment exhibiting a disabled module and a
        # non-admin identity. A checked-in example file describes no such
        # environment, so it blocks here exactly as the GitHub cases do.
        "E18",
        "E19",
        "E39",
        "E42",
        "E25",
        "E27",
    }
    # The rest of the matrix stays runnable: one absent fixture class must not
    # take down the cases that do not depend on it.
    assert {
        case_id for case_id, entry in matrix.items() if entry["status"] == cases.NOT_RUN
    } == {
        "E01",
        "E02",
        "E03",
        "E13",
        "E14",
        "E15",
        "E20",
        "E21",
        "E22",
        "E23",
        "E24",
        "E33",
        "E28",
        "E29",
        "E31",
        "E26",
        "E40",
        "E41",
        "E36",
        "E35",
        "E38",
        "E34",
        "E32",
        "E37",
        "E30",
    }


# --------------------------------------------------------------------------
# Runbook accuracy
# --------------------------------------------------------------------------
#
# Two documents describing one contract is how the second goes stale, and a
# stale runbook is how false confidence spreads during an incident. These tests
# keep the operator-facing names honest against the code.

RUNBOOK_PATH = (
    pathlib.Path(__file__).parents[2] / "docs/runbooks/cli-uplift-evaluation.md"
)


def runbook():
    return RUNBOOK_PATH.read_text()


def test_runbook_names_only_real_suites():
    """Every suite the runbook OFFERS must exist, or an operator dispatches a typo.

    Scoped to the offer itself — the `Suites:` list up to the paragraph break —
    rather than everything between two headings. The wider span swept up any
    backticked lower-case word in the surrounding prose, so explaining what a
    suite needs (`blocked`, `three_deployments`, `credential_secret_name`) failed
    a test about suite NAMES. The property worth keeping is that the list offers
    nothing `cases.SUITES` does not have; forbidding prose around it was never
    part of that, and would push the explanation somewhere less useful.
    """
    doc = runbook()
    start = doc.index("Suites:")
    section = doc[start : doc.index("\n\n", start)]
    offered = set(re.findall(r"`([a-z-]+)`", section))
    for name in offered:
        assert name in cases.SUITES, f"runbook offers unknown suite {name!r}"
    # ...and offers all of them, so a suite cannot be added without being
    # documented. This is the half the old span could not assert, because prose
    # names could not be told apart from offers.
    assert offered == set(cases.SUITES)


def test_runbook_names_only_real_faults():
    doc = runbook()
    section = doc[doc.index("## Fault injection") : doc.index("## Troubleshooting")]
    offered = {
        name for name in re.findall(r"`(\w+)`", section) if not name.startswith("test_")
    }
    assert offered <= set(runner.FAULTS), (
        f"runbook offers unknown faults {offered - set(runner.FAULTS)}"
    )
    # And every real fault is documented, so none is a hidden lever.
    assert set(runner.FAULTS) - {"none"} <= offered


def test_runbook_documents_every_runner_mode():
    doc = runbook()
    assert set(re.findall(r"mode=(\w+)", doc)) == {
        "start",
        "resume",
        "status",
        "cleanup",
    }


def test_runbook_does_not_tell_an_operator_to_read_the_revision_from_health():
    """It used to, and `/health` returns `{"status": "healthy"}` — `.revision` is null.

    The same wrong assumption was in preflight, where it aborted every live run
    before anything launched. Both were fixed together; this keeps the doc from
    drifting back, since a runbook that quietly stops working is how false
    confidence spreads during an incident.
    """
    doc = runbook()
    section = doc[: doc.index("### Full run")]
    assert "/api/health | jq -r '.revision" not in section
    # It names the evidence that actually carries the revision instead.
    assert "orchestration-tick" in section
    assert "imageDigest" in section


def test_runbook_verification_command_uses_the_real_ownership_tag():
    """An operator pasting this after a cancelled run must find real instances."""
    assert cleanup.OWNER_TAG in runbook()


def test_runbook_states_that_state_json_is_never_published():
    doc = runbook()
    assert "state.json" in doc
    assert (
        "never"
        in doc[doc.index("state.json") - 200 : doc.index("state.json") + 400].lower()
    )


def test_runbook_is_indexed():
    index = (pathlib.Path(__file__).parents[2] / "docs/runbooks/README.md").read_text()
    assert "cli-uplift-evaluation.md" in index


def test_runbook_points_at_the_schema_file_that_exists():
    """A dead link to the result contract sends a consumer to guess the shape."""
    assert "report.schema.json" in runbook()
    for target in re.findall(r"\]\((\.\.?/[^)]+)\)", runbook()):
        assert (RUNBOOK_PATH.parent / target).resolve().exists(), target


def test_runbook_names_report_json_status_as_the_authority():
    text = runbook()
    assert "authoritative verdict" in text or "authoritative" in text


# --------------------------------------------------------------------------
# The shipped E13 script, at the boundary that matters
#
# Same standard as E14: extract the bundle, serve the release AND the gateway API
# over loopback, and run `remote/api_parity.py` — the bytes the instance receives
# — against a really-installed CLI. Nothing about the case is asserted from the
# orchestrator's side, because that is what "E13 has a driver registered" would
# prove, and the reviewer's standard is that a stage being callable is not the
# implementation working.
#
# What is doubled here is only transport: the gateway (a loopback HTTP server that
# serves `/api/cli/<name>` from git blobs and answers the live API routes from a
# fixture store) and the IMDS ownership document. Everything the case decides is
# then decided by the product's own code — `adp-aws.py`'s `connections()`,
# `resolve_connection()`, `status_of()`, `adp-bedrock.py`'s `destinations()` and
# `current_mapping()` — imported from the installed prefix and CALLED. Which is
# the whole point of E13: a response that drops a field the CLI dereferences must
# raise inside the product, on the line a user's command would have raised on.
# --------------------------------------------------------------------------


# A gateway that serves the CLI release and answers the routes both consumers
# read. Runs in a subprocess for the same reason the release server does:
# `--disable-socket` stays fully armed in the test process.
API_SERVER_SOURCE = '''
import base64, io, http.server, json, socketserver, sys, urllib.parse, zipfile
root, portfile, statefile = sys.argv[1], sys.argv[2], sys.argv[3]
state = json.load(open(statefile))

def package_for(label):
    """The setup package the gateway builds for one connection.

    Nickname must equal that connection's label or the CLI refuses the package --
    which is a real check in `setup_package`, so it is honoured here rather than
    worked around.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("template.yaml", "Resources: {}\\n")
        archive.writestr("parameters.json", json.dumps([
            {"ParameterKey": "Nickname", "ParameterValue": label},
            {"ParameterKey": "ExternalId", "ParameterValue": "x" * 32},
            {"ParameterKey": "UserSessionTag", "ParameterValue": "user-eval"},
        ]))
        archive.writestr("README.md", "# setup\\n")
    return base64.b64encode(buffer.getvalue()).decode()

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route(self):
        return urllib.parse.urlsplit(self.path).path

    def do_GET(self):
        path = self.route()
        if path.startswith("/api/cli/"):
            return super().do_GET()
        if path == "/api/auth/credentials":
            return self.send_json(200, state["credentials"])
        if path == "/api/admin/bedrock-routing/destinations":
            return self.send_json(200, state["destinations"])
        if path == "/api/admin/bedrock-routing/mappings":
            return self.send_json(200, state["mappings"])
        if path.startswith("/api/auth/credentials/aws/") and path.endswith("/setup"):
            identifier = path.split("/")[-2]
            setup = dict(state["setup"])
            row = next(
                (r for r in state["credentials"] if r["id"] == identifier), None
            )
            if row is None:
                return self.send_json(404, {"detail": {"error": "not_found"}})
            setup["credential_id"] = identifier
            setup["role_arn"] = "arn:aws:iam::%s:role/ADP-Agent-%s" % (
                setup["account_id"],
                row["label"],
            )
            # The real gateway rebuilds the package from the connection's stored
            # ExternalId, so Nickname is that connection's label. The CLI checks
            # exactly this (`setup_package`), which is why the package is built
            # per-request here rather than once: a fixed Nickname is refused.
            setup["download_base64"] = package_for(row["label"])
            return self.send_json(200, setup)
        return self.send_json(404, {"detail": {"error": "not_found"}})

    def do_POST(self):
        path = self.route()
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/api/auth/credentials/aws/connect":
            identifier = state["next_id"]
            state["next_id"] = identifier + "x"
            state["credentials"].append(
                {
                    **state["row_template"],
                    "id": identifier,
                    "label": body["nickname"],
                    "scopes": {
                        "account_id": body["account_id"],
                        "status": "pending",
                    },
                }
            )
            return self.send_json(200, {"credential_id": identifier})
        return self.send_json(404, {"detail": {"error": "not_found"}})

    def do_DELETE(self):
        path = self.route()
        prefix = "/api/auth/credentials/"
        if not path.startswith(prefix):
            return self.send_json(404, {"detail": {"error": "not_found"}})
        identifier = path[len(prefix):]
        if identifier in (state.get("deletable_unowned") or []):
            # The authorization hole under test: a row this identity does not own,
            # deleted with a cheerful 204.
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        row = next((r for r in state["credentials"] if r["id"] == identifier), None)
        if row is None:
            # An id that is not the caller's is indistinguishable from absent,
            # which is what the real gateway does for an unowned credential.
            return self.send_json(404, {"detail": {"error": "not_found"}})
        if not state.get("ignore_delete"):
            state["credentials"].remove(row)
        # A 204 either way: the delete that is ACCEPTED but not honoured is why the
        # CLI reads the list back rather than trusting the status code.
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

class Reusable(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

import functools
with Reusable(("127.0.0.1", 0), functools.partial(Handler, directory=root)) as httpd:
    with open(portfile, "w") as handle:
        handle.write(str(httpd.server_address[1]))
    httpd.serve_forever()
'''


def credential_row(**overrides):
    """One `/auth/credentials` row, complete under the browser's declaration."""
    row = {
        "id": "conn-existing",
        "service": "aws",
        "label": "existing",
        "credential_type": "aws_role",
        "scope": "user",
        "scopes": {"account_id": "605440105851", "status": "verified"},
        "expires_at": None,
        "last_used_at": None,
        "strict": False,
        "created_at": "2026-09-15T00:00:00Z",
        "updated_at": None,
    }
    row.update(overrides)
    return row


def destination_row(**overrides):
    row = {
        "connection_id": None,
        "id": "dest-1",
        "account_id": "605440105851",
        "label": "dest",
        "region": "us-east-1",
        "source": "admin-registered",
        "owner_org_id": "org-eval",
        "routing_capable": True,
        "verified_at": "2026-09-15T00:00:00Z",
        "usable_for_routing": True,
        "reason": None,
        "used_by": 1,
    }
    row.update(overrides)
    return row


def mapping_row(**overrides):
    row = {
        "id": "map-1",
        "scope_type": "org",
        "scope_id_org": "org-eval",
        "scope_id_team": None,
        "scope_id_user": None,
        "scope": "org:org-eval",
        "destination_id": "dest-1",
        "destination_account_id": "605440105851",
        "destination_label": "dest",
        "destination_usable": True,
        "source": "platform_admin",
        "updated_at": "2026-09-15T00:00:00Z",
    }
    row.update(overrides)
    return row


def api_fixture_state(**overrides):
    state = {
        "credentials": [credential_row()],
        "destinations": [destination_row()],
        "mappings": [mapping_row()],
        "row_template": credential_row(),
        "next_id": "conn-created",
        "setup": {
            "account_id": "605440105851",
            "region": "us-east-1",
            "download_base64": "",
        },
    }
    state.update(overrides)
    return state


def local_gateway(tmp_path, state, revision=GOOD_REVISION):
    """Serve the release AND the live API routes over one loopback origin."""
    import subprocess
    import sys
    import time

    root = tmp_path / "gateway"
    directory = root / "api" / "cli"
    directory.mkdir(parents=True, exist_ok=True)
    for name in release.manifest(revision):
        (directory / name).write_bytes(
            release.git_blob(revision, f"{release.CLI_DIR}/{name}")
        )
    # The setup package is built PER REQUEST inside the server, because the CLI
    # requires its Nickname to match the connection's own label.
    statefile = tmp_path / "api-state.json"
    statefile.write_text(json.dumps(state))
    portfile = tmp_path / "api.port"
    process = subprocess.Popen(  # noqa: S603 - our own source, no shell
        [
            sys.executable,
            "-c",
            API_SERVER_SOURCE,
            str(root),
            str(portfile),
            str(statefile),
        ],
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if portfile.is_file() and portfile.read_text().strip():
            break
        assert process.poll() is None, "the local gateway exited at startup"
        time.sleep(0.05)
    else:  # pragma: no cover - reported rather than hanging the run
        process.kill()
        raise AssertionError("the local gateway never published its port")
    return process, f"http://127.0.0.1:{portfile.read_text().strip()}/api"


DISPATCH_SOURCE = """
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
# Exactly what the dispatcher does on the instance: its own directory first, so
# `import common` resolves to the shipped file and not to anything in this repo.
sys.path.insert(0, str(root / "remote"))
import common
# The ONLY double in this child: IMDS. There is no metadata service here, the
# address is link-local and hardcoded in the product for good reason, and the
# assertion it feeds (`assert_owned_instance`) still runs against this document.
identity = json.loads(sys.argv[2])
common.instance_identity = lambda: identity
import dispatcher
sys.exit(dispatcher.main(sys.argv[3:]))
"""


def run_e13(
    tmp_path, *, state=None, overrides=None, revision=GOOD_REVISION, session=True
):
    """Run the shipped E13 script the way the instance runs it: through dispatcher.

    In a SUBPROCESS, and not merely to satisfy `--disable-socket`. E13's whole
    method is to call the installed CLI's reader functions IN PROCESS, so unlike
    E14 -- whose product calls are all subprocesses -- its consumer traffic is
    this interpreter's traffic. Running it in a child keeps the socket guard fully
    armed for the test process (a green offline run still proves nothing reached
    AWS or the real gateway) while the only thing opening a socket is a child
    talking to 127.0.0.1.

    The entry point is `dispatcher.main(["api_parity", payload])` -- the exact argv
    `live.py` builds -- so the registry lookup, the defaults merge, the ownership
    assertion, the payload file and the always-emit-evidence contract are all
    production code. The CLI is installed by the release's own `install.sh` first,
    because E13 IMPORTS the installed helpers: an install this test faked would be
    checking files it wrote itself.
    """
    import os
    import subprocess
    import sys

    root = extracted_bundle(tmp_path)
    _script, common = shipped_script(tmp_path, "api_parity")
    server, gateway = local_gateway(tmp_path, state or api_fixture_state(), revision)
    previous_path = None
    try:
        previous_path = os.environ.get("PATH", "")
        home = tmp_path / "home"
        prefix = home / ".adp" / "bin"
        prefix.mkdir(parents=True)
        installer = tmp_path / "install.sh"
        installer.write_bytes(
            release.git_blob(revision, f"{release.CLI_DIR}/install.sh")
        )
        code, _out, _err = common.bounded(
            [
                "sh",
                str(installer),
                "--prefix",
                str(prefix),
                "--gateway-url",
                gateway,
                "--no-path-edit",
            ],
            env={"HOME": str(home), "PATH": previous_path},
            timeout=300,
        )
        assert code == 0, "the release under test did not install"

        # The precondition install_auth establishes on the real instance.
        work_dir = tmp_path / "adp-eval"
        reference = (
            seed_session_vault(work_dir) if session else str(work_dir / "session.json")
        )

        document = {
            "instance_id": "i-0eval",
            "platform_account": "879318057152",
            "destination_account": "605440105851",
            "gateway_url": gateway,
            "region": "us-east-1",
            "sts_endpoint": "https://sts-fips.us-east-1.amazonaws.com",
            "secrets_endpoint": "https://secretsmanager-fips.us-east-1.amazonaws.com",
            # A reference to the private on-instance vault, exactly as the
            # orchestrator supplies it in production. The tokens are NOT in the
            # payload: anything credential-shaped in evidence is redacted on its way
            # out of the instance, so a payload carrying "tokens" carried the literal
            # string "<redacted>". Written above by `seed_session_vault`.
            "session_ref": reference,
            "work_dir": str(work_dir),
            "session_expires_at": NOW + 3600,
            "evaluation_id": EVAL_ID,
            "cli_path": str(prefix / "adp"),
            "org_id": "org-eval",
            "test_user_id": "user-eval",
            "connection_name": EVAL_ID + "-parity",
            "absent_connection_id": "00000000-0000-4000-8000-000000000000",
            # Derived from the revision under test by the production extractor,
            # exactly as preflight derives it. NOT written out here: a hand-written
            # contract would let this test agree with itself while the real one
            # drifted.
            "ui_contracts": contracts.wire_contracts(revision),
        }
        document.update(overrides or {})
        payload = tmp_path / "payload.json"
        payload.write_text(json.dumps(document))
        identity = {"instanceId": "i-0eval", "accountId": "879318057152"}
        finished = subprocess.run(  # noqa: S603 - our own source, no shell
            [
                sys.executable,
                "-c",
                DISPATCH_SOURCE,
                str(root),
                json.dumps(identity),
                "api_parity",
                str(payload),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            # No inherited HOME, and no inherited PATH beyond the one this test
            # built: the script must find the CLI it was told about.
            env={"HOME": str(home), "PATH": previous_path},
        )
        return finished, document
    finally:
        server.kill()
        server.wait()
        if previous_path is not None:
            os.environ["PATH"] = previous_path


def e13_evidence(finished):
    """The one JSON document the dispatcher printed, as the orchestrator reads it."""
    for line in reversed((finished.stdout or "").splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise AssertionError(
        "the shipped script emitted no JSON document; stderr: "
        + (finished.stderr or "")[-2000:]
    )


def test_the_shipped_e13_script_runs_the_real_consumers_against_the_live_api(
    tmp_path,
):
    """The successful path: both consumers, a created resource, and its removal.

    The CLI reader functions are the installed release's own, imported from the
    prefix `install.sh` wrote and called against a live origin. The connection is
    created by `adp aws connect --download` — a real product command — and removed
    by `adp aws disconnect`, with absence asserted through `connections()` rather
    than inferred from the 204.
    """
    finished, _document = run_e13(tmp_path)
    evidence = e13_evidence(finished)
    assert finished.returncode == 0, evidence.get("error") or finished.stderr[-2000:]
    assert evidence["success"] is True and evidence["stage"] == "complete"
    # It went through the dispatcher, which is what stamps the purpose.
    assert evidence["purpose"] == "api_parity"
    assert evidence["checks"] == [
        "installed_cli_readers_ran_against_live_responses",
        "live_rows_satisfy_the_browser_declared_wire_types",
        "rows_are_scoped_to_the_calling_identity",
        "cli_created_resource_is_complete_for_both_consumers",
        "created_connection_was_removed_through_the_cli",
    ]

    # The CLI's own readers really ran: these counts come from `connections()` and
    # `destinations()` returning rows the product could subscript.
    reads = evidence["cli_reads"]
    assert reads["connection_rows"] == 1
    assert reads["statuses"] == ["verified"]
    assert reads["destination_rows"] == 1 and reads["destinations_usable"] == 1
    # `current_mapping` resolved through the CLI's own scope shape.
    assert reads["org_mapping_resolved"] is True
    assert reads["org_mapping_destination_account"] == "605440105851"

    # All three surfaces were checked against the browser's declaration, and the
    # declaration was the extracted one, not a list written in this file.
    #
    # Routes and contract names appear as VALUES in the evidence, not as keys,
    # because `common.redact` erases any key matching "credential" -- which
    # `/auth/credentials` and `CredentialItem` both do. Asserting on them here is
    # what keeps that structure from regressing into a silently redacted check.
    assert [entry["route"] for entry in evidence["ui_checked"]] == [
        "/admin/bedrock-routing/destinations",
        "/admin/bedrock-routing/mappings",
        "/auth/credentials",
    ]
    assert [entry["contract"] for entry in evidence["ui_contracts"]] == [
        "CredentialItem",
        "DestinationSummary",
        "MappingSummary",
    ]
    assert "<redacted>" not in json.dumps(evidence["ui_contracts"])

    # A real create through the product, complete for both consumers, then removed.
    equivalence = equivalence_of(evidence)
    assert equivalence["created_via"] == "adp aws connect --download"
    assert equivalence["visible_to_shared_list_endpoint"] is True
    assert equivalence["resolvable_by_name"] is True
    assert equivalence["complete_for_ui_contract"] is True
    assert equivalence["disconnected"] is True
    assert sorted(equivalence["handoff_files"]) == [
        "README.md",
        "connection.json",
        "parameters.json",
        "template.yaml",
    ]

    # Removal is CLAIMED explicitly, which is what lets the manifest close the
    # record. An id reported as a resource but not as removed stays pending.
    assert evidence["resources"] == [["adp_connection", "conn-created"]]
    assert evidence["removed"] == [["adp_connection", "conn-created"]]
    assert evidence["correlation"]["parity_connection_removed"] is True
    # No token or ExternalId reached the evidence.
    assert "<token>" not in json.dumps(evidence)


def equivalence_of(evidence):
    assert evidence.get("equivalence"), "the case reported no equivalence evidence"
    return evidence["equivalence"]


# The failure paths. A green happy path proves the case CAN pass; these prove it
# can still FAIL, which is the only thing that makes the pass mean anything. Each
# breaks the live API in one realistic way -- the way a regression would break it
# -- and asserts E13 catches it, names it, and reports non-zero.


def test_e13_fails_when_the_api_drops_a_field_the_cli_dereferences(tmp_path):
    """A dropped `credential_type` must raise inside the PRODUCT, not in a check.

    This is the whole reason the shipped script imports the installed readers
    rather than listing expected fields: `connections()` subscripts this key, so
    its absence fails on the exact line a user's `adp aws list` would fail on. A
    KeyError -- not a RemoteError -- is therefore the correct evidence, and the
    always-emit contract is what makes it visible.
    """
    state = api_fixture_state()
    del state["credentials"][0]["credential_type"]
    finished, _document = run_e13(tmp_path, state=state)
    evidence = e13_evidence(finished)

    assert finished.returncode == 1
    assert evidence["success"] is False
    assert evidence["stage"] == "cli_consumer"
    # The product's own code raised, so there is no curated message -- exactly what
    # distinguishes a real consumer from an assertion this harness wrote.
    assert evidence["error_type"] == "KeyError"
    assert evidence["checks"] == []


def test_e13_fails_when_a_row_is_null_where_the_browser_declares_non_null(tmp_path):
    """`label: string` receiving null is a rendered "undefined", so it must fail.

    The CLI tolerates it -- `connections()` never type-checks -- which is why the
    browser's declaration is checked independently. The contract comes from the
    revision under test, so this is the real declaration disagreeing with the real
    response.
    """
    state = api_fixture_state()
    state["destinations"] = [destination_row(label=None)]
    finished, _document = run_e13(tmp_path, state=state)
    evidence = e13_evidence(finished)

    assert finished.returncode == 1
    assert evidence["stage"] == "ui_consumer"
    assert evidence["error_type"] == "RemoteError"
    assert "browser" in evidence["error"] and "label" in evidence["error"]
    # The CLI consumer had already passed: this is the UI half doing the work.
    assert evidence["checks"] == ["installed_cli_readers_ran_against_live_responses"]


def test_e13_fails_when_a_row_carries_a_state_outside_a_declared_union(tmp_path):
    """A new enum member the UI has no branch for is a silent rendering failure.

    `MappingSource` is `'platform_admin' | 'self'`. A server that starts returning
    a third value satisfies "string" and would pass any type-only check, which is
    why literal unions are compared by VALUE.
    """
    state = api_fixture_state()
    state["mappings"] = [mapping_row(source="inherited")]
    finished, _document = run_e13(tmp_path, state=state)
    evidence = e13_evidence(finished)

    assert finished.returncode == 1
    assert evidence["stage"] == "ui_consumer"
    assert "source" in evidence["error"]


def test_e13_refuses_to_run_at_all_without_the_browser_contract(tmp_path):
    """No contract must be a hard failure, never a quiet downgrade to CLI-only.

    If an absent declaration merely skipped the UI half, a renamed interface would
    turn E13 into a CLI self-consistency check that still reported a pass -- the
    precise failure mode of a default-None driver.
    """
    finished, _document = run_e13(tmp_path, overrides={"ui_contracts": {}})
    evidence = e13_evidence(finished)

    assert finished.returncode == 1
    assert evidence["stage"] == "contracts"
    assert evidence["error_type"] == "RemoteError"
    assert "must not pass on the CLI consumer" in evidence["error"]
    # Nothing was checked, and nothing was created: it refused before any mutation.
    assert evidence["checks"] == []
    assert "resources" not in evidence


def test_e13_fails_when_a_contract_the_case_checks_was_not_supplied(tmp_path):
    """A partial contract is as bad as none: the missing surface goes unchecked."""
    partial = contracts.wire_contracts(GOOD_REVISION)
    partial.pop("MappingSummary")
    finished, _document = run_e13(tmp_path, overrides={"ui_contracts": partial})
    evidence = e13_evidence(finished)

    assert finished.returncode == 1
    assert evidence["stage"] == "contracts"
    assert "MappingSummary" in evidence["error"]


def test_e13_reports_the_connection_it_created_when_a_later_check_fails(tmp_path):
    """A leak must be attributable even when the case fails after creating it.

    R8's lesson, applied to a journey: the id is recorded the moment the create
    returns, and the name before the call, so the sweep can find the row whether
    or not the response ever arrived. A case that only reported its resources on
    success would leak exactly when a human is least likely to notice.
    """
    state = api_fixture_state()
    # The connection is created, then the disconnect at the end of the case fails
    # to remove it -- a delete the server accepts but does not honour, which is a
    # real class of gateway bug and the one `disconnect` reads the list back for.
    state["ignore_delete"] = True
    finished, _document = run_e13(tmp_path, state=state)
    evidence = e13_evidence(finished)

    assert finished.returncode == 1
    assert evidence["error_type"] == "RemoteError"
    # The created row IS reported for the sweep...
    assert evidence["resources"] == [["adp_connection", "conn-created"]]
    # ...and is NOT claimed as removed, so the manifest keeps it pending.
    assert "removed" not in evidence
    assert evidence["correlation"]["parity_connection_id"] == "conn-created"
    assert "parity_connection_removed" not in evidence["correlation"]
    # The equivalence claim itself stood up; only the teardown did not.
    assert "cli_created_resource_is_complete_for_both_consumers" in evidence["checks"]
    assert "created_connection_was_removed_through_the_cli" not in evidence["checks"]


def test_e13_fails_when_an_unowned_credential_id_is_deletable(tmp_path):
    """Authorization is the server's to enforce, so a 204 here is the finding.

    The check goes through the raw transport deliberately: the CLI refuses an id
    absent from the caller's own list before any request, so a CLI-only check
    would pass while the endpoint happily deleted other tenants' rows.
    """
    state = api_fixture_state()
    # The unowned id now answers 204 instead of 404/403 -- an authorization hole.
    state["deletable_unowned"] = ["00000000-0000-4000-8000-000000000000"]
    finished, _document = run_e13(tmp_path, state=state)
    evidence = e13_evidence(finished)

    assert finished.returncode == 1
    assert evidence["stage"] == "ownership"
    assert "expected it to be refused" in evidence["error"]
    assert evidence["ownership"]["unowned_delete_status"] == 204


def test_e13_fails_when_the_user_scoped_list_leaks_another_identity(tmp_path):
    """A row owned by someone else in a user-scoped list is a tenant isolation bug."""
    state = api_fixture_state()
    state["credentials"] = [credential_row(user_id="user-somebody-else")]
    finished, _document = run_e13(tmp_path, state=state)
    evidence = e13_evidence(finished)

    assert finished.returncode == 1
    assert evidence["stage"] == "ownership"
    assert "another identity" in evidence["error"]
    assert evidence["ownership"]["scoped_to_caller"] is False


# The recovery checkpoint uses the production stage mapping, including delivery
# and cleanup. It must not reach destination IAM, GitHub, or admin setup.
def test_login_checkpoint_needs_only_platform_and_native_login(tmp_path):
    def doubles(cfg):
        evidence = worker_evidence()
        # The login suite does not select E02, so the journey reuses the shared
        # fixture instead of creating its own identity. Both of these say that:
        # `created_username` is what makes a Cognito user this run's to remove, and
        # the ADP account only gets registered for an identity the run created —
        # `_onboard` returns early otherwise, because a shared fixture already has
        # one and must not be re-provisioned.
        evidence["install_auth"]["session"].pop("created_username")
        evidence["install_auth"].pop("resources")
        evidence["install_auth"].pop("onboard")
        wired = live_doubles(cfg, evidence=evidence)

        def absent(**kwargs):
            raise ports.PortError("s3.head_object failed: 404")

        wired["aws"].replies.update(
            {
                "ec2.terminate_instances": {},
                "ec2.describe_instances": {
                    "Reservations": [{"Instances": [{"State": {"Name": "terminated"}}]}]
                },
                "s3.delete_object": {},
                "s3.head_object": absent,
            }
        )
        return wired

    result = run_live_stages(
        tmp_path,
        extra=("--suite", "login"),
        doubles=doubles,
        destination_role_arn="",
        provisioner_role_arn="",
    )
    assert result.code == 0
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert set(document["matrix"]) == {"E01", "C01"}
    assert document["cleanup_ok"] is True
    assert not result.ports["aws"].assumed
    assert result.ports["ssm"].purposes_run == ["install_auth"]
    payload = result.ports["ssm"].payloads["install_auth"]
    assert payload["login_required"] is True
    assert payload["admin_challenges_required"] is False
    assert payload["admin_setup_required"] is False
    published = json.loads((tmp_path / STATE / "out" / "report.json").read_text())
    assert published["status"] == "passed"
    assert published["full_acceptance"] is False
    assert published["partial"] is True


@pytest.mark.parametrize("failure", ["login", "cleanup"])
def test_login_checkpoint_still_fails_on_auth_or_cleanup_error(tmp_path, failure):
    def doubles(cfg):
        evidence = worker_evidence()
        if failure == "login":
            evidence["install_auth"]["success"] = False
            evidence["install_auth"]["login"]["authenticated"] = False
        return live_doubles(cfg, evidence=evidence)

    extra = ["--suite", "login"]
    if failure == "cleanup":
        extra += ["--fault", "cleanup_failure"]
    result = run_live_stages(tmp_path, doubles=doubles, extra=extra)
    assert result.code == 1


@pytest.mark.parametrize("status", [cases.BLOCKED, cases.NOT_RUN, cases.FAILED])
def test_partial_checkpoint_never_passes_an_unfinished_case(status):
    matrix = cases.new_matrix(("login",))
    cases.record(matrix, "E01", cases.PASSED)
    cases.record(matrix, "C01", status)
    assert cases.accept(matrix, ("login",))[0] == cases.FAILED


def test_readiness_names_missing_bindings_before_writing_config(tmp_path, monkeypatch):
    for name in config.OVERLAY:
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "run.json"
    with pytest.raises(config.ConfigError, match="credential_secret_name"):
        build_run_config.main([str(target), "--check-ready", "--suites", "login"])
    assert not target.exists()
    monkeypatch.setenv("CLI_UPLIFT_EVAL_CREDENTIAL_SECRET_NAME", "adp/test/login")
    with pytest.raises(config.ConfigError, match="CLI_UPLIFT_EVAL_STATE_BUCKET"):
        build_run_config.main([str(target), "--check-ready"])
    monkeypatch.setenv("CLI_UPLIFT_EVAL_STATE_BUCKET", STATE_BUCKET)
    monkeypatch.setenv("EVAL_ROLE_ARN", "")
    with pytest.raises(config.ConfigError, match="AWS_CLI_UPLIFT_EVAL_ROLE_ARN"):
        build_run_config.main([str(target), "--check-ready"])
    monkeypatch.setenv("EVAL_ROLE_ARN", "arn:aws:iam::879318057152:role/eval-actions")
    build_run_config.main([str(target), "--check-ready"])
    assert config.load(target)["credential_secret_name"] == "adp/test/login"


def test_reviewed_bindings_file_supplies_references_but_never_beats_a_variable(
    tmp_path, monkeypatch
):
    """The bindings file unblocks a run that cannot write repository variables.

    It must lose to the environment overlay, or setting the real variable later
    would silently have no effect and the file would have to be reverted by hand.
    """
    for name in config.OVERLAY:
        monkeypatch.delenv(name, raising=False)
    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps(
            {
                "_comment": "underscore keys are documentation and must not overlay",
                "state_bucket": "bucket-from-file",
                "instance_profile": "profile-from-file",
                "credential_secret_name": "adp/from/file",
            }
        )
    )
    monkeypatch.setenv("CLI_UPLIFT_EVAL_BINDINGS", str(bindings))

    resolved = config.from_environment(os.environ)
    assert resolved["state_bucket"] == "bucket-from-file"
    assert resolved["instance_profile"] == "profile-from-file"
    assert resolved["credential_secret_name"] == "adp/from/file"
    # The bindings file's own documentation key never reaches the run config.
    assert "_comment" not in {
        key
        for key in resolved
        if key not in json.loads(config.EXAMPLE_PATH.read_text())
    }

    # A repository variable wins, so handing control back needs no revert.
    monkeypatch.setenv("CLI_UPLIFT_EVAL_STATE_BUCKET", "bucket-from-variable")
    assert config.from_environment(os.environ)["state_bucket"] == "bucket-from-variable"


def test_bindings_file_cannot_smuggle_a_credential_or_be_silently_absent(
    tmp_path, monkeypatch
):
    """The file is reviewed, but the guard is structural rather than trusting review."""
    for name in config.OVERLAY:
        monkeypatch.delenv(name, raising=False)
    leaky = tmp_path / "leaky.json"
    leaky.write_text(json.dumps({"admin_password": "hunter2"}))
    monkeypatch.setenv("CLI_UPLIFT_EVAL_BINDINGS", str(leaky))
    with pytest.raises(config.ConfigError, match="looks like a secret"):
        config.from_environment(os.environ)

    # A typo'd path must fail loudly: silently ignoring it would resurrect the
    # "Durable state: DISABLED" run that reported clean while resources lived.
    monkeypatch.setenv("CLI_UPLIFT_EVAL_BINDINGS", str(tmp_path / "absent.json"))
    with pytest.raises(config.ConfigError, match="could not be read"):
        config.from_environment(os.environ)


def test_checked_in_dev_bindings_carry_the_login_references_and_no_secret(monkeypatch):
    """The actual file the workflow points at must satisfy the login checkpoint."""
    path = pathlib.Path("tests/e2e/cli_uplift/bindings.dev.json")
    document = json.loads(path.read_text())
    # Loading it through the real code path is what proves it passes the secret
    # guard, since that is where documentation keys are dropped first.
    for name in config.OVERLAY:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLI_UPLIFT_EVAL_BINDINGS", str(path))
    resolved = config.from_environment(os.environ)
    assert resolved["instance_profile"] == "adp-cli-uplift-eval-instance"
    assert (
        resolved["credential_secret_name"] == "adp/dev/gateway/test-admin-credentials"
    )
    assert resolved["state_bucket"]
    # This file's own documentation keys stay out of the run config. (The example
    # config's `_`-prefixed prose is pre-existing and deliberately retained.)
    own_docs = {key for key in document if str(key).startswith("_")}
    example = json.loads(
        pathlib.Path("tests/e2e/cli_uplift/config.example.json").read_text()
    )
    assert own_docs - set(example) and not (own_docs - set(example)) & set(resolved)
    # Destination/GitHub bindings must stay ABSENT: a placeholder would let a
    # cross-account test run against one account, and a weaker GitHub fixture
    # would pass cases that should block.
    for key in ("destination_role_arn", "provisioner_role_arn", "github"):
        assert key not in document


def load_remote_common():
    """Import the SHIPPED remote/common.py the way the instance imports it."""
    import importlib.util

    path = pathlib.Path("tests/e2e/cli_uplift/remote/common.py")
    spec = importlib.util.spec_from_file_location("cli_uplift_remote_common", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_login_fixture_reader_accepts_the_established_unprefixed_keys():
    """The dev fixture stores username/password; the journeys read admin_*.

    A missing key raises by design (so a typo cannot degrade a check into a
    skip), which is exactly why the checkpoint failed on a fixture that is
    correct and correctly owned. Exercised through `fixture_secret` itself rather
    than by asserting the alias table, so the precedence rules are really tested.
    """
    module = load_remote_common()
    fixture = {"username": "admin-fixture", "password": "from-unprefixed-key"}
    module._SECRET_CACHE.clear()
    module._SECRET_CACHE["adp/dev/gateway/test-admin-credentials"] = fixture
    cfg = {"credential_secret": "adp/dev/gateway/test-admin-credentials"}

    # The alias resolves, so the login journey can read the real fixture.
    assert module.fixture_secret(cfg, {}, "admin_username") == "admin-fixture"
    assert module.fixture_secret(cfg, {}, "admin_password") == "from-unprefixed-key"

    # A canonical key still wins over its alias.
    module._SECRET_CACHE["adp/dev/gateway/test-admin-credentials"] = {
        "username": "unprefixed",
        "admin_username": "canonical",
    }
    assert module.fixture_secret(cfg, {}, "admin_username") == "canonical"

    # The alias must NOT leak across roles: a non-admin lookup can never resolve
    # to the admin identity, which would silently escalate the case under test.
    module._SECRET_CACHE["adp/dev/gateway/test-admin-credentials"] = fixture
    with pytest.raises(Exception, match="non_admin_username"):
        module.fixture_secret(cfg, {}, "non_admin_username")
    assert module.fixture_secret(cfg, {}, "non_admin_username", default="") == ""
    module._SECRET_CACHE.clear()


def test_ci_uses_explicit_oidc_without_role_chaining():
    """#6004: the bounded runtime ceiling denies role-chaining's sts:AssumeRole.

    Both jobs must use explicit OIDC (secrets only, no hardcoded fallback ARN),
    no role-chaining, and no static keys. Ambient credentials must be cleared
    before acquiring the purpose-scoped OIDC identity.
    """
    document, _ = workflow()
    for job in ("evaluate", "recover"):
        auth = next(
            s
            for s in document["jobs"][job]["steps"]
            if "configure-aws-credentials@" in s.get("uses", "")
        )
        role = auth["with"]["role-to-assume"]
        # Only secrets, no vars or hardcoded ARN fallback.
        assert "AWS_CLI_UPLIFT_EVAL_ROLE_ARN" in role
        assert "AWS_E2E_ROLE_ARN" in role
        assert "adp-cli-uplift-eval-orchestrator" not in role, (
            "Hardcoded fallback ARN must be removed"
        )
        assert "CLI_UPLIFT_EVAL_ORCHESTRATOR_ROLE_ARN" not in role, (
            "vars-based fallback must be removed"
        )
        # No role chaining at all.
        assert auth["with"]["role-chaining"] is False
        assert auth["with"]["unset-current-credentials"] is True
        assert auth["with"]["force-skip-oidc"] is False
        # No static-key path anywhere.
        assert "aws-access-key-id" not in auth["with"]
        # Session tagging must stay off.
        assert auth["with"]["role-skip-session-tagging"] is True
    # OIDC allows the full 3h duration (no chained-session cap).
    evaluate_auth = next(
        s
        for s in document["jobs"]["evaluate"]["steps"]
        if "configure-aws-credentials@" in s.get("uses", "")
    )
    assert evaluate_auth["with"]["role-duration-seconds"] == 10800


def test_ci_fails_closed_when_oidc_secret_is_missing():
    """#6004: a missing OIDC secret must fail the job with a clear error,
    not silently fall back to ambient runner authority."""
    document, _ = workflow()
    for job in ("evaluate", "recover"):
        steps = document["jobs"][job]["steps"]
        # Find the fail-closed guard step — it must exist and precede credentials.
        guard_indices = [
            i
            for i, s in enumerate(steps)
            if "ROLE_ARN" in (s.get("env", {}).get("ROLE_ARN", "") or s.get("run", ""))
            and "exit 1" in (s.get("run") or "")
        ]
        assert guard_indices, (
            f"Job '{job}' must have a fail-closed guard for the OIDC role"
        )
        guard_at = guard_indices[0]
        creds_at = next(
            i
            for i, s in enumerate(steps)
            if "configure-aws-credentials@" in s.get("uses", "")
        )
        assert guard_at < creds_at, (
            f"Job '{job}': fail-closed guard must precede the credential step"
        )
        guard = steps[guard_at]
        assert "AWS_CLI_UPLIFT_EVAL_ROLE_ARN" in guard["run"], (
            "The error message must name the missing secret"
        )
        assert "AWS_E2E_ROLE_ARN" in guard["run"]


def test_ci_points_both_jobs_at_the_same_reviewed_bindings_file():
    """Divergence here is what made the recovery sweep report clean falsely."""
    document, _ = workflow()
    paths = {
        document["jobs"][job]["env"]["CLI_UPLIFT_EVAL_BINDINGS"]
        for job in ("evaluate", "recover")
    }
    assert paths == {
        "tests/e2e/cli_uplift/bindings.${{ inputs.environment || 'dev' }}.json"
    }
    for environment in ("dev", "pre-production"):
        assert pathlib.Path(
            f"tests/e2e/cli_uplift/bindings.{environment}.json"
        ).is_file()


def test_ci_fetches_release_history_and_defaults_to_login_checkpoint():
    document, _ = workflow()
    checkout = next(
        s
        for s in document["jobs"]["offline"]["steps"]
        if "actions/checkout@" in s.get("uses", "")
    )
    assert checkout["with"]["fetch-depth"] == 0
    steps = document["jobs"]["evaluate"]["steps"]
    ready = next(i for i, s in enumerate(steps) if "--check-ready" in s.get("run", ""))
    auth = next(
        i
        for i, s in enumerate(steps)
        if "configure-aws-credentials@" in s.get("uses", "")
    )
    # Revision discovery is an authenticated read; readiness must still precede
    # execution, which is the first stage allowed to create evaluation resources.
    pin = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Pin the deployment for this EC2 suite"
    )
    execute = next(
        i for i, step in enumerate(steps) if step.get("name") == "Run the evaluation"
    )
    assert auth < pin < ready < execute
    fetch = next(s for s in steps if s.get("name") == "Fetch the expected CLI release")
    assert 'git fetch --no-tags --depth 1 origin "$REVISION"' in fetch["run"]
    assert workflow()[1]["workflow_dispatch"]["inputs"]["suites"]["default"] == "login"


LOGIN_SERVER_SOURCE = r"""
import functools, http.server, json, pathlib, sys
class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args): pass
    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == "/api/auth/cli/admin-session":
            return self.reply(200, {"verified": True, "user_id": "test-admin", "org_id": "test-org", "role": "platform_admin"})
        return super().do_GET()
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        with open(sys.argv[3], "a") as log: log.write(self.path + "\n")
        if self.path == "/api/auth/cli/password" and body.get("password") != "fixture-only":
            return self.reply(401, {"detail": "invalid credentials"})
        if self.path not in ("/api/auth/cli/password", "/api/auth/cli/refresh"):
            return self.reply(404, {})
        return self.reply(200, {"access_token": "fixture-access", "id_token": "fixture-id", "refresh_token": "fixture-refresh", "expires_in": 3600, "client_id": "cli123", "user_pool_id": "us-east-1_JEhv9xSGG", "region": "us-east-1"})
with http.server.HTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=sys.argv[1])) as server:
    pathlib.Path(sys.argv[2]).write_text(str(server.server_port))
    server.serve_forever()
"""


@pytest.mark.parametrize("password", ["fixture-only", "incorrect"])
def test_shipped_login_checkpoint_installs_and_uses_real_cli(tmp_path, password):
    """Run the bundled dispatcher and actual release CLI, with only HTTP/IMDS
    and Secrets Manager replaced. This catches installer and auth wiring bugs
    that canned SSM evidence cannot detect. No AWS or deployed gateway is used.
    """
    import subprocess
    import sys
    import time

    root = tmp_path / "server"
    artifacts = root / "api" / "cli"
    artifacts.mkdir(parents=True)
    for name in release.manifest(GOOD_REVISION):
        (artifacts / name).write_bytes(
            release.git_blob(GOOD_REVISION, f"{release.CLI_DIR}/{name}")
        )
    portfile, calls = tmp_path / "port", tmp_path / "calls"
    server = subprocess.Popen(
        [
            sys.executable,
            "-c",
            LOGIN_SERVER_SOURCE,
            str(root),
            str(portfile),
            str(calls),
        ]
    )
    try:
        deadline = time.monotonic() + 10
        while not portfile.exists():
            assert server.poll() is None and time.monotonic() < deadline
            time.sleep(0.02)
        remote = extracted_bundle(tmp_path) / "remote"
        payload = {
            "instance_id": "i-test",
            "sts_endpoint": "https://sts-fips.us-east-1.amazonaws.com",
            "secrets_endpoint": "https://secretsmanager-fips.us-east-1.amazonaws.com",
            "platform_account": PLATFORM_ACCOUNT,
            "evaluation_id": EVAL_ID,
            "region": "us-east-1",
            "gateway_url": f"http://127.0.0.1:{portfile.read_text()}/api",
            "expected_hashes": release.manifest(GOOD_REVISION),
            "login_required": True,
            "admin_challenges_required": False,
            "admin_setup_required": False,
            "work_dir": str(tmp_path / "work"),
        }
        payloadfile = tmp_path / "payload.json"
        payloadfile.write_text(json.dumps(payload))
        code = """
import sys
sys.path.insert(0, sys.argv[1])
import common, dispatcher
common.instance_identity = lambda: {"instanceId": "i-test", "accountId": "879318057152"}
fixture = {"admin_username": "test-admin", "admin_password": sys.argv[3]}
common.fixture_secret = lambda cfg, env, key, **kw: fixture.get(key, kw.get("default", ""))
sys.exit(dispatcher.main(["install_auth", sys.argv[2]]))
"""
        finished = subprocess.run(
            [sys.executable, "-c", code, str(remote), str(payloadfile), password],
            capture_output=True,
            text=True,
            timeout=45,
        )
        evidence = json.loads(finished.stdout.strip().splitlines()[-1])
        assert evidence.get("install", {}).get("hashes_match") is True, evidence
        assert "fixture-access" not in finished.stdout
        assert "fixture-only" not in finished.stdout
        if password == "fixture-only":
            assert finished.returncode == 0, evidence
            assert evidence["login"]["authenticated"] is True
            assert evidence["login"]["refresh_succeeded"] is True
            assert "setup" not in evidence
            # Where the vault actually landed, through the production entry point
            # rather than through a direct `_session_document` call. `execute`
            # chooses the directory, so this is the only place a wrong level
            # (`work_dir/cli` instead of `work_dir`) is observable: it would put
            # the session where no later journey resolves it while every other
            # assertion still passed.
            session = evidence["session"]
            assert session["session_ref"] == str(payload["work_dir"]) + "/session.json"
            assert session["cli_path"] == str(payload["work_dir"]) + "/cli/adp"
            # And nothing token-shaped came out with it.
            for key in ("access_token", "id_token", "refresh_token"):
                assert key not in session
            assert calls.read_text().splitlines() == [
                "/api/auth/cli/password",
                "/api/auth/cli/password",
                "/api/auth/cli/refresh",
            ]
        else:
            assert finished.returncode != 0
            assert evidence["success"] is False
    finally:
        server.terminate()
        server.wait(timeout=5)


# --------------------------------------------------------------------------
# E03 reads the envelope, not the exit code
#
# `adp` exits 4 when any provider is `pending` and 5 when one is `failed` --
# documented convention, and `common.Cli`'s own docstring says the envelope is
# the contract. E03 nonetheless required exit 0 from `admin setup --yes`, so on
# a dev environment where bedrock and github are legitimately unprovisioned the
# case failed on a harness expectation and reported it as a product defect
# (run 35101482739). These drive the SHIPPED `_setup` against a stub CLI that
# reproduces the real exit codes.
# --------------------------------------------------------------------------


class ExitCodeCli:
    """A `common.Cli` stand-in that returns real envelopes with real exit codes.

    Deliberately enforces `expected` exactly as the shipped `Cli.run` does, so a
    test here fails for the same reason the instance would.
    """

    def __init__(self, steps, *, rerun_status="pending"):
        self.steps = steps
        self.rerun_status = rerun_status
        self.calls = []

    def _envelope(self, args):
        dry = "--dry-run" in args
        status = (
            "failed"
            if any(s["status"] == "failed" for s in self.steps)
            else "pending"
            if any(s["status"] in ("pending", "unavailable") for s in self.steps)
            else "configured"
        )
        if not dry:
            status = self.rerun_status
        code = 5 if status == "failed" else 4 if status == "pending" else 0
        # `adp admin setup --dry-run` maps a pending 4 down to 0, but NOT a
        # failed 5 -- mirrored from adp-admin.py:main.
        if dry and code == 4:
            code = 0
        return code, {
            "status": status,
            "command": "admin setup",
            "detail": {"steps": list(self.steps)},
        }

    def run(self, args, *, expected=0, **_kwargs):
        self.calls.append(list(args))
        code, payload = self._envelope(args)
        if expected is not None and code != expected:
            raise AssertionError(f"exited {code}, expected {expected}")
        return code, payload

    def json(self, args, *, expected=0, **kwargs):
        return self.run(args, expected=expected, **kwargs)[1]


def run_shipped_setup(tmp_path, cli):
    script, _common = shipped_script(tmp_path, "install_auth")
    evidence = {"transcript": [], "checks": []}
    script._setup({}, evidence, cli)
    return evidence


def test_e03_accepts_a_pending_rerun_because_pending_exits_four(tmp_path):
    """The exact dev shape: bedrock and github unprovisioned, so `--yes` exits 4.

    E03's subject is the SHAPE of the rerun -- no duplicated step, no regression
    -- which is independent of how much happens to be configured. Requiring exit
    0 made an unprovisioned environment indistinguishable from a broken product.
    """
    cli = ExitCodeCli(
        [
            {"name": "bedrock", "title": "Model access", "status": "pending"},
            {"name": "github", "title": "GitHub App", "status": "pending"},
        ]
    )
    evidence = run_shipped_setup(tmp_path, cli)
    assert evidence["setup"]["statuses_accurate"] is True
    assert evidence["setup"]["dry_run_is_read_only"] is True
    assert evidence["setup"]["rerun_completed_missing_only"] is True
    assert evidence["setup"]["duplicates"] == []
    assert ["admin", "setup", "--yes"] in cli.calls


def test_e03_still_fails_a_rerun_that_regresses_a_configured_provider(tmp_path):
    """The property E03 exists to protect must still fail. Not a blanket pass."""

    # The rerun reports a previously-configured provider back as pending.
    class Regressing(ExitCodeCli):
        def _envelope(self, args):
            code, payload = super()._envelope(args)
            if "--dry-run" not in args:
                payload["detail"]["steps"] = [
                    {"name": "bedrock", "title": "Model access", "status": "pending"}
                ]
            return code, payload

    regressing = Regressing(
        [{"name": "bedrock", "title": "Model access", "status": "configured"}]
    )
    with pytest.raises(Exception, match="regressed"):
        run_shipped_setup(tmp_path, regressing)


def test_e03_still_fails_when_a_rerun_duplicates_a_provider_step(tmp_path):
    """The other invariant: a rerun must not duplicate an entry."""

    class Duplicating(ExitCodeCli):
        def _envelope(self, args):
            code, payload = super()._envelope(args)
            if "--dry-run" not in args:
                payload["detail"]["steps"] = self.steps + self.steps
            return code, payload

    cli = Duplicating(
        [{"name": "bedrock", "title": "Model access", "status": "pending"}]
    )
    with pytest.raises(Exception, match="duplicated"):
        run_shipped_setup(tmp_path, cli)


def test_e03_surfaces_a_failed_provider_as_a_finding_not_an_exit_code_abort(tmp_path):
    """A `failed` provider exits 5 even under --dry-run, since only 4 is remapped.

    E03's job is to report that accurately, so it must reach its own assertions
    rather than aborting on the exit code before it can.
    """
    cli = ExitCodeCli(
        [{"name": "bedrock", "title": "Model access", "status": "failed"}],
        rerun_status="failed",
    )
    with pytest.raises(Exception, match="must not fail"):
        run_shipped_setup(tmp_path, cli)
    # It got past both dry runs (exit 5) to the rerun, which is the point.
    assert ["admin", "setup", "--yes"] in cli.calls


# --------------------------------------------------------------------------
# Credential lifetime (blocker 6)
#
# `max_run_minutes` is 180, but the chained-role path is capped at the STS
# one-hour maximum, so the orchestrator's own session can be SHORTER than the run
# it is meant to cover. The platform account has no GitHub OIDC provider (only
# EKS ones), so the longer 3h path cannot resolve today and this is a live
# condition, not a hypothetical.
#
# Before this, an expiry surfaced as whichever AWS call happened to be in flight
# when the token died -- an ExpiredToken blamed on an instance launch, or a
# cleanup that could not authenticate and so could not honestly report what it
# had failed to delete. The run must instead stop between stages, name the
# reason, and stop EARLY enough to still terminate what it created.
# --------------------------------------------------------------------------


def test_credential_expiry_reads_both_iso_and_epoch_forms(monkeypatch):
    monkeypatch.setenv(runner.CREDENTIAL_EXPIRY_ENV, "2027-01-02T03:04:05Z")
    assert runner.credential_expiry() == 1798859045
    monkeypatch.setenv(runner.CREDENTIAL_EXPIRY_ENV, "1798859045")
    assert runner.credential_expiry() == 1798859045


def test_an_unknown_or_malformed_credential_expiry_does_not_block_the_run(monkeypatch):
    """None, not an error, and not a far-future guess.

    A malformed value must not be the thing that stops a run from evaluating
    anything; the guard degrades to the pre-existing wall-clock behaviour.
    """
    monkeypatch.delenv(runner.CREDENTIAL_EXPIRY_ENV, raising=False)
    assert runner.credential_expiry() is None
    monkeypatch.setenv(runner.CREDENTIAL_EXPIRY_ENV, "not-a-timestamp")
    assert runner.credential_expiry() is None
    monkeypatch.setenv(runner.CREDENTIAL_EXPIRY_ENV, "")
    assert runner.credential_expiry() is None


def test_an_expiring_session_stops_the_run_with_a_named_credential_reason(
    tmp_path, monkeypatch, capsys
):
    """The whole point: `credentials_expired`, not a mystery ExpiredToken.

    The session dies 30s from now while `cleanup_wait_seconds` alone is 120, so
    there is not enough left to run a stage AND clean up. The run must stop at the
    stage boundary and say so.
    """
    monkeypatch.setenv(runner.CREDENTIAL_EXPIRY_ENV, str(NOW + 30))
    reached = []
    code = run_cli(
        tmp_path,
        {
            "preflight": lambda ctx: reached.append("preflight"),
            "journeys": pass_everything,
            "cleanup": lambda ctx: True,
        },
    )
    assert code == 1
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert "credentials_expired" in document["stages"].values()
    assert reached == [], "a stage ran on credentials that could not outlast it"
    capsys.readouterr()
    # The published report must name the credential as the cause, so an operator
    # reads "the session was too short" and not "preflight is broken".
    report_text = (tmp_path / STATE / "out" / "report.json").read_text()
    published = json.loads(report_text)
    assert any("credentials_expired" in reason for reason in published["reasons"]), (
        published["reasons"]
    )
    # The full operator-facing explanation is a harness error type, so it carries
    # no provider text and is safe to keep -- but it lives in the private state,
    # which is where an operator diagnosing a stopped run looks.
    assert "credentials expire" in json.dumps(document.get("errors") or []).lower()
    # And nothing in the published report leaks the session material itself.
    assert "expire_at" not in report_text or "credentials_expire_at" not in report_text
    # Distinguishable from the spend deadline, which is a different operator fix.
    assert "timed_out" not in document["stages"].values()


def test_the_credential_guard_reserves_enough_session_to_clean_up(
    tmp_path, monkeypatch
):
    """Stopping when the token dies would be too late -- cleanup needs to authenticate.

    Session outlives the stage boundary by 150s, which is MORE than zero but less
    than `cleanup_wait_seconds` (120) plus margin. It must still stop.
    """
    monkeypatch.setenv(runner.CREDENTIAL_EXPIRY_ENV, str(NOW + 150))
    swept = []
    code = run_cli(
        tmp_path,
        {
            "journeys": pass_everything,
            "cleanup": lambda ctx: swept.append(True) or True,
        },
    )
    assert code == 1
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert "credentials_expired" in document["stages"].values()
    # Cleanup still ran, on credentials that were still alive. That is the reserve
    # doing its job: the run stopped while it could still terminate what it made.
    assert swept == [True]
    assert document["cleanup_ok"] is True


def test_a_session_that_outlasts_the_run_changes_nothing(tmp_path, monkeypatch):
    """No false positives: an adequate session must not degrade the run."""
    monkeypatch.setenv(runner.CREDENTIAL_EXPIRY_ENV, str(NOW + 10_000))
    code = run_cli(tmp_path, {"journeys": pass_everything, "cleanup": lambda ctx: True})
    assert code == 0
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert "credentials_expired" not in document["stages"].values()
    assert document["credentials_expire_at"] == NOW + 10_000


def test_a_resumed_attempt_takes_the_new_session_not_the_dead_one(
    tmp_path, monkeypatch
):
    """Otherwise the mechanism that RECOVERS from an expiry refuses to start.

    Resume runs in a new job with a new session. Inheriting the previous
    attempt's expiry would make every resume-after-expiry stop immediately,
    reporting a credential it no longer holds.
    """
    monkeypatch.setenv(runner.CREDENTIAL_EXPIRY_ENV, str(NOW + 30))
    assert run_cli(tmp_path, {"cleanup": lambda ctx: True}) == 1
    stale = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert stale["credentials_expire_at"] == NOW + 30

    monkeypatch.setenv(runner.CREDENTIAL_EXPIRY_ENV, str(NOW + 10_000))
    code = run_cli(
        tmp_path,
        {"journeys": pass_everything, "cleanup": lambda ctx: True},
        mode="resume",
    )
    assert code == 0
    document = json.loads((tmp_path / STATE / runner.STATE_FILE).read_text())
    assert document["credentials_expire_at"] == NOW + 10_000
    assert "credentials_expired" not in document["stages"].values()


@pytest.fixture
def observed(monkeypatch):
    cfg = config.load(config.EXAMPLE_PATH)
    cfg["gateway_deployment"] = "dev"
    selected = dp.binding(cfg)
    digest, build = next(iter(selected["images"].items()))
    deployment = {
        "kind": "Deployment",
        "metadata": {
            "name": "bedrockgateway",
            "namespace": "adp-gateway",
            "generation": 42,
            "uid": "fixture",
        },
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": selected["selector"]},
            "template": {
                "metadata": {"labels": selected["selector"]},
                "spec": {
                    "containers": [
                        {
                            "name": "bedrockgateway",
                            "image": selected["image_repository"] + "@" + digest,
                        }
                    ]
                },
            },
        },
        "status": {
            "observedGeneration": 42,
            "replicas": 2,
            "updatedReplicas": 2,
            "readyReplicas": 2,
            "availableReplicas": 2,
            "conditions": [
                {"type": "Available", "status": "True"},
                {
                    "type": "Progressing",
                    "status": "True",
                    "reason": "NewReplicaSetAvailable",
                },
            ],
        },
    }
    service = {
        "kind": "Service",
        "metadata": {"name": "bedrockgateway", "namespace": "adp-gateway"},
        "spec": {"selector": copy.deepcopy(selected["selector"])},
    }
    cluster = {
        "name": selected["cluster"],
        "arn": selected["cluster_arn"],
        "status": "ACTIVE",
        "endpoint": "https://fixture.us-east-1.eks.amazonaws.com",
        "certificateAuthority": {"data": "fixture-ca"},
    }
    aws = Mock()
    aws.call.return_value = {"cluster": cluster}
    aws.session.return_value.get_credentials.return_value = Credentials(
        "fixture-access", "fixture-secret", "fixture-session"
    )
    reads = []

    def read(url, bearer, ca):
        reads.append((url, bearer, ca))
        return copy.deepcopy(deployment if "/deployments/" in url else service)

    monkeypatch.setattr(dp, "get_json", read)
    return cfg, aws, deployment, service, cluster, reads, build


def test_resolver_uses_actual_gateway_and_signed_cluster_auth(observed):
    cfg, aws, _, _, _, reads, build = observed
    # Deliberately different caller expectation: the resolver must not copy it.
    cfg["expected_revision"] = "f" * 40
    http = Mock()
    record = {}
    assert live._deployed_revision(aws, http, cfg)(record) == build["source_sha"]
    http.get.assert_not_called()
    aws.call.assert_called_once_with(
        "eks", "describe_cluster", name="adp-dev-eks-cluster"
    )
    assert [urlsplit(x[0]).path for x in reads] == [
        "/apis/apps/v1/namespaces/adp-gateway/deployments/bedrockgateway",
        "/api/v1/namespaces/adp-gateway/services/bedrockgateway",
    ]
    assert all(x[2] == "fixture-ca" for x in reads)
    token = reads[0][1].removeprefix("k8s-aws-v1.")
    signed = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    query = parse_qs(urlsplit(signed).query)
    assert query["Action"] == ["GetCallerIdentity"]
    assert query["X-Amz-SignedHeaders"] == ["host;x-k8s-aws-id"]
    assert query["X-Amz-Expires"] == ["60"]
    assert query["X-Amz-Security-Token"] == ["fixture-session"]
    assert "fixture-session" not in json.dumps(record)
    assert record["revision_source"] == "gateway_eks_build_receipt"


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_digest",
        "mutable_image",
        "foreign_repository",
        "missing_container",
        "service_selector",
        "deployment_selector",
        "template_selector",
        "wrong_object",
        "deleting",
        "generation",
        "old_replicas",
        "unavailable",
        "terminating",
        "zero",
        "paused",
        "progressing",
        "unready",
        "wrong_cluster",
        "cluster_inactive",
        "foreign_endpoint",
        "target_url",
        "unknown_binding",
    ],
)
def test_unproven_gateway_fails_without_lambda_fallback(observed, mutation):
    cfg, aws, d, service, cluster, _, _ = observed
    container = d["spec"]["template"]["spec"]["containers"][0]
    if mutation == "unknown_digest":
        container["image"] = container["image"].split("@")[0] + "@sha256:" + "0" * 64
    elif mutation == "mutable_image":
        container["image"] = container["image"].split("@")[0] + ":latest"
    elif mutation == "foreign_repository":
        container["image"] = container["image"].replace("adp-gateway@", "foreign@")
    elif mutation == "missing_container":
        container["name"] = "other"
    elif mutation == "service_selector":
        service["spec"]["selector"] = {"app": "foreign"}
    elif mutation == "deployment_selector":
        d["spec"]["selector"] = {"matchLabels": {"app": "foreign"}}
    elif mutation == "template_selector":
        d["spec"]["template"]["metadata"]["labels"] = {"app": "foreign"}
    elif mutation == "wrong_object":
        d["metadata"]["name"] = "other"
    elif mutation == "deleting":
        d["metadata"]["deletionTimestamp"] = "now"
    elif mutation == "generation":
        d["status"]["observedGeneration"] = 41
    elif mutation == "old_replicas":
        d["status"]["replicas"] = 3
    elif mutation == "unavailable":
        d["status"]["unavailableReplicas"] = 1
    elif mutation == "terminating":
        d["status"]["terminatingReplicas"] = 1
    elif mutation == "zero":
        d["spec"]["replicas"] = 0
    elif mutation == "paused":
        d["spec"]["paused"] = True
    elif mutation == "progressing":
        d["status"]["conditions"][1]["reason"] = "ReplicaSetUpdated"
    elif mutation == "unready":
        d["status"]["readyReplicas"] = 1
    elif mutation == "wrong_cluster":
        cluster["arn"] = cluster["arn"].replace("879318057152", "123456789012")
    elif mutation == "cluster_inactive":
        cluster["status"] = "UPDATING"
    elif mutation == "foreign_endpoint":
        cluster["endpoint"] = "https://evil.example"
    elif mutation == "target_url":
        cfg["gateway_url"] = "https://other.example/api"
    elif mutation == "unknown_binding":
        cfg["gateway_deployment"] = "other"
    http = Mock()
    with pytest.raises(PortError):
        live._deployed_revision(aws, http, cfg)({})
    http.get.assert_not_called()
    assert all(call.args[0] != "lambda" for call in aws.call.call_args_list)


def test_missing_observer_permission_is_not_a_fallback(observed):
    cfg, aws, *_ = observed
    aws.call.side_effect = PortError("AccessDenied")
    with pytest.raises(PortError):
        live._deployed_revision(aws, Mock(), cfg)({})


def test_legacy_health_path_and_fingerprint_remain_available():
    cfg = config.load(config.EXAMPLE_PATH)
    http = Mock()
    http.get.return_value = (200, {"revision": "a" * 40})
    aws = Mock()
    assert live._deployed_revision(aws, http, cfg)({}) == "a" * 40
    aws.call.assert_not_called()
    before = runner.fingerprint(cfg)
    cfg["gateway_deployment"] = "dev"
    assert runner.fingerprint(cfg) != before


def test_transport_uses_cluster_ca_bearer_no_redirects_and_scrubs_failures(monkeypatch):
    # Exercise the HTTP adapter without opening any socket.
    context = Mock()
    create = Mock(return_value=context)
    monkeypatch.setattr(dp.ssl, "create_default_context", create)
    response = Mock(status=200)
    response.read.return_value = b'{"kind":"Service"}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    opener = Mock()
    opener.open.return_value = response
    build = Mock(return_value=opener)
    monkeypatch.setattr(dp.urllib.request, "build_opener", build)
    assert dp.get_json(
        "https://fixture.eks.amazonaws.com/path",
        "never-print-me",
        base64.b64encode(b"fixture CA").decode(),
    ) == {"kind": "Service"}
    create.assert_called_once_with(cadata="fixture CA")
    request = opener.open.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer never-print-me"
    assert opener.open.call_args.kwargs["timeout"] == 30
    redirect = build.call_args.args[1]
    assert (
        redirect.redirect_request(None, None, 302, "", {}, "https://evil.example")
        is None
    )
    opener.open.side_effect = RuntimeError("never-print-me provider response")
    with pytest.raises(PortError, match="^Gateway metadata read failed$"):
        dp.get_json(
            "https://fixture.eks.amazonaws.com/path",
            "never-print-me",
            base64.b64encode(b"fixture CA").decode(),
        )


def test_observer_artifacts_grant_only_two_named_reads():
    import yaml

    directory = Path(__file__).parents[2] / "docs/evaluations/cli-uplift"
    role, binding = list(
        yaml.safe_load_all((directory / "gateway-observer-rbac.yaml").read_text())
    )
    assert (
        role["metadata"]["namespace"]
        == binding["metadata"]["namespace"]
        == "adp-gateway"
    )
    assert role["rules"] == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "resourceNames": ["bedrockgateway"],
            "verbs": ["get"],
        },
        {
            "apiGroups": [""],
            "resources": ["services"],
            "resourceNames": ["bedrockgateway"],
            "verbs": ["get"],
        },
    ]
    assert binding["subjects"] == [
        {
            "kind": "Group",
            "name": "adp:cli-uplift-gateway-observer",
            "apiGroup": "rbac.authorization.k8s.io",
        }
    ]
    policy = json.loads((directory / "gateway-observer-policy.json").read_text())
    assert policy["Statement"][0]["Action"] == "eks:DescribeCluster"
    assert (
        policy["Statement"][0]["Resource"]
        == "arn:aws:eks:us-east-1:879318057152:cluster/adp-dev-eks-cluster"
    )


def test_nightly_includes_each_merged_story_and_cannot_claim_full_acceptance():
    selected = cases.resolve_suites(("nightly",))
    assert {case.id for case in selected} == {
        "E01",
        "C01",
        "E20",
        "E21",
        "E22",
        "E23",
        "E24",
        "E33",
        "E31",
        "E25",
        "E26",
        "E27",
        "E40",
        "E41",
        "E28",
        "E39",
        "E42",
        "E36",
        "E29",
        "E35",
        "E38",
        "E34",
        "E32",
        "E37",
        "E30",
    }
    assert {cases.BY_ID[key].owner for key in ("E20", "E21", "E22", "E23")} == {
        "#5621",
        "#5628",
        "#5629",
        "#5622",
    }
    assert not cases.is_full(("nightly",))
    assert all(
        stages.JOURNEY_DRIVERS[key] in bundle.purposes()
        for key in ("E20", "E21", "E22", "E23")
    )
    matrix = cases.new_matrix(("nightly",))
    for key in matrix:
        cases.record(matrix, key, cases.PASSED)
    cases.record(matrix, "E21", cases.FAILED)
    assert cases.accept(matrix, ("nightly",))[0] == cases.FAILED


@pytest.mark.parametrize("status", [401, 403, 500, None])
def test_story_activity_does_not_mistake_other_errors_for_missing_run(tmp_path, status):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.return_value = {"status": "ok", "detail": {"items": [], "complete": True}}
    cli.run.return_value = (5, {"status": "failed", "error": {"http_status": status}})
    with pytest.raises(common.RemoteError, match="structured HTTP 404"):
        module.activity(cli, {})


@pytest.mark.parametrize(
    "code,complete,cursor", [(0, False, "next"), (4, True, None), (4, False, None)]
)
def test_story_usage_rejects_false_export_success_and_missing_cursor(
    tmp_path, code, complete, cursor
):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.return_value = {
        "status": "ok",
        "detail": {"scope": {"kind": "own"}, "items": []},
    }
    cli.run.return_value = (
        code,
        {
            "status": "ok" if complete else "pending",
            "detail": {
                "scope": {"kind": "own"},
                "items": [],
                "complete": complete,
                "next_cursor": cursor,
            },
        },
    )
    with pytest.raises(common.RemoteError):
        module.usage(cli, {})


def test_story_capabilities_rejects_unknown_auth_readiness(tmp_path):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.side_effect = [
        {"status": "ok", "detail": {"operations": [{"id": "agent.list"}]}},
        {
            "status": "ok",
            "detail": {
                "checks": {"auth": {"state": "unknown"}, "api": {"state": "ok"}}
            },
        },
    ]
    with pytest.raises(common.RemoteError, match="readiness"):
        module.capabilities(cli, {})


@pytest.mark.parametrize("mode", ["capabilities", "usage", "activity"])
def test_story_read_success_requires_no_inference_or_control_write(
    tmp_path, mode, monkeypatch
):
    module, _ = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    if mode == "capabilities":
        cli.json.side_effect = [
            {"status": "ok", "detail": {"operations": [{"id": "agent.list"}]}},
            {
                "status": "ok",
                "detail": {"checks": {"auth": {"state": "ok"}, "api": {"state": "ok"}}},
            },
        ]
    elif mode == "usage":
        cli.binary, cli.env, cli.timeout, cli.transcript = "/isolated/adp", {}, 30, []
        exporter = module.exercise_usage_exports.__globals__

        def raw_export(argv, **kwargs):
            assert argv[1:3] == ["logs", "export"]
            meta = {
                "scope": {"kind": "own"},
                "complete": True,
                "next_cursor": None,
                "start": argv[argv.index("--start") + 1],
                "end": argv[argv.index("--end") + 1],
            }
            continuation = json.dumps(
                {"type": "continuation", "status": "ok", "detail": meta}
            )
            if argv[argv.index("--format") + 1] == "ndjson":
                return 0, continuation + "\n", ""
            return 0, ",".join(exporter["COLUMNS"]) + "\n", continuation

        monkeypatch.setattr(exporter["common"], "bounded", raw_export)
        cli.json.return_value = {
            "status": "ok",
            "detail": {"scope": {"kind": "own"}, "items": []},
        }
        cli.run.return_value = (
            0,
            {
                "status": "ok",
                "detail": {"scope": {"kind": "own"}, "items": [], "complete": True},
            },
        )
    else:
        cli.json.return_value = {
            "status": "ok",
            "detail": {"items": [], "complete": True},
        }
        cli.run.return_value = (5, {"status": "failed", "error": {"http_status": 404}})
    module.SCENARIOS[mode](cli, {})
    for call in cli.method_calls:
        assert not set(call.args[0]) & {
            "pause",
            "resume",
            "steer",
            "abort",
            "submit",
            "chat",
        }


def test_vault_nightly_rejects_secret_bearing_metadata(tmp_path):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.return_value = {
        "status": "ok",
        "detail": {"items": [{"id": "owned", "value": "synthetic-secret"}]},
    }
    with pytest.raises(common.RemoteError, match="secret fields"):
        module.vault(cli, {})


def test_vault_nightly_is_selected_and_shipped():
    assert cases.BY_ID["E24"].owner == "#5631"
    assert "E24" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E24"] in bundle.purposes()


def test_hierarchy_reads_are_wired_to_existing_nightly(tmp_path):
    assert cases.BY_ID["E29"].owner == "#5623"
    assert "E29" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E29"] in bundle.purposes()
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.side_effect = [
        {"status": "ok", "detail": {"items": [{"id": "org"}]}},
        *[
            {"status": "ok", "detail": {"org_id": "org", "kind": kind, "items": []}}
            for kind in ("department", "team", "member")
        ],
    ]
    evidence = {}
    module.hierarchy(cli, evidence)
    assert evidence["org_id"] == "org"
    cli.run.assert_not_called()


def test_hierarchy_read_refuses_foreign_row(tmp_path):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.side_effect = [
        {"status": "ok", "detail": {"items": [{"id": "org"}]}},
        {
            "status": "ok",
            "detail": {
                "org_id": "org",
                "kind": "department",
                "items": [{"org_id": "foreign"}],
            },
        },
    ]
    with pytest.raises(common.RemoteError, match="Foreign hierarchy"):
        module.hierarchy(cli, {})


def test_machine_story_reads_are_wired_to_existing_nightly(tmp_path):
    assert cases.BY_ID["E31"].owner == "#5624"
    assert "E31" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E31"] in bundle.purposes()
    module, _common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.side_effect = [
        {"status": "ok", "detail": {"tenant_id": "org"}},
        *[
            {"status": "ok", "detail": {"identity_type": kind, "items": []}}
            for kind in ("sql-iam", "iam-registry", "cognito-client")
        ],
    ]
    evidence = {}
    module.machine(cli, evidence)
    assert evidence["org_id"] == "org"
    assert cli.json.call_count == 4


def test_story_budget_reads_all_periods_without_writes(tmp_path):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.side_effect = [
        {
            "status": "ok",
            "detail": {
                "period": {"period_type": p},
                "lines": [
                    {"cap_status": "uncapped", "cap_usd": None, "remaining_usd": None}
                ],
            },
        }
        for p in ("daily", "weekly", "monthly")
    ]
    evidence = {}
    module.budget(cli, evidence)
    assert [c.args[0] for c in cli.json.call_args_list] == [
        ["budget", "me", "--period", p] for p in ("daily", "weekly", "monthly")
    ]
    cli.run.assert_not_called()


def test_story_budget_rejects_uncapped_zero(tmp_path):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.return_value = {
        "status": "ok",
        "detail": {
            "period": {"period_type": "daily"},
            "lines": [
                {
                    "cap_status": "uncapped",
                    "cap_usd": "0.00",
                    "remaining_usd": "0.000000",
                }
            ],
        },
    }
    with pytest.raises(common.RemoteError, match="zero headroom"):
        module.budget(cli, {})


def test_budget_story_is_wired_into_nightly():
    assert cases.BY_ID["E26"].owner == "#5589"
    assert "E26" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E26"] in bundle.purposes()


def test_github_maintenance_nightly_is_read_and_preview_only(tmp_path):
    module, _ = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.side_effect = [
        {
            "status": "ok",
            "detail": {
                "contract": "app-maintenance-v1",
                "app_id": "123",
                "key_version": "revision",
            },
        },
        {"status": "dry_run"},
        {"status": "dry_run"},
    ]
    cli.run.return_value = (1, {"status": "failed"})
    evidence = {}
    module.github_maintenance(cli, evidence)
    for call in cli.method_calls:
        argv = call.args[0]
        assert "--yes" not in argv
        if any(action in argv for action in ("disconnect", "rotate-key")):
            assert "--dry-run" in argv
    assert "live_acceptance_hold" in evidence
    assert cases.BY_ID["E28"].owner == "#5634"
    assert "E28" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E28"] in bundle.purposes()


def test_bedrock_lifecycle_nightly_preview_has_no_probes_or_writes(tmp_path):
    assert cases.BY_ID["E34"].owner == "#5633"
    assert "E34" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E34"] in bundle.purposes()
    module, _ = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.run.side_effect = [
        (
            0,
            {
                "status": "dry_run",
                "detail": {
                    "before": {"revision": "a" * 64, "effective": {"rung": "org"}}
                },
            },
        ),
        (1, {"status": "failed", "error": {"code": "usage_error"}}),
    ]
    evidence = {}
    module.bedrock_lifecycle(cli, evidence)
    assert evidence["live_acceptance"].startswith("held:")
    for call in cli.method_calls:
        assert not {"--yes", "verify", "connect", "submit"}.intersection(call.args[0])


def test_bedrock_lifecycle_nightly_does_not_hide_missing_server(tmp_path):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.run.return_value = (
        5,
        {"status": "failed", "error": {"code": "unsupported_operation"}},
    )
    with pytest.raises(common.RemoteError, match="Unexpected personal Bedrock refusal"):
        module.bedrock_lifecycle(cli, {})


def test_gitlab_nightly_does_not_claim_live_delivery_or_write(tmp_path):
    module, _ = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.return_value = {
        "status": "ok",
        "detail": {
            "contract": "gitlab_cli_v1",
            "providers": [],
            "identity_linked": False,
            "webhook_delivery": "unverified",
            "agent_runtime": "unverified",
        },
    }
    cli.run.return_value = (1, {"status": "failed"})
    evidence = {}
    module.gitlab(cli, evidence)
    assert evidence["provider_count"] == 0
    assert "live_acceptance_hold" in evidence
    assert all("--yes" not in call.args[0] for call in cli.method_calls)
    assert cases.BY_ID["E30"].owner == "#5635"
    assert "E30" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E30"] in bundle.purposes()


@pytest.mark.parametrize("bad_group", [False, True])
def test_ec2_uses_explicit_no_ingress_group_and_rejects_foreign_vpc(
    tmp_path, bad_group
):
    launches = []
    group_id = "sg-0123456789abcdef0"

    def doubles(cfg):
        ports_double = live_doubles(cfg)
        ports_double["aws"].replies["ec2.describe_security_groups"] = {
            "SecurityGroups": [
                {
                    "GroupId": group_id,
                    "VpcId": "vpc-foreign" if bad_group else cfg["vpc_id"],
                    "IpPermissions": [],
                    "IpPermissionsEgress": [
                        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443}
                    ],
                }
            ]
        }

        def launch(**kwargs):
            launches.append(kwargs)
            return {"Instances": [{"InstanceId": "i-0abc"}]}

        ports_double["aws"].replies["ec2.run_instances"] = launch
        return ports_double

    run_live_stages(tmp_path, doubles=doubles, instance_security_group_id=group_id)
    if bad_group:
        assert launches == []
    else:
        assert len(launches) == 1
        assert launches[0]["SecurityGroupIds"] == [group_id]


def test_ratelimit_story_wired_and_read_only(tmp_path):
    assert cases.BY_ID["E36"].owner == "#5627"
    assert "E36" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E36"] in bundle.purposes()
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.return_value = {
        "status": "ok",
        "detail": {
            "runtime": {
                "tpm": "unavailable_actual_usage_not_reconciled",
                "worker_convergence": "unknown",
                "state": "configured_not_probed",
            },
            "lines": [
                {"effective": {"rpm": 60}, "sources": {"rpm": "account_type_default"}}
            ],
        },
    }
    evidence = {}
    module.ratelimit(cli, evidence)
    assert cli.json.call_args.args[0] == ["ratelimit", "me"]
    assert evidence["enforcement_qualification"] == "not_run"
    cli.run.assert_not_called()
    cli.json.return_value["detail"]["runtime"]["tpm"] = "enforced"
    with pytest.raises(common.RemoteError, match="TPM gap"):
        module.ratelimit(cli, {})


def test_person_budget_story_is_wired_into_nightly():
    assert cases.BY_ID["E35"].owner == "#5626"
    assert "E35" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E35"] in bundle.purposes()


def test_person_budget_story_retains_authority_and_refusal(tmp_path):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.side_effect = [
        {
            "status": "ok",
            "detail": {
                "configuration": {
                    "period_type": p,
                    "cap_status": "uncapped",
                    "cap_usd": None,
                },
                "authority": "platform_admin",
            },
        }
        for p in ("daily", "weekly", "monthly")
    ]
    cli.run.return_value = (
        3,
        {"status": "failed", "error": {"code": "permission_denied"}},
    )
    evidence = {}
    module.person_budget(cli, evidence)
    assert cli.json.call_count == 3
    assert cli.run.call_count == 2
    assert evidence["self_write_refusals"] == 2
    assert "spend-through-and-restoration" in evidence["live_holds"]


def test_model_policy_e38_is_wired_and_retains_live_holds(tmp_path):
    assert cases.BY_ID["E38"].owner == "#5636"
    assert "E38" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E38"] in bundle.purposes()
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.side_effect = [
        {
            "status": "ok",
            "detail": {"tenant_id": "org", "persona_key": "architect", "models": []},
        },
        {
            "status": "ok",
            "detail": {
                "tenant_id": "org",
                "selected_persona": "architect",
                "entries": [],
                "status": "unknown",
                "aggregate_scope": "all_personas_for_selected_owner_and_chain",
            },
        },
    ]
    evidence = {}
    module.model_policy(cli, evidence)
    assert "posture-rollback" in evidence["live_holds"]
    cli.run.assert_not_called()


def test_knowledge_nightly_is_selected_and_has_no_dispatch(tmp_path):
    assert cases.BY_ID["E32"].owner == "#5632"
    assert "E32" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E32"] in bundle.purposes()
    module, _ = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.run.side_effect = [
        (0, {"status": "ok", "detail": {"items": []}}),
        (1, {"status": "failed", "error": {"code": "usage_error"}}),
    ]
    cli.json.return_value = {
        "status": "preview",
        "detail": {"effect": "soft removal; index artifacts retained"},
    }
    cli.binary = str(tmp_path / "adp")
    evidence = {}
    module.knowledge(cli, evidence)
    assert evidence["live_acceptance"].startswith("held:")
    assert evidence["discovery"] == "available"
    assert all(
        not {"--yes", "reindex", "add", "commit", "submit"}.intersection(call.args[0])
        for call in cli.method_calls
    )
    # Issue #6437: the observed exit/error pair is retained on success too, so a
    # later run has a baseline to compare a regression against.
    observed = evidence["invalid_status_target"]
    assert observed["exit_code"] == 1 and observed["error_code"] == "usage_error"
    assert observed["envelope"] == "present"
    assert observed["invocation"].endswith("knowledge status not-a-uuid --json")


@pytest.mark.parametrize(
    ("code", "envelope", "expected"),
    [
        # Issue #6437: the reproduced cause — the dispatcher's pre-dispatch tenant
        # resolution failed and returned no envelope at all. The case must still
        # fail, and must now say what it saw instead of only naming the refusal.
        (5, None, "exit 5, error None, envelope absent"),
        (
            2,
            {"status": "failed", "error": {"code": "authentication_required"}},
            "exit 2, error 'authentication_required', envelope present",
        ),
        # A genuine regression: the target reached the gateway instead of being
        # refused locally.
        (0, {"status": "ok", "detail": {}}, "exit 0, error None, envelope present"),
    ],
)
def test_knowledge_nightly_retains_actual_invalid_target_evidence(
    tmp_path, code, envelope, expected
):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.binary = str(tmp_path / "adp")
    cli.run.side_effect = [
        (0, {"status": "ok", "detail": {"items": []}}),
        (code, envelope),
    ]
    cli.json.return_value = {
        "status": "preview",
        "detail": {"effect": "soft removal; index artifacts retained"},
    }
    evidence = {}
    with pytest.raises(common.RemoteError) as failure:
        module.knowledge(cli, evidence)
    assert "Invalid knowledge target was not refused locally" in str(failure.value)
    assert expected in str(failure.value)
    # Retained on the evidence too, not only in the message.
    assert evidence["invalid_status_target"]["exit_code"] == code


@pytest.mark.parametrize(
    "invalid",
    [
        [{"error": {"code": "usage_error"}}],
        {"status": "failed", "error": "not an object"},
    ],
)
def test_knowledge_nightly_retains_malformed_response_before_assertion(
    tmp_path, invalid
):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.binary = str(tmp_path / "adp")
    cli.run.side_effect = [
        (0, {"status": "ok", "detail": {"items": []}}),
        (1, invalid),
    ]
    cli.json.return_value = {
        "status": "preview",
        "detail": {"effect": "artifacts retained"},
    }
    evidence = {}
    with pytest.raises(
        common.RemoteError, match="Invalid knowledge target was not refused locally"
    ):
        module.knowledge(cli, evidence)
    assert evidence["invalid_status_target"]["exit_code"] == 1
    assert evidence["invalid_status_target"]["error_code"] is None
    assert evidence["invalid_status_target"]["invocation_path"] == str(tmp_path / "adp")
    assert evidence["invalid_status_target"]["envelope"] == (
        "present" if isinstance(invalid, dict) else "invalid:list"
    )


@pytest.mark.parametrize(
    "output",
    [
        '[{"error":{"code":"usage_error"}}]',
        '{"status":"failed","command":"knowledge status","error":{"code":"usage_error","message":"token=private-password"}}',
    ],
)
def test_cli_evidence_preserves_bounded_redacted_stdout_and_stderr(
    tmp_path, monkeypatch, output
):
    _, common = shipped_script(tmp_path, "story_reads")
    monkeypatch.setattr(
        common,
        "bounded",
        lambda *args, **kwargs: (1, output, "token=private-password HTTP 429"),
    )
    cli = common.Cli(tmp_path / "installed" / "adp", {}, [])
    diagnostics = {}
    code, payload = cli.run(
        ["knowledge", "status", "not-a-uuid"], expected=None, diagnostics=diagnostics
    )
    assert code == 1
    assert isinstance(payload, list) == output.startswith("[")
    assert diagnostics["stderr"]["http_status"] == ["429"]
    assert diagnostics["stderr"]["bytes"] > 0
    if isinstance(payload, dict):
        assert diagnostics["stdout_error_envelope"]["error"]["code"] == "usage_error"
        assert diagnostics["stdout_error_envelope"]["error"]["message"]["bytes"] > 0
    else:
        assert diagnostics["stdout_error_envelope"] == {"shape": "list"}
    assert "private-password" not in str(diagnostics)


def test_knowledge_e32_failure_retains_installed_diagnostics(tmp_path, monkeypatch):
    module, common = shipped_script(tmp_path, "story_reads")
    installed = tmp_path / "installed"
    installed.mkdir()
    for name in ("adp", "adp-tenant.py", "adp-knowledge.py"):
        (installed / name).write_text(name)
    symlink = tmp_path / "adp"
    symlink.symlink_to(installed / "adp")
    responses = iter(
        [
            (0, json.dumps({"status": "ok", "detail": {"items": []}}), ""),
            (
                0,
                json.dumps(
                    {"status": "preview", "detail": {"effect": "artifacts retained"}}
                ),
                "",
            ),
            (
                5,
                json.dumps(
                    {
                        "status": "failed",
                        "command": "adp tenant",
                        "error": {
                            "code": "too_many_requests",
                            "message": "HTTP 429 token=private-password",
                        },
                    }
                ),
                "token=private-password HTTP 429",
            ),
        ]
    )
    monkeypatch.setattr(common, "bounded", lambda *args, **kwargs: next(responses))
    evidence = {}
    cli = common.Cli(symlink, {}, [])
    with pytest.raises(common.RemoteError, match="exit 5, error 'too_many_requests'"):
        module.knowledge(cli, evidence)
    observed = evidence["invalid_status_target"]
    assert observed["invocation_path"] == str(symlink)
    assert observed["resolved_dispatcher_path"] == str(installed / "adp")
    assert (
        observed["served_helpers"]["adp-knowledge.py"]
        == hashlib.sha256(b"adp-knowledge.py").hexdigest()
    )
    assert observed["stdout_error_envelope"]["error"]["code"] == "too_many_requests"
    assert observed["stderr"]["http_status"] == ["429"]
    assert observed["stdout"]["bytes"] > 0
    assert "private-password" not in json.dumps(evidence)


@pytest.mark.parametrize(
    ("exit_code", "last_stdout", "last_stderr", "error_code", "failure_kind"),
    [
        (
            5,
            "",
            "bash: adp-tenant.py: command not found token=private-value",
            None,
            "command_not_found",
        ),
        (
            5,
            json.dumps(
                {
                    "status": "failed",
                    "command": "adp tenant",
                    "error": {
                        "code": "tenant_identity_changed",
                        "message": "token=private-value",
                    },
                }
            ),
            "PermissionError: token=private-value",
            "tenant_identity_changed",
            "permission_denied",
        ),
        (
            5,
            json.dumps(
                {
                    "status": "failed",
                    "command": "adp tenant",
                    "error": {
                        "code": "invalid_response",
                        "message": "Malformed membership response: token=private-value",
                    },
                }
            ),
            "PermissionError: token=private-value",
            "invalid_response",
            "permission_denied",
        ),
        (
            4,
            json.dumps(
                {
                    "status": "failed",
                    "command": "adp tenant",
                    "error": {
                        "code": "tenant_selection_required",
                        "message": "Select a tenant: token=private-value",
                    },
                }
            ),
            "PermissionError: token=private-value",
            "tenant_selection_required",
            "permission_denied",
        ),
    ],
)
def test_e32_failure_diagnostics_survive_emission_and_report(
    tmp_path,
    monkeypatch,
    capsys,
    exit_code,
    last_stdout,
    last_stderr,
    error_code,
    failure_kind,
):
    module, common = shipped_script(tmp_path, "story_reads")
    installed = tmp_path / "installed"
    installed.mkdir()
    for name in ("adp", "adp-tenant.py", "adp-knowledge.py"):
        (installed / name).write_text(name)
    binary = tmp_path / "adp"
    binary.symlink_to(installed / "adp")
    monkeypatch.setattr(common, "assert_owned_instance", lambda config: None)
    monkeypatch.setattr(common, "load_session", lambda config: {"org_id": "native"})
    monkeypatch.setattr(module, "_write_session", lambda *args: None)
    responses = iter(
        [
            (0, json.dumps({"status": "ok", "detail": {"items": []}}), ""),
            (
                0,
                json.dumps(
                    {"status": "preview", "detail": {"effect": "artifacts retained"}}
                ),
                "",
            ),
            (exit_code, last_stdout, last_stderr),
        ]
    )
    monkeypatch.setattr(common, "bounded", lambda *args, **kwargs: next(responses))
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "cli_path": str(binary),
                "mode": "knowledge",
                "org_id": "native",
                "region": "us-east-1",
                "sts_endpoint": "https://sts.example.test",
                "gateway_url": "https://gateway.example.test",
            }
        )
    )
    assert common.run_script(module.execute, [str(payload)]) == 1
    emitted = json.loads(capsys.readouterr().out)
    matrix = cases.new_matrix(("nightly",))
    context = {
        "document": {"instance_id": "i-owned"},
        "matrix": matrix,
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
        "transcript": [],
        "manifest": Mock(),
        "correlation": {},
        "fault": None,
    }
    stages.journeys_stage(
        {"region": "us-east-1"},
        {"journey": lambda purpose: lambda instance, context: emitted},
    )(context)
    evaluation_id = "adp-e2e-20260928-120000-abcdef"
    document = report.build(
        matrix=matrix,
        suites=("nightly",),
        config=config.validate(config_fixture()),
        evaluation_id=evaluation_id,
        attempt_id=evaluation_id + "-a1",
        cleanup_ok=False,
        timing={
            "started_at": "2026-09-28T12:00:00Z",
            "ended_at": "2026-09-28T12:00:01Z",
            "duration_seconds": 1,
        },
        correlation={},
    )
    artifact = report.write(tmp_path / "artifact", document, matrix, evaluation_id)
    published = json.loads(Path(artifact["report"]).read_text())
    row = next(row for row in published["cases"] if row["id"] == "E32")
    observed = row["detail"]["invalid_status_target"]
    assert row["status"] == cases.FAILED
    assert published["status"] != cases.PASSED
    assert observed["exit_code"] == exit_code
    assert observed["error_code"] == error_code
    if error_code is None:
        assert observed["envelope"] == "absent"
        assert observed["stdout_error_envelope"] == {"shape": "NoneType"}
    else:
        assert observed["stdout_error_envelope"]["error"]["code"] == error_code
    assert observed["stderr"]["failure_kinds"] == [failure_kind]
    assert observed["invocation"].endswith("knowledge status not-a-uuid --json")
    assert observed["invocation_path"] == str(binary)
    assert observed["resolved_dispatcher_path"] == str(installed / "adp")
    for name in ("adp", "adp-tenant.py", "adp-knowledge.py"):
        assert (
            observed["served_helpers"][name]
            == hashlib.sha256(name.encode()).hexdigest()
        )
    assert "private-value" not in Path(artifact["report"]).read_text()
    assert "private-value" not in Path(artifact["junit"]).read_text()


def test_cli_error_codes_are_bounded_machine_fields_not_free_text(tmp_path):
    _, common = shipped_script(tmp_path, "story_reads")
    for unsafe in (
        "my_secret",
        "arbitrary_code",
        "tenant-identity-changed",
        "long" * 30,
    ):
        assert (
            common.safe_error_envelope({"error": {"code": unsafe}})["error"]["code"]
            == "<redacted>"
        )
    for code in (
        "tenant_identity_changed",
        "invalid_response",
        "tenant_selection_required",
    ):
        assert (
            common.safe_error_envelope({"error": {"code": code}})["error"]["code"]
            == code
        )


@pytest.mark.parametrize("status", [401, 429, 500])
def test_knowledge_nightly_does_not_hide_unexpected_errors(tmp_path, status):
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.run.return_value = (
        5,
        {"status": "failed", "error": {"message": f"HTTP {status}"}},
    )
    with pytest.raises(common.RemoteError, match="Unexpected knowledge discovery"):
        module.knowledge(cli, {})


def test_recovery_nightly_reads_and_refuses_without_mutation(tmp_path):
    module, _ = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.return_value = {"status": "ok", "detail": {"flows": []}}
    cli.run.return_value = (1, {"status": "failed"})
    evidence = {}
    module.recovery(cli, evidence)
    assert evidence["malformed_target_refused"] is True
    assert "live_acceptance_hold" in evidence
    assert all("--yes" not in call.args[0] for call in cli.method_calls)
    assert cases.BY_ID["E37"].owner == "#5630"
    assert stages.JOURNEY_DRIVERS["E37"] in bundle.purposes()


def test_platform_e41_is_read_only_and_retains_live_holds(tmp_path):
    assert cases.BY_ID["E41"].owner == "#5641"
    assert "E41" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E41"] in bundle.purposes()
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.return_value = {
        "status": "ok",
        "detail": {
            "full_deployment_verified": False,
            "environment_verified": False,
            "artifact_verification": "unknown",
            "components": dict.fromkeys(
                ["gateway", "factory", "webhook", "models", "github_wiring"], {}
            ),
        },
    }
    evidence = {}
    module.platform(cli, evidence)
    assert "authorized-teardown-cleanup" in evidence["live_holds"]
    assert cli.json.call_args.args[0] == ["platform", "status", "--environment", "dev"]
    cli.run.assert_not_called()


def test_superplane_lifecycle_story_is_bounded_preview(tmp_path):
    assert cases.BY_ID["E39"].owner == "#5638"
    assert cases.SUPERPLANE_DOMAIN in cases.BY_ID["E39"].requires
    assert "E39" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert stages.JOURNEY_DRIVERS["E39"] in bundle.purposes()
    module, common = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.side_effect = [
        {"status": "ok", "detail": {"workspaces": [{"id": "workspace"}]}},
        {
            "status": "dry_run",
            "detail": {
                "before": {"workspace_id": "workspace", "billing_state": "unconfirmed"}
            },
        },
        {"status": "ok", "detail": {"workspace_id": "workspace", "events": []}},
    ]
    evidence = {}
    module.superplane_lifecycle(cli, evidence)
    assert evidence["mutations"] == 0
    assert cli.json.call_args_list[1].args[0][-1] == "--dry-run"
    cli.run.assert_not_called()


def test_chat_read_scenario_is_nightly_and_read_only(tmp_path):
    module, _ = shipped_script(tmp_path, "story_reads")
    cli = Mock()
    cli.json.side_effect = [
        {
            "status": "ok",
            "detail": {
                "tenant_id": "tenant",
                "user_id": "user",
                "enabled": True,
                "history_configured": True,
                "history_ready": "unknown",
                "general_turns_supported": False,
                "authorized_personas": [],
            },
        },
        {
            "status": "ok",
            "detail": {"tenant_id": "tenant", "user_id": "user", "items": []},
        },
    ]
    module.chat(cli, {})
    assert cli.json.call_args_list[1].args[0] == ["chat", "list", "--page-size", "1"]
    assert stages.JOURNEY_DRIVERS["E40"] in bundle.purposes()


def test_coding_nightly_fixture_is_explicit_and_defaults_blocked(tmp_path):
    module, _ = shipped_script(tmp_path, "hosted_coding")
    assert not module.fixture_valid({})
    fixture = dict(
        enrollment_verified=True,
        shared_budget_authorized=True,
        max_dispatches=1,
        max_task_usd=1,
        scenario="cancel",
        persona="agent-task-codex-developer",
        snapshot={"repository": "owner/repo"},
        instructions="bounded issue",
    )
    assert module.fixture_valid(fixture)
    for changes in (
        {"max_dispatches": 2},
        {"max_task_usd": 1.01},
        {"enrollment_verified": False},
        {"shared_budget_authorized": False},
        {"instructions": "x" * 4097},
        {"instructions": "eyJabcdefgh.abcdefgh.abcdefgh"},
    ):
        assert not module.fixture_valid({**fixture, **changes})
    assert "E42" in {case.id for case in cases.resolve_suites(("nightly",))}
    assert cases.HUMAN_TASK_CODING not in config.fixture_classes(VALID)


def test_coding_lost_cli_receipt_recovers_owned_task_and_cleans_up(
    tmp_path, monkeypatch
):
    import json

    module, remote_common = shipped_script(tmp_path, "hosted_coding")
    task_id = "tsk_12345678-1234-4123-8123-123456789abc"
    calls = []
    work = tmp_path / "run"
    work.mkdir()

    class Cli:
        def __init__(self, executable, env, transcript, **kwargs):
            self.home = Path(env["HOME"])

        def json(self, argv, **kwargs):
            if argv == ["models", "mappings", "list"]:
                return {
                    "detail": {
                        "tenant_id": "fixture-tenant",
                        "principal_id": "fixture-human",
                    }
                }
            if "--dry-run" in argv:
                return {"status": "dry_run"}
            journal = self.home / ".adp/state/hosted-tasks"
            journal.mkdir(parents=True)
            (journal / "receipt.json").write_text(
                json.dumps(
                    {
                        "artifact_id": "art-fixture",
                        "task_id": task_id,
                        "fingerprint": "exact",
                    }
                )
            )
            raise RuntimeError("CLI stdout lost after durable acceptance")

        def run(self, argv, **kwargs):
            calls.append(argv)
            return 0, {
                "detail": {"status": "cancelled" if "wait" in argv else "accepted"}
            }

    monkeypatch.setattr(remote_common, "Cli", Cli)
    monkeypatch.setattr(
        remote_common,
        "clean_env",
        lambda cfg, **kwargs: {key: str(value) for key, value in kwargs.items()},
    )
    monkeypatch.setattr(remote_common, "session_tokens", lambda cfg: {})
    monkeypatch.setattr(module, "_write_session", lambda *args: None)
    fixture = dict(
        enrollment_verified=True,
        shared_budget_authorized=True,
        max_dispatches=1,
        max_task_usd=1,
        scenario="cancel",
        persona="agent-task-codex-developer",
        snapshot={"repository": "owner/repo", "issue": 42},
        instructions="bounded edit",
    )
    evidence = {"transcript": []}
    with pytest.raises(RuntimeError, match="stdout lost"):
        module.execute(
            coding_remote_config(module, fixture, work),
            evidence,
        )
    assert evidence["task_id"] == task_id
    assert evidence["acceptance_reconciled"] is True
    assert evidence["cleanup_status"] == "cancelled"
    assert [argv[1] for argv in calls] == ["abort", "wait"]
    recovery = Path(evidence["recovery_path"])
    assert recovery.exists() and recovery.stat().st_mode & 0o777 == 0o600
    record = json.loads(recovery.read_text())
    assert (
        record["task_id"] == task_id
        and record["journal"]["artifact_id"] == "art-fixture"
    )
    assert record["phase"] == "cancelled"
    assert "access_token" not in record


@pytest.mark.parametrize("namespace", ["", "tenants/" + "a" * 24 + "/"])
def test_coding_acceptance_replay_keeps_same_key_and_retains_unknown(
    tmp_path, namespace
):
    import json

    module, _ = shipped_script(tmp_path, "hosted_coding")
    journal = tmp_path / (".adp/state/" + namespace + "hosted-tasks")
    journal.mkdir(parents=True)
    (journal / "receipt.json").write_text(
        json.dumps({"artifact_id": "art-fixture", "fingerprint": "exact"})
    )
    task_id = "tsk_12345678-1234-4123-8123-123456789abc"
    trigger = ["agent", "trigger", "--request-id", "stable-request"]
    calls = []

    class Cli:
        def run(self, argv, **kwargs):
            calls.append(argv)
            return 4, {"detail": {"task_id": task_id}}

    assert module.reconcile_acceptance(Cli(), trigger, tmp_path) == task_id
    assert calls == [[*trigger, "--yes"]]

    class Broken:
        def run(self, argv, **kwargs):
            raise RuntimeError("still unavailable")

    assert module.reconcile_acceptance(Broken(), trigger, tmp_path) is None
    assert module.local_receipt(tmp_path)["artifact_id"] == "art-fixture"


@pytest.mark.parametrize("journal_mode", ["legacy", "tenant", "missing"])
def test_coding_unknown_acceptance_survives_worker_cleanup_in_report(
    tmp_path, monkeypatch, capsys, journal_mode
):
    import shutil

    module, remote_common = shipped_script(tmp_path, "hosted_coding")
    work = tmp_path / "worker-run"
    work.mkdir()
    calls = []

    class Cli:
        def __init__(self, executable, env, transcript, **kwargs):
            self.home = Path(env["HOME"])

        def json(self, argv, **kwargs):
            if argv == ["models", "mappings", "list"]:
                return {
                    "detail": {
                        "tenant_id": "fixture-tenant",
                        "principal_id": "fixture-human",
                    }
                }
            if "--dry-run" in argv:
                return {"status": "dry_run"}
            if journal_mode != "missing":
                suffix = "tenants/" + "b" * 24 + "/" if journal_mode == "tenant" else ""
                journal = self.home / (".adp/state/" + suffix + "hosted-tasks")
                journal.mkdir(parents=True)
                (journal / "receipt.json").write_text(
                    json.dumps({"artifact_id": "art-fixture", "fingerprint": "exact"})
                )
            return {
                "status": "failed",
                "error": {
                    "code": "task_http_error",
                    "http_status": 422,
                    "message": "private-response-must-not-escape",
                },
            }

        def run(self, argv, **kwargs):
            calls.append(argv)
            return 5, None

    monkeypatch.setattr(remote_common, "Cli", Cli)
    monkeypatch.setattr(
        remote_common,
        "clean_env",
        lambda cfg, **kwargs: {key: str(value) for key, value in kwargs.items()},
    )
    monkeypatch.setattr(
        remote_common,
        "session_tokens",
        lambda cfg: {"access_token": "session-must-not-escape"},
    )
    monkeypatch.setattr(module, "_write_session", lambda *args: None)
    fixture = dict(
        enrollment_verified=True,
        shared_budget_authorized=True,
        max_dispatches=1,
        max_task_usd=1,
        scenario="cancel",
        persona="agent-task-codex-developer",
        snapshot={"repository": "owner/repo", "issue": 42},
        instructions="Fix exactly this issue.\nPreserve this input.",
    )
    evidence = {"success": False, "transcript": []}
    with pytest.raises(remote_common.RemoteError, match="acceptance is unknown"):
        module.execute(
            coding_remote_config(module, fixture, work),
            evidence,
        )
    remote_common.emit(evidence)
    emitted = capsys.readouterr().out
    assert (
        len(emitted.encode()) < 24000
    )  # SSM inline output limit; no full repository snapshot.
    assert "session-must-not-escape" not in emitted
    assert "private-response-must-not-escape" not in emitted
    assert evidence["detail"]["trigger_outcome"] == {
        "status": "failed",
        "code": "task_http_error",
        "http_status": 422,
    }
    document = json.loads(emitted)
    matrix = {"E42": {"status": cases.NOT_RUN}}
    ctx = {
        "document": {"instance_id": "i-fixture"},
        "matrix": matrix,
        "transcript": [],
        "correlation": {},
        "fault": "none",
        "record": lambda case_id, status, detail: cases.record(
            matrix, case_id, status, detail
        ),
    }
    stages.journeys_stage(
        {}, {"journey": lambda purpose: lambda instance, ctx: document}
    )(ctx)
    assert matrix["E42"]["status"] == cases.FAILED
    paths = report.write(
        tmp_path / "published",
        published_report(matrix=matrix),
        matrix,
        "adp-e2e-20260915-143022-a1b2c3",
    )
    shutil.rmtree(work)  # Model the disposable EC2 instance disappearing.
    retained = json.loads(Path(paths["report"]).read_text())["cases"][0]["detail"][
        "recovery"
    ]
    assert retained["phase"] == "acceptance_unknown"
    assert retained["task_id"] is None
    assert retained["gateway"] == "https://gateway"
    if journal_mode == "missing":
        assert retained["submit_body"] is None
        assert retained["journal"] == {}
        assert calls == []
        return
    assert retained["submit_body"] == {
        "schema_version": "1.0",
        "persona": fixture["persona"],
        "instructions": fixture["instructions"],
        "inputs": {"repository_snapshot_artifact": "art-fixture"},
        "artifact_ids": ["art-fixture"],
        "external_reference": "owner/repo#42",
    }
    assert retained["request_id"] == evidence["request_id"]
    assert calls[0][calls[0].index("--request-id") + 1] == retained["request_id"]


def test_coding_marks_success_only_after_execute_returns_and_exports_details(
    tmp_path, monkeypatch
):
    module, _ = shipped_script(tmp_path, "hosted_coding")
    monkeypatch.setattr(
        module,
        "_execute",
        lambda cfg, evidence: evidence.update(
            task_id="tsk-fixture", terminal_status="completed"
        ),
    )
    evidence = {"success": False}
    module.execute({}, evidence)
    assert evidence["success"] is True
    assert evidence["detail"]["task_id"] == "tsk-fixture"
    assert evidence["detail"]["terminal_status"] == "completed"


@pytest.mark.parametrize("outcome", ["absent", "owned", "foreign", "denied"])
def test_pending_launch_cleanup_discovers_only_exact_attempt(outcome):
    attempt = "adp-e2e-20260926-012131-df70e8-a1"
    owner = attempt.rsplit("-a", 1)[0]

    def describe(**kwargs):
        if "InstanceIds" in kwargs:
            assert kwargs["InstanceIds"] == ["i-0abc"]
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": "i-0abc", "State": {"Name": "terminated"}}
                        ]
                    }
                ]
            }
        assert kwargs["Filters"] == [
            {"Name": "tag:" + cleanup.OWNER_TAG, "Values": [owner]},
            {"Name": "tag:Name", "Values": ["cli-uplift-eval-" + attempt]},
        ]
        if outcome == "denied":
            raise ports.PortError(
                "ec2.describe_instances failed: UnauthorizedOperation"
            )
        if outcome == "absent":
            return {"Reservations": []}
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0abc",
                            "Tags": [
                                {
                                    "Key": cleanup.OWNER_TAG,
                                    "Value": owner
                                    if outcome == "owned"
                                    else "another-evaluation",
                                },
                                {"Key": "Name", "Value": "cli-uplift-eval-" + attempt},
                            ],
                        }
                    ]
                }
            ]
        }

    aws = FakeAws({"ec2.describe_instances": describe, "ec2.terminate_instances": {}})
    delete = live._deleters(aws, VALID)(VALID)["ec2_instance"]
    if outcome in {"foreign", "denied"}:
        with pytest.raises(ports.PortError):
            delete("pending:" + attempt)
    else:
        delete("pending:" + attempt)
    mutations = [
        kw for service, operation, kw in aws.calls if operation == "terminate_instances"
    ]
    assert mutations == ([{"InstanceIds": ["i-0abc"]}] if outcome == "owned" else [])


@pytest.mark.parametrize("foreign_last_page", [False, True])
def test_pending_launch_cleanup_finishes_discovery_before_any_termination(
    foreign_last_page,
):
    attempt = "adp-e2e-20260926-012131-df70e8-a1"
    owner = attempt.rsplit("-a", 1)[0]
    discovered = []

    def describe(**kwargs):
        if "InstanceIds" in kwargs:
            return {
                "Reservations": [{"Instances": [{"State": {"Name": "terminated"}}]}]
            }
        page = 2 if kwargs.get("NextToken") == "page-two" else 1
        discovered.append(page)
        instance = {
            "InstanceId": "i-0ab" + str(page),
            "Tags": [
                {
                    "Key": cleanup.OWNER_TAG,
                    "Value": "foreign" if page == 2 and foreign_last_page else owner,
                },
                {"Key": "Name", "Value": "cli-uplift-eval-" + attempt},
            ],
        }
        return {
            "Reservations": [{"Instances": [instance]}],
            **({"NextToken": "page-two"} if page == 1 else {}),
        }

    def terminate(**kwargs):
        assert discovered == [1, 2], "Never delete before complete discovery"
        return {}

    aws = FakeAws(
        {"ec2.describe_instances": describe, "ec2.terminate_instances": terminate}
    )
    delete = live._deleters(aws, VALID)(VALID)["ec2_instance"]
    if foreign_last_page:
        with pytest.raises(ports.PortError, match="foreign identity"):
            delete("pending:" + attempt)
    else:
        delete("pending:" + attempt)
    terminated = [
        kwargs["InstanceIds"]
        for _, operation, kwargs in aws.calls
        if operation == "terminate_instances"
    ]
    assert terminated == ([] if foreign_last_page else [["i-0ab1"], ["i-0ab2"]])


@pytest.mark.parametrize("fails", [False, True])
def test_story_read_script_reports_real_completion(
    tmp_path, monkeypatch, capsys, fails
):
    script, common = shipped_script(tmp_path, "story_reads")
    payload = tmp_path / "read.json"
    payload.write_text(
        json.dumps(
            {
                "mode": "capabilities",
                "org_id": "native-tenant",
                "cli_path": "/served/adp",
                "gateway_url": "https://adp.example",
                "region": "us-east-1",
                "sts_endpoint": "https://sts.us-east-1.amazonaws.com",
            }
        )
    )
    monkeypatch.setattr(common, "assert_owned_instance", lambda _: None)
    monkeypatch.setattr(common, "load_session", lambda _: {"org_id": "native-tenant"})
    monkeypatch.setattr(script, "_write_session", lambda *args: None)
    monkeypatch.setattr(common, "Cli", lambda *args, **kwargs: object())

    def scenario(cli, evidence):
        if fails:
            raise common.RemoteError("actual CLI assertion failed")
        evidence["operation_count"] = 65

    monkeypatch.setitem(script.SCENARIOS, "capabilities", scenario)
    code = common.run_script(script.execute, [str(payload)])
    emitted = json.loads(capsys.readouterr().out)
    assert code == int(fails)
    assert emitted["success"] is not fails
    if not fails:
        assert emitted["stage"] == "complete"
        assert emitted["detail"]["operation_count"] == 65
        assert "not full story acceptance" in emitted["detail"]["qualification"]
    else:
        assert emitted["error"] == "actual CLI assertion failed"


def test_remote_exception_after_success_cannot_emit_green(
    tmp_path, monkeypatch, capsys
):
    _, common = shipped_script(tmp_path, "story_reads")
    payload = tmp_path / "failure.json"
    payload.write_text("{}")
    monkeypatch.setattr(common, "assert_owned_instance", lambda _: None)

    def execute(config, evidence):
        evidence["success"] = True
        raise common.RemoteError("cleanup failed")

    assert common.run_script(execute, [str(payload)]) == 1
    assert json.loads(capsys.readouterr().out)["success"] is False


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_tenant_script_completion_requires_session_preservation(
    tmp_path, monkeypatch, capsys, cleanup_fails
):
    script, common = shipped_script(tmp_path, "tenant_isolation")
    payload = tmp_path / "tenant.json"
    payload.write_text(
        json.dumps(
            {
                "mode": "smoke",
                "cli_path": "/served/adp",
                "gateway_url": "https://adp.example",
                "region": "us-east-1",
                "sts_endpoint": "https://sts.us-east-1.amazonaws.com",
                "session_ref": str(tmp_path / "session.json"),
            }
        )
    )
    monkeypatch.setattr(common, "assert_owned_instance", lambda _: None)
    monkeypatch.setattr(common, "load_session", lambda _: {})
    monkeypatch.setattr(common, "session_tokens", lambda _: {})

    def write_session(home, *args):
        target = home / ".bedrock-gateway"
        target.mkdir()
        (target / "tokens.json").write_text("{}")

    monkeypatch.setattr(script, "_write_session", write_session)
    monkeypatch.setattr(common, "Cli", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        script, "smoke", lambda cli, evidence: evidence.update(tenant_count=2)
    )

    def save(*args, **kwargs):
        if cleanup_fails:
            raise common.RemoteError("session preservation failed")

    monkeypatch.setattr(common, "save_session", save)
    assert common.run_script(script.execute, [str(payload)]) == int(cleanup_fails)
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["success"] is not cleanup_fails
    if not cleanup_fails:
        assert emitted["detail"]["tenant_count"] == 2


def test_recovery_negative_scenario_accepts_actual_cli_nonzero_exit(
    tmp_path, monkeypatch
):
    script, common = shipped_script(tmp_path, "story_reads")
    replies = iter(
        [
            (0, json.dumps({"status": "ok", "detail": {"flows": []}}), ""),
            (1, json.dumps({"status": "failed", "error": {"code": "usage_error"}}), ""),
        ]
    )
    monkeypatch.setattr(common, "bounded", lambda *args, **kwargs: next(replies))
    evidence = {}
    cli = common.Cli("adp", {}, [])
    script.recovery(cli, evidence)
    assert evidence["malformed_target_refused"] is True


@pytest.mark.parametrize(
    "fault",
    [None, "lost_first_receipt", "unknown_second", "wrong_fixture", "watch_pending"],
)
def test_shipped_hosted_chat_two_turns_and_durable_unknown(
    tmp_path, monkeypatch, fault
):
    module, remote_common = shipped_script(tmp_path, "hosted_chat")
    work = tmp_path / "worker"
    work.mkdir()
    requests = {}
    attempts = []
    sid = "chat-fixture"
    marker = [None]
    all_messages = []
    cleanup_calls = []

    class Cli:
        def __init__(self, binary, env, transcript, **kwargs):
            assert env["BG_CONFIG_DIR"].startswith(env["HOME"])
            assert env["ADP_TENANT"] == "fixture-tenant"

        def json(self, argv, **kwargs):
            if argv[:2] == ["chat", "status"]:
                return {
                    "detail": {
                        "tenant_id": "fixture-tenant",
                        "user_id": "other"
                        if fault == "wrong_fixture"
                        else "fixture-user",
                        "general_turns_supported": True,
                        "authorized_personas": ["agent-task-investigator"],
                    }
                }
            if "--dry-run" in argv:
                return {
                    "status": "dry_run",
                    "detail": {"session_id": sid, "dispatched": False},
                }
            if argv[:2] == ["chat", "watch"]:
                if fault == "watch_pending":
                    raise remote_common.RemoteError(
                        "Task did not finish before timeout"
                    )
                task_id = argv[argv.index("--task-id") + 1]
                return {
                    "detail": {
                        "session_id": sid,
                        "matched_task_id": task_id,
                        "answer_completion_verified": True,
                        "messages": list(all_messages),
                    }
                }
            if argv[:2] == ["chat", "show"]:
                return {"detail": {"session_id": sid, "messages": list(all_messages)}}
            raise AssertionError(argv)

        def run(self, argv, **kwargs):
            if argv[0] == "agent":
                cleanup_calls.append(argv)
                return 0, {"detail": {"status": "cancelled"}}
            if argv[:2] == ["chat", "show"]:
                return 0, {
                    "detail": {"session_id": sid, "messages": list(all_messages)}
                }
            assert argv[:2] in (["chat", "start"], ["chat", "resume"])
            request_id = argv[argv.index("--request-id") + 1]
            attempts.append(request_id)
            if fault == "unknown_second" and argv[1] == "resume":
                return 4, {"detail": {"request_id": request_id, "outcome": "unknown"}}
            if request_id not in requests:
                index = len(requests)
                task_id = f"tsk_12345678-1234-4123-8123-{index:012d}"
                flag = "--message-file" if index == 0 else "--answer-file"
                content = Path(argv[argv.index(flag) + 1]).read_text()
                if index == 0:
                    marker[0] = re.search(r"memory-[0-9a-f]+", content).group()
                else:
                    assert (
                        marker[0] not in content
                    )  # Recall must come from earlier context.
                all_messages.extend(
                    [
                        {"role": "user", "content": content, "task_id": task_id},
                        {
                            "role": "assistant",
                            "content": "NOTED" if index == 0 else marker[0],
                            "task_id": task_id,
                        },
                    ]
                )
                requests[request_id] = {
                    "request_id": request_id,
                    "session_id": sid,
                    "task_id": task_id,
                }
                if fault == "lost_first_receipt" and index == 0:
                    return 4, None
            return 4, {"detail": requests[request_id]}

    monkeypatch.setattr(remote_common, "Cli", Cli)
    monkeypatch.setattr(
        remote_common,
        "clean_env",
        lambda cfg, **kwargs: {key: str(value) for key, value in kwargs.items()},
    )
    monkeypatch.setattr(
        remote_common, "session_tokens", lambda cfg: {"access_token": "must-not-escape"}
    )
    monkeypatch.setattr(module, "_write_session", lambda *args: None)
    cfg = {
        "gateway_url": "https://gateway",
        "cli_path": "/installed/adp",
        "work_dir": str(work),
        "test_user_id": "fixture-login",
        "evaluation_id": "stable-chat-evaluation",
        "human_task_chat": {
            "enrollment_verified": True,
            "shared_budget_authorized": True,
            "max_tasks": 2,
            "max_task_usd": 0.25,
            "login_user_id": "fixture-login",
            "canonical_user_id": "fixture-user",
            "tenant_id": "fixture-tenant",
        },
    }
    cfg["recovery_plan"] = module.recovery_plan(cfg)
    evidence = {"success": False, "transcript": []}
    if fault in {"unknown_second", "wrong_fixture", "watch_pending"}:
        with pytest.raises(remote_common.RemoteError):
            module.execute(cfg, evidence)
        assert evidence["success"] is False
        if fault == "wrong_fixture":
            assert not attempts
            return
        if fault == "watch_pending":
            assert len(requests) == 1 and len(evidence["detail"]["turns"]) == 1
            assert [args[1] for args in cleanup_calls] == ["abort", "wait"]
            assert all(
                args[args.index("--run") + 1]
                == evidence["detail"]["turns"][0]["task_id"]
                for args in cleanup_calls
            )
            assert evidence["detail"]["turns"][0]["phase"] == "cancelled"
            return
        record = evidence["detail"]["turns"][1]
        assert record["phase"] == "acceptance_unknown"
        assert record["endpoint"] == "/chat/sessions/chat-fixture/turns"
        assert record["body"]["request_id"] == record["request_id"]
        assert record["body"]["message"].startswith("What label")
        assert len(set(attempts)) == 2  # Never start a replacement turn.
        import shutil

        shutil.rmtree(work)
        retained = json.loads(json.dumps(remote_common.redact(evidence)))
        assert retained["detail"]["turns"][1] == record
    else:
        module.execute(cfg, evidence)
        assert evidence["success"] is True
        assert evidence["detail"]["context_recalled"] is True
        assert len(requests) == 2 and len(set(attempts)) == 2
        assert attempts[0].startswith("chatdiag-") and attempts[0].endswith("-0")
        assert all(row["phase"] == "completed" for row in evidence["detail"]["turns"])
        rerun = {"success": False, "transcript": []}
        module.execute(cfg, rerun)
        assert (
            rerun["success"] is True and len(requests) == 2 and len(set(attempts)) == 2
        )
        assert [row["request_id"] for row in rerun["detail"]["turns"]] == [
            row["request_id"] for row in evidence["detail"]["turns"]
        ]
    encoded = json.dumps(remote_common.redact(evidence))
    assert "must-not-escape" not in encoded and len(encoded.encode()) < 24000


def test_hosted_chat_fixture_is_explicit_and_not_e40(tmp_path):
    module, _ = shipped_script(tmp_path, "hosted_chat")
    assert not module.valid_fixture({})
    assert stages.JOURNEY_DRIVERS["E40"] == "story_chat"
    assert stages.JOURNEY_DRIVERS["D01"] == "hosted_chat"
    assert "hosted_chat" in bundle.purposes()


@pytest.mark.parametrize(
    "fault", ["instance_loss", "sink_failure", "changed_plan", "no_manifest"]
)
def test_hosted_chat_intent_precedes_ssm_and_survives_instance_loss(tmp_path, fault):
    from tests.e2e.cli_uplift.remote.chat_plan import recovery_plan

    cfg = {
        "evaluation_id": "durable-chat",
        "gateway_url": "https://gateway",
        "test_user_id": "login",
        "human_task_chat": {
            "tenant_id": "tenant",
            "canonical_user_id": "human",
            "max_tasks": 2,
            "max_task_usd": 0.25,
        },
    }
    cfg["recovery_plan"] = recovery_plan(cfg)
    durable = []
    calls = []

    def push(document, *, critical):
        assert critical is True
        if fault == "sink_failure":
            raise RuntimeError("durable sink unavailable")
        durable.append(json.loads(json.dumps(document)))

    manifest = cleanup.Manifest(tmp_path / "manifest.json", "chat", on_change=push)

    class Ssm:
        def json_result(self, *args, **kwargs):
            calls.append("ssm")
            assert durable[-1]["diagnostic_intents"][
                "hosted_chat:durable-chat"
            ] == recovery_plan(cfg)
            raise RuntimeError("EC2 terminated after acceptance, before any result")

    worker = live._run_worker(Ssm(), {}, lambda *args: calls.append("install"))
    if fault == "changed_plan":
        cfg["recovery_plan"]["turns"][0]["message"] = "replacement paid request"
    with pytest.raises((RuntimeError, ValueError, PortError)):
        worker(
            "i-owned",
            "hosted_chat",
            cfg,
            manifest=None if fault == "no_manifest" else manifest,
        )
    if fault == "instance_loss":
        assert calls == ["install", "ssm"]
        # Worker state and output are absent. An independent caller can still
        # reconstruct exactly the original requests from the external snapshot.
        import shutil

        shutil.rmtree(tmp_path)
        retained = durable[-1]["diagnostic_intents"]["hosted_chat:durable-chat"]
        assert retained["turns"] == recovery_plan(cfg)["turns"]
        assert len({turn["request_id"] for turn in retained["turns"]}) == 2
    else:
        assert calls == []


def test_diagnostic_manifest_refuses_replacement_request_and_local_only_sink(tmp_path):
    plan = {"evaluation_id": "same", "turns": [{"request_id": "original"}]}
    local = cleanup.Manifest(tmp_path / "local" / "manifest.json", "chat")
    with pytest.raises(ValueError, match="external durable"):
        local.record_diagnostic("hosted_chat", plan)
    manifest = cleanup.Manifest(
        tmp_path / "durable" / "manifest.json", "chat", on_change=lambda *a, **k: None
    )
    manifest.record_diagnostic("hosted_chat", plan)
    manifest.record_diagnostic("hosted_chat", plan)
    with pytest.raises(ValueError, match="different inputs"):
        manifest.record_diagnostic(
            "hosted_chat", {**plan, "turns": [{"request_id": "replacement"}]}
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "lost_create_receipt",
        "stale_write_accepted",
        "unlink_unavailable",
        "externally_verified",
        "wrong_fixture",
    ],
)
def test_shipped_vault_lifecycle_owns_mutations_and_retains_recovery(
    tmp_path, monkeypatch, fault
):
    script, remote_common = shipped_script(tmp_path, "vault_lifecycle")
    work = tmp_path / "worker"
    work.mkdir()
    vault = {
        "unrelated": {
            "id": "unrelated",
            "service": "existing",
            "label": "retain",
            "scope": "user",
            "revision": "old",
        }
    }
    links = {
        "unrelated-link": {
            "id": "unrelated-link",
            "provider": "github",
            "provider_user_id": "existing",
            "verification_method": "oauth",
            "verified_at": "then",
        }
    }
    mutations = []
    synthetic_values = []
    operation = [None]

    class Cli:
        def __init__(self, binary, env, transcript, **kwargs):
            assert env["BG_CONFIG_DIR"].startswith(env["HOME"])
            assert env["ADP_TENANT"] == "fixture-tenant"

        def json(self, args, **kwargs):
            if args[:3] == ["models", "mappings", "list"]:
                return {
                    "detail": {
                        "principal_id": "wrong"
                        if fault == "wrong_fixture"
                        else "fixture-user",
                        "tenant_id": "fixture-tenant",
                    }
                }
            if args[1] == "list":
                rows = vault if args[0] == "credential" else links
                return {
                    "detail": {
                        "items": [dict(row) for row in rows.values()],
                        "complete": True,
                    }
                }
            assert "--dry-run" in args
            return {"status": "dry_run"}

        def run(self, args, **kwargs):
            area, action = args[:2]
            if action == "update" and "--value-stdin" in args:
                return 1, {"error": {"code": "usage_error"}}
            if action == "link" and "--resume" in args:
                row = next(
                    row
                    for row in links.values()
                    if row.get("provider_user_id")
                    == args[args.index("--provider-user-id") + 1]
                )
                return 4, {"detail": dict(row)}
            mutations.append(list(args))
            if area == "credential":
                if action == "add":
                    value = kwargs["stdin_text"]
                    synthetic_values.append(value)
                    assert value.startswith("ADP_SYNTHETIC_NOT_A_PROVIDER_CREDENTIAL_")
                    key = args[args.index("--operation-id") + 1]
                    operation[0] = key
                    new = key not in vault
                    vault.setdefault(
                        key,
                        {
                            "id": key,
                            "service": args[args.index("--service") + 1],
                            "label": args[args.index("--label") + 1],
                            "scope": "user",
                            "revision": "r1",
                        },
                    )
                    if fault == "lost_create_receipt" and new:
                        return 5, None
                    return 0, {"detail": dict(vault[key])}
                key = args[2]
                assert key != "unrelated"
                if action == "delete":
                    vault.pop(key, None)
                    return 0, {"detail": {"id": key}}
                assert action == "update"
                revision = args[args.index("--expected-revision") + 1]
                if (
                    revision != vault[key]["revision"]
                    and fault != "stale_write_accepted"
                ):
                    return 4, {"error": {"http_status": 409}}
                vault[key].update(label=args[args.index("--label") + 1], revision="r2")
                return 0, {"detail": dict(vault[key])}
            if action == "link":
                links["owned-link"] = {
                    "id": "owned-link",
                    "provider": "discord",
                    "provider_user_id": args[args.index("--provider-user-id") + 1],
                    "verification_method": "oauth"
                    if fault == "externally_verified"
                    else "self_asserted",
                    "verified_at": "now" if fault == "externally_verified" else None,
                }
                return 4, {"detail": {"identity_id": "owned-link"}}
            assert action == "unlink" and args[2] == "owned-link"
            if fault == "unlink_unavailable":
                return 5, None
            links.pop("owned-link")
            return 0, {"detail": {"id": "owned-link"}}

    monkeypatch.setattr(remote_common, "Cli", Cli)
    monkeypatch.setattr(
        remote_common,
        "clean_env",
        lambda cfg, **kwargs: {key: str(value) for key, value in kwargs.items()},
    )
    monkeypatch.setattr(
        remote_common, "session_tokens", lambda cfg: {"access_token": "do-not-persist"}
    )
    monkeypatch.setattr(script, "_write_session", lambda *args: None)
    cfg = {
        "evaluation_id": "stable-vault-run",
        "gateway_url": "https://gateway",
        "cli_path": "/installed/adp",
        "work_dir": str(work),
        "test_user_id": "fixture-login",
        "vault_lifecycle": {
            "owned_mutations_authorized": True,
            "login_user_id": "fixture-login",
            "canonical_user_id": "fixture-user",
            "tenant_id": "fixture-tenant",
        },
    }
    cfg["recovery_plan"] = script.recovery_plan(cfg)
    evidence = {"success": False, "transcript": []}
    if fault in {
        "stale_write_accepted",
        "unlink_unavailable",
        "externally_verified",
        "wrong_fixture",
    }:
        with pytest.raises(remote_common.RemoteError):
            script.execute(cfg, evidence)
        assert evidence["success"] is False
    else:
        script.execute(cfg, evidence)
        assert evidence["success"] is True
        assert evidence["detail"]["entry_phase"] == "metadata_absent"
        assert evidence["detail"]["identity_phase"] == "absent"
        assert "stale-update-refused" in evidence["detail"]["checks"]
        assert len(synthetic_values) == 2 and synthetic_values[0] == synthetic_values[1]
    assert "unrelated" in vault and "unrelated-link" in links
    if fault == "wrong_fixture":
        assert not mutations
        return
    assert (
        operation[0] not in vault
    )  # Cleanup proceeds even when the link cannot be removed.
    durable = evidence["detail"]
    assert durable["operation_id"] == operation[0]
    assert durable["provider_user_id"].startswith("adp-evaluation-")
    if fault in {"unlink_unavailable", "externally_verified"}:
        assert durable["identity_phase"] == "cleanup_pending"
        if fault == "externally_verified":
            assert not any(args[:2] == ["identity", "unlink"] for args in mutations)
    import shutil

    shutil.rmtree(work)
    published = json.dumps(remote_common.redact(evidence))
    assert "do-not-persist" not in published and all(
        value not in published for value in synthetic_values
    )
    assert json.loads(published)["detail"]["operation_id"] == operation[0]
    assert len(published.encode()) < 24000


@pytest.mark.parametrize("changed", [False, True])
def test_vault_lifecycle_requires_exact_predeclared_plan_before_any_access(
    tmp_path, monkeypatch, changed
):
    script, remote_common = shipped_script(tmp_path, "vault_lifecycle")
    cfg = {
        "evaluation_id": "stable-run",
        "gateway_url": "https://gateway",
        "cli_path": "/installed/adp",
        "work_dir": str(tmp_path / "work"),
        "test_user_id": "fixture-login",
        "vault_lifecycle": {
            "owned_mutations_authorized": True,
            "login_user_id": "fixture-login",
            "canonical_user_id": "fixture-user",
            "tenant_id": "fixture-tenant",
        },
    }
    plan = script.recovery_plan(cfg)
    assert script.recovery_plan(cfg) == plan
    assert set(plan).isdisjoint({"password", "token", "value", "secret"})
    if changed:
        cfg["recovery_plan"] = {**plan, "operation_id": "changed"}
    monkeypatch.setattr(
        remote_common,
        "session_tokens",
        lambda _: pytest.fail("No fixture auth before plan verification"),
    )
    with pytest.raises(remote_common.RemoteError, match="durably recorded"):
        script.execute(cfg, {"success": False, "transcript": []})
    assert not Path(cfg["work_dir"]).exists()


@pytest.mark.parametrize(
    "fault", ["instance_loss", "sink_failure", "changed_plan", "no_manifest"]
)
def test_vault_plan_durable_before_dispatch(tmp_path, fault):
    from tests.e2e.cli_uplift.remote.vault_lifecycle_plan import recovery_plan

    payload = {
        "evaluation_id": "vault-run",
        "gateway_url": "https://gateway",
        "vault_lifecycle": {
            "tenant_id": "tenant",
            "canonical_user_id": "owner",
            "login_user_id": "login",
        },
    }
    payload["recovery_plan"] = recovery_plan(payload)
    snapshots, calls = [], []

    def push(document, *, critical):
        assert critical
        if fault == "sink_failure":
            raise RuntimeError("External sink unavailable")
        snapshots.append(json.loads(json.dumps(document)))

    manifest = cleanup.Manifest(tmp_path / "manifest.json", "vault", on_change=push)

    class Ssm:
        def json_result(self, *args, **kwargs):
            calls.append("ssm")
            assert snapshots[-1]["diagnostic_intents"][
                "vault_lifecycle:vault-run"
            ] == recovery_plan(payload)
            raise RuntimeError("Lost instance before any output")

    if fault == "changed_plan":
        payload["recovery_plan"]["operation_id"] = "different-target"
    worker = live._run_worker(Ssm(), {}, lambda *args: calls.append("install"))
    with pytest.raises((RuntimeError, ValueError, PortError)):
        worker(
            "i-owned",
            "vault_lifecycle",
            payload,
            manifest=None if fault == "no_manifest" else manifest,
        )
    if fault == "instance_loss":
        assert calls == ["install", "ssm"]
        import shutil

        shutil.rmtree(tmp_path)
        retained = snapshots[-1]["diagnostic_intents"]["vault_lifecycle:vault-run"]
        assert retained == recovery_plan(payload)
        assert retained["operation_id"] and retained["provider_user_id"]
    else:
        assert calls == []


def diagnostic_fixture(name="human_task_chat"):
    identity = {
        "login_user_id": "fixture-login",
        "canonical_user_id": "fixture-human",
        "tenant_id": "fixture-tenant",
    }
    if name == "vault_lifecycle":
        return {**identity, "owned_mutations_authorized": True}
    paid = {
        **identity,
        "enrollment_verified": True,
        "shared_budget_authorized": True,
        "max_task_usd": 0.25,
    }
    if name == "human_task_chat":
        return {**paid, "max_tasks": 2}
    return {
        **paid,
        "max_dispatches": 1,
        "scenario": "complete",
        "persona": "agent-task-claude-developer",
        "instructions": "Fix the requested CLI behavior.",
        "snapshot": {
            "schema_version": "1.0",
            "repository_id": 42,
            "repository": "owner/repo",
            "issue": 123,
            "commit_sha": "a" * 40,
            "files": [
                {
                    "path": "cli/main.py",
                    "blob_sha": "b" * 40,
                    "content": "print('hello')\n",
                }
            ],
        },
    }


@pytest.mark.parametrize(
    "name", ["human_task_coding", "human_task_chat", "vault_lifecycle"]
)
def test_workflow_fixture_overlay_same_for_evaluate_and_recover(name):
    from tests.e2e.cli_uplift import fixtures

    value = {name: diagnostic_fixture(name)}
    env = {"CLI_UPLIFT_EVAL_FIXTURES": json.dumps(value)}
    first = config.from_environment(env, base=dict(VALID))
    second = config.from_environment(env, base=dict(VALID))
    assert first == second and first[name] == value[name]
    assert fixtures.parse(json.dumps(value)) == value


@pytest.mark.parametrize(
    "fault",
    [
        "override",
        "unknown",
        "secret",
        "budget",
        "tasks",
        "missing_identity",
        "snapshot",
        "duplicate",
    ],
)
def test_workflow_fixture_overlay_refuses_unbounded_or_secret_inputs(fault):
    from tests.e2e.cli_uplift.fixtures import parse

    value = {"human_task_chat": diagnostic_fixture()}
    if fault == "override":
        value["gateway_url"] = "https://other"
    elif fault == "unknown":
        value["human_task_chat"]["skip_checks"] = True
    elif fault == "secret":
        value["human_task_chat"]["password"] = "not-allowed"
    elif fault == "budget":
        value["human_task_chat"]["max_task_usd"] = 2
    elif fault == "tasks":
        value["human_task_chat"]["max_tasks"] = 3
    elif fault == "missing_identity":
        del value["human_task_chat"]["login_user_id"]
    elif fault == "snapshot":
        value = {"human_task_coding": diagnostic_fixture("human_task_coding")}
        value["human_task_coding"]["snapshot"]["files"][0]["path"] = "../other"
    raw = (
        json.dumps(value)
        if fault != "duplicate"
        else '{"human_task_chat": {}, "human_task_chat": {}}'
    )
    with pytest.raises(config.ConfigError):
        parse(raw)


def test_owned_diagnostics_are_explicit_and_missing_fixtures_block():
    assert {
        row.id
        for row in cases.resolve_suites(["login", "hosted-chat", "vault-lifecycle"])
    } == {"E01", "C01", "D01", "D02"}
    assert not {"D01", "D02"}.intersection(
        row.id for row in cases.suite_cases("nightly")
    )
    assert not {"D01", "D02"}.intersection(row.id for row in cases.suite_cases("full"))
    matrix = cases.new_matrix(["hosted-chat", "vault-lifecycle"])
    blocked = cases.block_missing_fixtures(matrix, config.fixture_classes(VALID))
    assert set(blocked) == {"D01", "D02"}
    cfg = {
        **VALID,
        "human_task_chat": diagnostic_fixture(),
        "vault_lifecycle": diagnostic_fixture("vault_lifecycle"),
    }
    assert {cases.HUMAN_TASK_CHAT, cases.VAULT_LIFECYCLE} <= config.fixture_classes(cfg)


@pytest.mark.parametrize(
    "purpose,key",
    [
        ("hosted_coding", "human_task_coding"),
        ("hosted_chat", "human_task_chat"),
        ("vault_lifecycle", "vault_lifecycle"),
    ],
)
def test_diagnostic_journey_passes_manifest_and_records_plan_before_ssm(
    tmp_path, monkeypatch, purpose, key
):
    payload = {
        "evaluation_id": "workflow-fixture",
        "gateway_url": "https://gateway",
        "test_user_id": "fixture-login",
        key: diagnostic_fixture(key),
    }
    snapshots = []
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        "fixture",
        on_change=lambda doc, **kw: snapshots.append(json.loads(json.dumps(doc))),
    )
    monkeypatch.setattr(live, "_journey_payload", lambda cfg, ctx: dict(payload))

    class Ssm:
        def json_result(self, *args, **kwargs):
            assert (
                snapshots[-1]["diagnostic_intents"][purpose + ":workflow-fixture"][
                    "evaluation_id"
                ]
                == "workflow-fixture"
            )
            return "command-id", {"success": True, "detail": {"cleanup": "complete"}}

    driver = live._journey(Ssm(), {}, lambda *args: None)(purpose)
    assert driver("i-owned", {"manifest": manifest})["success"] is True


def coding_remote_config(module, fixture, work):
    fixture = {
        **fixture,
        "login_user_id": "fixture-login",
        "canonical_user_id": "fixture-human",
        "tenant_id": "fixture-tenant",
    }
    cfg = {
        "evaluation_id": "stable-coding-run",
        "test_user_id": "fixture-login",
        "human_task_coding": fixture,
        "cli_path": "/fixture/adp",
        "gateway_url": "https://gateway",
        "work_dir": str(work),
    }
    cfg["recovery_plan"] = module.recovery_plan(cfg)
    return cfg


@pytest.mark.parametrize(
    "fault", ["instance_loss", "sink_failure", "changed_plan", "no_manifest"]
)
def test_coding_workflow_intent_durable_before_dispatch(tmp_path, fault):
    from tests.e2e.cli_uplift.remote.coding_plan import recovery_plan

    payload = {
        "evaluation_id": "coding-run",
        "gateway_url": "https://gateway",
        "human_task_coding": diagnostic_fixture("human_task_coding"),
    }
    payload["recovery_plan"] = recovery_plan(payload)
    snapshots, calls = [], []

    def push(document, *, critical):
        assert critical
        if fault == "sink_failure":
            raise RuntimeError("External sink unavailable")
        snapshots.append(json.loads(json.dumps(document)))

    manifest = cleanup.Manifest(tmp_path / "manifest.json", "coding", on_change=push)

    class Ssm:
        def json_result(self, *args, **kwargs):
            calls.append("ssm")
            assert snapshots[-1]["diagnostic_intents"][
                "hosted_coding:coding-run"
            ] == recovery_plan(payload)
            raise RuntimeError("Lost instance before any output")

    if fault == "changed_plan":
        payload["recovery_plan"]["request_id"] = "replacement-request"
    worker = live._run_worker(Ssm(), {}, lambda *args: calls.append("install"))
    with pytest.raises((RuntimeError, ValueError, PortError)):
        worker(
            "i-owned",
            "hosted_coding",
            payload,
            manifest=None if fault == "no_manifest" else manifest,
        )
    if fault == "instance_loss":
        assert calls == ["install", "ssm"]
        import shutil

        shutil.rmtree(tmp_path)
        retained = snapshots[-1]["diagnostic_intents"]["hosted_coding:coding-run"]
        assert retained == recovery_plan(payload)
        assert retained["request_id"] == recovery_plan(payload)["request_id"]
    else:
        assert calls == []


def test_coding_requires_original_plan_before_fixture_session_access(
    tmp_path, monkeypatch
):
    module, remote_common = shipped_script(tmp_path, "hosted_coding")
    cfg = coding_remote_config(
        module, diagnostic_fixture("human_task_coding"), tmp_path / "absent"
    )
    cfg["recovery_plan"]["request_id"] = "changed"
    monkeypatch.setattr(
        remote_common,
        "session_tokens",
        lambda _: pytest.fail("No fixture access before recovery guard"),
    )
    with pytest.raises(remote_common.RemoteError, match="exact coding recovery"):
        module.execute(cfg, {"transcript": []})
    assert not Path(cfg["work_dir"]).exists()


def test_fixture_input_exposed_identically_to_evaluate_and_recover():
    import yaml

    workflow = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2]
            / ".github/workflows/eval-cli-uplift.yml"
        ).read_text()
    )
    events = workflow.get("on", workflow.get(True))
    for trigger in ("workflow_dispatch", "workflow_call"):
        assert events[trigger]["inputs"]["fixtures_json"]["default"] == "{}"
    for job in ("evaluate", "recover"):
        assert (
            workflow["jobs"][job]["env"]["CLI_UPLIFT_EVAL_FIXTURES"]
            == "${{ inputs.fixtures_json }}"
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "lost_create_reply",
        "foreign_baseline",
        "wrong_actor",
        "revoked_still_allowed",
        "restore_failure",
        "cleanup_failure",
        "changed_default",
        "wrong_parentage",
        "ordinary_read_allowed",
        "ordinary_write_allowed",
        "name_selector_accepted",
        "duplicate_create_accepted",
        "delete_retry_accepted",
    ],
)
def test_shipped_hierarchy_owned_cleanup_and_tenant_revocation(
    tmp_path, monkeypatch, fault
):
    script, remote_common = shipped_script(tmp_path, "hierarchy_lifecycle")
    fixture = {
        "owned_mutations_authorized": True,
        "login_user_id": "admin-native",
        "canonical_user_id": "admin-tenant",
        "tenant_id": "aws-e",
        "ordinary_login_user_id": "ordinary-native",
        "ordinary_canonical_user_id": "ordinary-tenant",
        "ordinary_native_tenant": "native",
    }
    cfg = {
        "evaluation_id": "hierarchy-test",
        "test_user_id": "admin-native",
        "gateway_url": "https://gateway",
        "cli_path": "/served/adp",
        "hierarchy_lifecycle": fixture,
    }
    cfg["recovery_plan"] = script.recovery_plan(cfg)
    plan = cfg["recovery_plan"]
    resources, mutations = {}, []
    member = {
        "role": "member",
        "team_id": "",
        "teams": [],
        "membership_status": "active",
    }
    if fault == "foreign_baseline":
        member["teams"] = [
            {"team_id": "unrelated", "role": "member", "is_primary": True}
        ]
    revision = [0]

    def snapshot():
        return {"revision": str(revision[0]), "resource": copy.deepcopy(member)}

    class Cli:
        def __init__(self, binary, env, *args, **kwargs):
            self.admin = pathlib.Path(env["HOME"]).name == "admin"
            self.tenant = env["ADP_TENANT"]
            assert env["BG_CONFIG_DIR"].startswith(env["HOME"])

        def json(self, argv, **kwargs):
            code, result = self.run(argv, **kwargs)
            if code != 0:
                raise remote_common.RemoteError("CLI failed")
            return result

        def run(self, argv, **kwargs):
            def option(name):
                return argv[argv.index(name) + 1]

            if argv[:3] == ["models", "mappings", "list"]:
                if (
                    not self.admin
                    and self.tenant == "aws-e"
                    and member["membership_status"] == "revoked"
                    and fault != "revoked_still_allowed"
                ):
                    return 3, {"error": {"code": "tenant_not_visible"}}
                actor = (
                    "admin-tenant"
                    if self.admin
                    else "ordinary-tenant"
                    if self.tenant == "aws-e"
                    else "ordinary-native"
                )
                if fault == "wrong_actor" and not self.admin:
                    actor = "foreign"
                return 0, {"detail": {"principal_id": actor, "tenant_id": self.tenant}}
            if argv[:2] == ["admin", "login"]:
                return 3, {"error": {"code": "permission_denied"}}
            if argv[:1] == ["--tenant"]:
                return 3, {"error": {"code": "tenant_not_visible"}}
            assert argv[0] == "admin"
            if not self.admin:
                if fault == "ordinary_read_allowed" and argv[2] == "show":
                    return 0, {"detail": {}}
                if fault == "ordinary_write_allowed" and argv[2] == "update":
                    return 0, {"detail": {}}
                return 5, {"error": {"http_status": 403}}
            if argv[1:3] == ["team", "show"] and "--name" in argv:
                return (
                    (0, {})
                    if fault == "name_selector_accepted"
                    else (1, {"error": {"code": "usage_error"}})
                )
            kind, action = argv[1:3]
            if kind == "member" and action == "remove" and "--dry-run" in argv:
                return 0, {"status": "dry_run", "detail": {"before": snapshot()}}
            if "--dry-run" in argv:
                return 0, {"status": "dry_run"}
            org = option("--org") if "--org" in argv else option("--id")
            key = org if kind == "org" else option("--id") if "--id" in argv else None
            if action == "show":
                row = resources.get((kind, key, org))
                return (
                    (0, {"detail": copy.deepcopy(row)})
                    if row
                    else (5, {"error": {"http_status": 404}})
                )
            if (
                kind == "org"
                and action == "update"
                and option("--expected-revision")
                != resources[(kind, key, org)]["revision"]
            ):
                return 4, {"error": {"code": "stale_revision"}}
            if (
                kind == "department"
                and action == "delete"
                and any(k[0] == "team" and k[2] == org for k in resources)
            ):
                return 4, {"error": {"code": "hierarchy_has_dependencies"}}
            if action == "create" and (kind, key, org) in resources:
                return (
                    (0, {})
                    if fault == "duplicate_create_accepted"
                    else (5, {"error": {"http_status": 409}})
                )
            if (
                action == "delete"
                and (kind, key, org) not in resources
                and kind != "member"
            ):
                return (
                    (0, {})
                    if fault == "delete_retry_accepted"
                    else (5, {"error": {"http_status": 404}})
                )
            mutations.append(argv)
            revision[0] += 1
            if kind == "member":
                if action == "remove":
                    member.update(membership_status="revoked", team_id="", teams=[])
                else:
                    if fault == "restore_failure":
                        return 5, {}
                    member["membership_status"] = "active"
                return 0, {"detail": snapshot()}
            if kind == "team" and action == "members":
                team = option("--team")
                if argv[3] == "add":
                    member["teams"].append(
                        {
                            "team_id": team,
                            "role": "member",
                            "is_primary": not member["teams"],
                        }
                    )
                else:
                    member["teams"] = [
                        r for r in member["teams"] if r["team_id"] != team
                    ]
                member["team_id"] = (
                    member["teams"][0]["team_id"] if member["teams"] else ""
                )
                if member["teams"]:
                    member["teams"][0]["is_primary"] = True
                return 0, {"detail": snapshot()}
            resource_key = (kind, key, org)
            if action == "create":
                resources[resource_key] = {
                    "id": key,
                    "revision": str(revision[0]),
                    "resource": {
                        "name": key,
                        "org_id": org,
                        "department_id": "foreign-department"
                        if fault == "wrong_parentage"
                        else plan["department_id"],
                    },
                }
                if kind == "org":
                    for child_kind, child_id in (
                        ("department", plan["default_department_id"]),
                        ("team", plan["default_team_id"]),
                    ):
                        resources[(child_kind, child_id, org)] = {
                            "id": child_id,
                            "revision": str(revision[0]),
                            "resource": {
                                "org_id": org,
                                "name": "Default",
                                "description": "Default " + child_kind,
                                "department_id": plan["default_department_id"],
                            },
                        }
                if kind == "org" and fault == "changed_default":
                    resources[("team", plan["default_team_id"], org)]["resource"][
                        "name"
                    ] = "Changed by another actor"
                if fault == "lost_create_reply":
                    raise remote_common.RemoteError("Accepted create lost reply")
            elif action == "update":
                resources[resource_key]["revision"] = str(revision[0])
                resources[resource_key]["resource"]["name"] = option("--name")
            else:
                assert action == "delete"
                if fault == "cleanup_failure":
                    return 5, {}
                resources.pop(resource_key, None)
            return 0, {"detail": {}}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __init__(self, value):
            self.value = value

        def read(self):
            return json.dumps(self.value).encode()

    def urlopen(request, **kwargs):
        if request.full_url.endswith("/workspaces/context"):
            return Response(
                {
                    "canonical_user_id": "ordinary-tenant",
                    "tenant_id": "aws-e",
                    "context_token": "private-ordinary-lease",
                }
            )
        if request.full_url.endswith("/me/persona-models"):
            raise script.urllib.error.HTTPError(
                request.full_url, 403, "revoked", {}, None
            )
        return Response({"access_token": "private-ordinary-token"})

    monkeypatch.setattr(script.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(remote_common, "Cli", Cli)
    monkeypatch.setattr(
        remote_common,
        "clean_env",
        lambda cfg, **kwargs: {key: str(value) for key, value in kwargs.items()},
    )
    monkeypatch.setattr(
        remote_common,
        "session_tokens",
        lambda cfg: {"access_token": "private-admin-token"},
    )
    monkeypatch.setattr(
        remote_common,
        "fixture_secret",
        lambda cfg, env, key: "private-ordinary-password"
        if key.endswith("password")
        else "owned-ordinary",
    )
    monkeypatch.setattr(script, "_write_session", lambda *a: None)
    evidence = {"success": False, "transcript": []}
    if fault:
        with pytest.raises(remote_common.RemoteError):
            script.execute(cfg, evidence)
        assert evidence["success"] is False
    else:
        script.execute(cfg, evidence)
        assert evidence["success"] is True
        assert "revoked-tenant-denied-native-preserved" in evidence["detail"]["checks"]
        assert {
            "explicit-department-team-parentage",
            "ordinary-hierarchy-read-write-refused",
            "canonical-id-required-name-selector-refused",
            "same-id-create-conflict-original-unchanged",
            "same-id-delete-retry-reports-absence",
        } <= set(evidence["detail"]["checks"])
        assert (
            evidence["detail"]["parentage"][plan["team_ids"][0]]["department_id"]
            == plan["department_id"]
        )
        assert not any(
            "--role" in argv and argv[argv.index("--role") + 1] != "member"
            for argv in mutations
        )
    if fault in {"wrong_actor", "foreign_baseline"}:
        assert mutations == []
    if fault not in {"cleanup_failure", "changed_default", "wrong_parentage"}:
        assert not resources
    if fault == "wrong_parentage":
        assert ("team", plan["team_ids"][0], fixture["tenant_id"]) in resources
        assert evidence["detail"]["cleanup"][plan["team_ids"][0]] == "pending"
    if fault == "changed_default":
        assert ("team", plan["default_team_id"], plan["org_id"]) in resources
        assert evidence["detail"]["cleanup"][plan["default_team_id"]] == "pending"
    if fault not in {"foreign_baseline", "restore_failure"}:
        assert member == plan["restore"]
    if fault == "restore_failure":
        assert evidence["detail"]["membership_restoration"] == "pending"
    serialized = json.dumps(remote_common.redact(evidence))
    assert "private-ordinary" not in serialized and "private-admin" not in serialized


def test_hierarchy_plan_precedes_instance_loss_and_rejects_changed_inputs(tmp_path):
    from tests.e2e.cli_uplift.remote.hierarchy_plan import recovery_plan

    payload = {
        "evaluation_id": "owned",
        "gateway_url": "https://gateway",
        "hierarchy_lifecycle": {"tenant_id": "aws-e"},
    }
    payload["recovery_plan"] = recovery_plan(payload)
    saved = []
    manifest = cleanup.Manifest(
        tmp_path / "manifest.json",
        "hierarchy",
        on_change=lambda doc, **kw: saved.append(copy.deepcopy(doc)),
    )
    ssm = Mock()
    ssm.json_result.side_effect = RuntimeError("Instance lost")
    worker = live._run_worker(ssm, {}, lambda *a: None)
    with pytest.raises(RuntimeError):
        worker("i-owned", "hierarchy_lifecycle", payload, manifest=manifest)
    assert saved[-1]["diagnostic_intents"][
        "hierarchy_lifecycle:owned"
    ] == recovery_plan(payload)
    assert ssm.json_result.call_count == 1
    payload["recovery_plan"]["team_ids"] = ["foreign"]
    with pytest.raises(PortError):
        worker("i-owned", "hierarchy_lifecycle", payload, manifest=manifest)
    assert ssm.json_result.call_count == 1


@pytest.mark.parametrize(
    "fault",
    [
        "lost_create_reply",
        "lost_client",
        "sink_failure",
        "changed_scope",
        "changed_payload",
    ],
)
def test_workspace_create_intent_is_external_and_immutable_before_transport(
    tmp_path, fault
):
    from tests.e2e.cli_uplift.workspace_recovery import create_intent, dispatch_create

    plan = create_intent(
        evaluation_id="stable-workspace-run",
        gateway_url="https://gateway",
        tenant_id="tenant",
        principal_id="ordinary",
        session_secret_name="adp/eval/ordinary",
        operation_id="d03af966-58ed-4d9b-8bf9-cc9ae17fbcad",
        name="owned-workspace",
    )
    retained, calls = [], []

    def push(document, *, critical):
        assert critical
        if fault == "sink_failure":
            raise RuntimeError("No durable sink")
        retained.append(json.loads(json.dumps(document)))

    manifest = cleanup.Manifest(tmp_path / "manifest.json", "workspace", on_change=push)

    def dispatch(original):
        calls.append(original)
        assert (
            retained[-1]["diagnostic_intents"][
                "superplane_workspace:stable-workspace-run"
            ]
            == plan
        )
        raise RuntimeError("Accepted create but transport output lost")

    with pytest.raises(RuntimeError):
        dispatch_create(manifest, plan, dispatch)
    if fault == "sink_failure":
        assert calls == []
        return
    assert len(calls) == 1
    recovered = retained[-1]["diagnostic_intents"][
        "superplane_workspace:stable-workspace-run"
    ]
    import shutil

    shutil.rmtree(tmp_path)
    replacement = cleanup.Manifest(
        tmp_path / "replacement.json", "workspace", on_change=push
    )
    replacement.record_diagnostic("superplane_workspace", recovered)
    if fault == "changed_scope":
        recovered = {**recovered, "principal_id": "foreign"}
    if fault == "changed_payload":
        recovered = {**recovered, "argv": ["replacement"]}
    if fault in {"changed_scope", "changed_payload"}:
        with pytest.raises(ValueError):
            dispatch_create(
                replacement,
                recovered,
                lambda _: pytest.fail("No foreign or changed dispatch"),
            )
    else:
        result = dispatch_create(
            replacement,
            recovered,
            lambda p: {
                "operation_id": p["operation_id"],
                "resource_id": "exact-created-id",
            },
        )
        assert result["operation_id"] == plan["operation_id"]
        assert recovered["request"] == plan["request"]
    assert "access_token" not in json.dumps(retained)


@pytest.mark.parametrize(
    "states,expected",
    [
        (["queued", "running"], "running"),
        (["completed"], "completed"),
        (["queued"] * 4, "queued"),
    ],
)
def test_coding_control_records_actual_state_with_bounded_same_task_poll(
    tmp_path, monkeypatch, states, expected
):
    module, _ = shipped_script(tmp_path, "hosted_coding")
    calls = []
    ticks = iter(range(10))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    pending = iter(states)

    class Cli:
        def json(self, argv):
            calls.append(argv)
            return {"detail": {"task_id": "tsk-owned", "status": next(pending)}}

    evidence = {}
    assert (
        module.observe_before_control(
            Cli(),
            "tsk-owned",
            {"control_when": "running", "running_wait_seconds": 2},
            evidence,
        )
        == expected
    )
    assert evidence["pre_control_status"] == expected
    assert all(argv == ["agent", "status", "--run", "tsk-owned"] for argv in calls)


@pytest.mark.parametrize(
    "status,task_id,command_id",
    [
        ("error", "tsk-owned", "command"),
        ("pending", "tsk-other", "command"),
        ("pending", "tsk-owned", "other"),
    ],
)
def test_coding_control_does_not_mistake_exit_four_for_receipt(
    tmp_path, status, task_id, command_id
):
    module, common = shipped_script(tmp_path, "hosted_coding")
    with pytest.raises(common.RemoteError, match="acceptance unconfirmed"):
        module.require_control_receipt(
            {
                "status": status,
                "detail": {"task_id": task_id, "command_id": command_id},
            },
            "tsk-owned",
            "command",
        )
    module.require_control_receipt(
        {
            "status": "pending",
            "detail": {"task_id": "tsk-owned", "command_id": "command"},
        },
        "tsk-owned",
        "command",
    )


def test_coding_terminal_before_control_fails_without_replacement_or_abort(
    tmp_path, monkeypatch
):
    module, remote_common = shipped_script(tmp_path, "hosted_coding")
    task_id = "tsk_12345678-1234-4123-8123-123456789abc"
    calls = []

    class Cli:
        def __init__(self, *args, **kwargs):
            pass

        def json(self, argv, **kwargs):
            calls.append(argv)
            if argv == ["models", "mappings", "list"]:
                return {
                    "detail": {
                        "tenant_id": "fixture-tenant",
                        "principal_id": "fixture-human",
                    }
                }
            if "--dry-run" in argv:
                return {"status": "dry_run"}
            return {
                "status": "pending" if argv[:2] == ["agent", "trigger"] else "ok",
                "detail": {"task_id": task_id, "status": "completed"},
            }

        def run(self, *args, **kwargs):
            pytest.fail("Terminal Task must not be cancelled or replaced")

    monkeypatch.setattr(remote_common, "Cli", Cli)
    monkeypatch.setattr(
        remote_common,
        "clean_env",
        lambda cfg, **kwargs: {key: str(value) for key, value in kwargs.items()},
    )
    monkeypatch.setattr(remote_common, "session_tokens", lambda cfg: {})
    monkeypatch.setattr(module, "_write_session", lambda *args: None)
    fixture = dict(
        enrollment_verified=True,
        shared_budget_authorized=True,
        max_dispatches=1,
        max_task_usd=1,
        scenario="cancel",
        control_when="running",
        persona="agent-task-codex-developer",
        snapshot={"repository": "owner/repo", "issue": 42},
        instructions="bounded edit",
    )
    evidence = {"transcript": []}
    with pytest.raises(remote_common.RemoteError, match="terminal before cancellation"):
        module.execute(coding_remote_config(module, fixture, tmp_path), evidence)
    assert evidence["pre_control_status"] == "completed"
    assert not any(argv[1] in {"abort", "steer"} for argv in calls)
    triggers = [argv for argv in calls if argv[1] == "trigger" and "--yes" in argv]
    assert len(triggers) == 2 and triggers[0] == triggers[1]


@pytest.mark.parametrize(
    "fault", [None, "missing_native", "missing_vault_native", "different_native"]
)
def test_story_reads_pin_verified_native_tenant_with_multiple_memberships(
    tmp_path, monkeypatch, fault
):
    script, common = shipped_script(tmp_path, "story_reads")
    cfg = {
        "mode": "capabilities",
        "cli_path": "/served/adp",
        "gateway_url": "https://gateway.example",
        "region": "us-east-1",
        "sts_endpoint": "https://sts.us-east-1.amazonaws.com",
        "org_id": "native-tenant",
    }
    session = {"org_id": "native-tenant"}
    if fault == "missing_native":
        cfg.pop("org_id")
    elif fault == "missing_vault_native":
        session.pop("org_id")
    elif fault == "different_native":
        session["org_id"] = "another-tenant"
    monkeypatch.setattr(common, "load_session", lambda _: session)
    monkeypatch.setattr(script, "_write_session", lambda *args: None)
    calls = []
    memberships = ["another-tenant", "native-tenant"]

    class Cli:
        def __init__(self, binary, env, transcript, **kwargs):
            calls.append(env)
            # CLI deliberately refuses ambiguous sessions without a selection.
            assert env["ADP_TENANT"] == memberships[1]
            assert env["ADP_TENANT"] != memberships[0]
            assert env["BG_CONFIG_DIR"].startswith(env["HOME"])

    monkeypatch.setattr(common, "Cli", Cli)
    monkeypatch.setitem(script.SCENARIOS, "capabilities", lambda cli, evidence: None)
    evidence = {"transcript": []}
    if fault:
        with pytest.raises(
            common.RemoteError, match="verified login session native tenant"
        ):
            script.execute(cfg, evidence)
        assert calls == []
    else:
        script.execute(cfg, evidence)
        assert len(calls) == 1
        assert evidence["detail"]["tenant_id"] == "native-tenant"
        assert evidence["detail"]["tenant_selection"] == "verified_native_login_session"


def test_capability_contrast_can_be_selected_without_unrelated_parity_mutations():
    from tests.e2e.cli_uplift.fixtures import parse

    fixture = capability_contrast_config()["capability_contrast"]
    assert (
        parse(json.dumps({"capability_contrast": fixture}))["capability_contrast"]
        == fixture
    )
    assert [case.id for case in cases.suite_cases("capability-contrast")] == ["E19"]
    with pytest.raises(config.ConfigError):
        parse(
            json.dumps({"capability_contrast": {**fixture, "access_token": "private"}})
        )


@pytest.mark.parametrize("fails", [False, True])
def test_capability_ordinary_fixture_login_uses_existing_secret_without_token_store(
    tmp_path, monkeypatch, fails
):
    module, common = shipped_script(tmp_path, "capability_contrast")
    cfg = {
        "gateway_url": "https://gateway/api",
        "region": "us-east-1",
        "sts_endpoint": "https://sts",
    }
    contrast = {"ordinary_fixture_name": "owned-fixture"}

    def secret(config, env, key, **kwargs):
        assert config["credential_secret"] == "owned-fixture"
        return {
            "ordinary_session": None,
            "non_admin_username": "ordinary",
            "non_admin_password": "private-password",
        }[key]

    monkeypatch.setattr(common, "fixture_secret", secret)

    def open_request(request, timeout):
        assert request.full_url == "https://gateway/api/auth/cli/password"
        assert json.loads(request.data) == {
            "username": "ordinary",
            "password": "private-password",
        }
        assert timeout == 45
        if fails:
            raise RuntimeError("private-password private-token")
        return io.BytesIO(json.dumps({"access_token": "private-token"}).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", open_request)
    if fails:
        with pytest.raises(
            common.RemoteError, match="^Ordinary fixture authentication failed$"
        ):
            module.ordinary_session(cfg, contrast)
    else:
        assert module.ordinary_session(cfg, contrast) == {
            "access_token": "private-token"
        }


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "foreign_task",
        "foreign_invocation",
        "stale_status",
        "missing_source",
        "missing_id",
    ],
)
def test_coding_activity_readback_requires_exact_task_identity(tmp_path, fault):
    from unittest.mock import MagicMock

    module, common = shipped_script(tmp_path, "hosted_coding")
    task = {
        "task_id": "tsk-owned",
        "invocation_id": "invocation-owned",
        "status": "cancelled",
    }
    activity = {
        "source_type": "task",
        "task_id": "tsk-owned",
        "invocation_id": "invocation-owned",
        "task_snapshot": dict(task),
        "transcript_status": "available",
    }
    if fault == "foreign_task":
        activity["task_id"] = "foreign"
    elif fault == "foreign_invocation":
        activity["task_snapshot"]["invocation_id"] = "foreign"
    elif fault == "stale_status":
        activity["task_snapshot"]["status"] = "running"
    elif fault == "missing_source":
        activity.pop("source_type")
    elif fault == "missing_id":
        task.pop("invocation_id")
    cli = MagicMock()
    cli.json.return_value = {"detail": activity}
    if fault:
        with pytest.raises(common.RemoteError):
            module.activity_readback(cli, task)
    else:
        evidence = module.activity_readback(cli, task)
        assert evidence["task_status"] == "cancelled"
        cli.json.assert_called_once_with(
            ["agent", "status", "--run", "invocation-owned"]
        )


def coding_stream_frame(task, sequence, kind="progress.updated"):
    cursor = f"{task}:{sequence}"
    return {
        "type": "event",
        "data": {
            "event": "event",
            "id": cursor,
            "data": {
                "task_id": task,
                "sequence": sequence,
                "event_id": cursor,
                "type": kind,
            },
        },
    }


def test_coding_stream_ignores_wrapped_snapshot_and_rejects_foreign_cursor(tmp_path):
    module, common = shipped_script(tmp_path, "hosted_coding")
    snapshot = {
        "type": "event",
        "data": {"event": "snapshot", "data": {"task_id": "tsk-owned"}},
    }
    frames, events = module.durable_events(json.dumps(snapshot), "tsk-owned")
    assert len(frames) == 1 and events == []
    foreign = coding_stream_frame("tsk-foreign", 1)
    with pytest.raises(common.RemoteError):
        module.durable_events(json.dumps(foreign), "tsk-owned")


@pytest.mark.parametrize(
    "fault", [None, "gap", "duplicate", "changed", "missing_terminal"]
)
def test_coding_terminal_replay_requires_exact_suffix(tmp_path, monkeypatch, fault):
    module, common = shipped_script(tmp_path, "hosted_coding")
    task = "tsk-owned"
    initial = [
        coding_stream_frame(task, 1),
        coding_stream_frame(task, 2),
        coding_stream_frame(task, 3, "task.cancelled"),
    ]
    _, events = module.durable_events(
        "\n".join(json.dumps(frame) for frame in initial), task
    )
    replayed = copy.deepcopy(initial[1:])
    if fault == "gap":
        replayed = replayed[1:]
    if fault == "duplicate":
        replayed.insert(0, copy.deepcopy(replayed[0]))
    if fault == "changed":
        replayed[0]["data"]["data"]["type"] = "changed"
    if fault == "missing_terminal":
        replayed[-1]["data"]["data"]["type"] = "progress.updated"
    calls = []

    def bounded(argv, **kwargs):
        calls.append(argv)
        return 0, "\n".join(json.dumps(frame) for frame in replayed), ""

    monkeypatch.setattr(common, "bounded", bounded)
    detail = {
        "task_id": task,
        "status": "cancelled",
        "latest_event_cursor": task + ":3",
        "oldest_event_cursor": task + ":1",
    }
    if fault:
        with pytest.raises(common.RemoteError):
            module.verify_replay({"cli_path": "/served/adp"}, {}, detail, events)
    else:
        result = module.verify_replay({"cli_path": "/served/adp"}, {}, detail, events)
        assert result["event_count"] == 2 and result["through"] == task + ":3"
    assert calls[0][calls[0].index("--last-event-id") + 1] == task + ":1"
    assert not any(command in calls[0] for command in ("trigger", "abort", "steer"))


@pytest.mark.parametrize("fault", [None, "absent", "duplicate", "foreign_invocation"])
def test_coding_owner_list_verifies_one_exact_new_task(tmp_path, fault):
    from unittest.mock import MagicMock

    module, common = shipped_script(tmp_path, "hosted_coding")
    detail = {"task_id": "tsk-owned", "invocation_id": "owned", "status": "cancelled"}
    item = {**detail, "source_type": "task", "task_snapshot": dict(detail)}
    items = [item]
    if fault == "absent":
        items = []
    elif fault == "duplicate":
        items.append(dict(item))
    elif fault == "foreign_invocation":
        item["invocation_id"] = "foreign"
    cli = MagicMock()
    cli.json.return_value = {"detail": {"items": items}}
    if fault:
        with pytest.raises(common.RemoteError):
            module.activity_list_readback(cli, detail)
    else:
        assert module.activity_list_readback(cli, detail) == detail
    cli.json.assert_called_once_with(
        ["agent", "list", "--tasks", "--page-size", "20", "--max-pages", "5"]
    )


@pytest.mark.parametrize("fault", ["duplicate", "gap", "truncated"])
def test_coding_replay_refuses_incomplete_or_duplicate_initial_history(
    tmp_path, monkeypatch, fault
):
    module, common = shipped_script(tmp_path, "hosted_coding")
    task = "tsk-owned"
    frames = [
        coding_stream_frame(task, 1),
        coding_stream_frame(task, 2),
        coding_stream_frame(task, 3, "task.cancelled"),
    ]
    if fault == "duplicate":
        frames.insert(0, frames[0])
    elif fault == "gap":
        frames.pop(1)
    else:
        frames.pop(0)
    _, events = module.durable_events(
        "\n".join(json.dumps(frame) for frame in frames), task
    )
    monkeypatch.setattr(
        common,
        "bounded",
        lambda *args, **kwargs: pytest.fail("Do not reconnect a known invalid history"),
    )
    detail = {
        "task_id": task,
        "status": "cancelled",
        "latest_event_cursor": task + ":3",
        "oldest_event_cursor": task + ":1",
    }
    with pytest.raises(common.RemoteError):
        module.verify_replay({"cli_path": "/served/adp"}, {}, detail, events)


def test_tenant_isolation_dispatch_fixture_reaches_evaluate_and_recovery():
    from tests.e2e.cli_uplift.fixtures import parse

    value = {"tenant_isolation": {"tenant_ids": ["adp-platform", "aws-e"]}}
    env = {"CLI_UPLIFT_EVAL_FIXTURES": json.dumps(value)}
    evaluated = config.from_environment(env, base=dict(VALID))
    recovered = config.from_environment(env, base=dict(VALID))
    assert evaluated == recovered
    assert evaluated["tenant_isolation"] == value["tenant_isolation"]
    assert parse(json.dumps(value)) == value
    assert cases.TENANT_ISOLATION in preflight.evaluate_fixtures(evaluated)
    assert {row.id for row in cases.resolve_suites(["tenant-isolation"])} == {"E27"}


@pytest.mark.parametrize(
    "fixture",
    [
        {},
        {"tenant_ids": []},
        {"tenant_ids": ["one"]},
        {"tenant_ids": ["one", "two", "three"]},
        {"tenant_ids": ["same", "same"]},
        {"tenant_ids": "one,two"},
        {"tenant_ids": ["one", 2]},
        {"tenant_ids": ["one", {}]},
        {"tenant_ids": ["one", ""]},
        {"tenant_ids": ["one", "a" * 129]},
        {"tenant_ids": ["one", "https://other"]},
        {"tenant_ids": ["one", "two"], "gateway_url": "https://other"},
        {"tenant_ids": ["one", "two"], "password": "not-allowed"},
        {"tenant_ids": ["one", "two"], "owned_mutations_authorized": True},
    ],
)
def test_tenant_isolation_dispatch_refuses_invalid_or_extra_fields(fixture):
    from tests.e2e.cli_uplift.fixtures import parse

    with pytest.raises(config.ConfigError):
        parse(json.dumps({"tenant_isolation": fixture}))


@pytest.mark.parametrize("running_at,expected", [(88, "running"), (181, "queued")])
def test_coding_running_control_allows_recovery_delay_but_stays_bounded(
    tmp_path, monkeypatch, running_at, expected
):
    module, _ = shipped_script(tmp_path, "hosted_coding")
    elapsed = [0]
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])

    def sleep(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(module.time, "sleep", sleep)

    class Cli:
        def json(self, argv):
            calls.append(argv)
            return {
                "detail": {
                    "task_id": "tsk-owned",
                    "status": "running" if elapsed[0] >= running_at else "queued",
                }
            }

    evidence = {}
    assert (
        module.observe_before_control(
            Cli(),
            "tsk-owned",
            {"control_when": "running", "running_wait_seconds": 180},
            evidence,
        )
        == expected
    )
    assert elapsed[0] == min(running_at, 180)
    assert evidence["observed_states"][60] == "queued"
    assert evidence["pre_control_status"] == expected
    assert all(argv == ["agent", "status", "--run", "tsk-owned"] for argv in calls)


@pytest.mark.parametrize(
    "seconds,valid",
    [(1, True), (180, True), (181, False), (0, False), (True, False), (180.0, False)],
)
def test_coding_running_wait_validation_matches_worker(tmp_path, seconds, valid):
    from tests.e2e.cli_uplift.fixtures import parse

    module, _ = shipped_script(tmp_path, "hosted_coding")
    fixture = {
        **diagnostic_fixture("human_task_coding"),
        "scenario": "cancel",
        "control_when": "running",
        "running_wait_seconds": seconds,
    }
    assert module.fixture_valid(fixture) is valid
    payload = json.dumps({"human_task_coding": fixture})
    if valid:
        assert parse(payload)["human_task_coding"] == fixture
    else:
        with pytest.raises(config.ConfigError, match="Running wait"):
            parse(payload)


def cancellation_cli_response(status="accepted"):
    return {
        "type": "abort_receipt",
        "data": {
            "terminal_cancellation_confirmed": False,
            "receipt": {
                "command_id": "owned-command",
                "kind": "cancel",
                "status": status,
                "handoff": "not_started",
                "command_sequence": 3,
                "accepted_at": "2026-09-26T05:00:00Z",
            },
        },
    }


def test_coding_cancellation_replays_one_command_and_conflicts_changed_payload(
    tmp_path,
):
    module, _ = shipped_script(tmp_path, "hosted_coding")
    calls = []

    class Cli:
        def json(self, argv, expected):
            calls.append((argv, expected))
            if len(calls) == 1:
                return {
                    "status": "pending",
                    "detail": {
                        "task_id": "tsk-owned",
                        **cancellation_cli_response()["data"]["receipt"],
                    },
                }
            if len(calls) == 4:
                return {"status": "failed", "error": {"code": "task_conflict"}}
            return cancellation_cli_response(
                "cancelled" if len(calls) > 1 else "accepted"
            )

    result = module.cancel_with_replay(Cli(), "tsk-owned", "owned-command")
    assert result["same_payload_replay"] == "confirmed"
    assert result["changed_payload"] == "task_conflict"
    assert calls[1] == calls[2] == calls[4]
    assert calls[0][0][:4] == ["agent", "abort", "--run", "tsk-owned"]
    assert calls[0][0][calls[0][0].index("--reason") + 1] == module.CANCEL_REASON
    assert result["initial_cli"] == "adp agent abort"
    assert all(
        argv[:4] == ["task", "abort", "tsk-owned", "--human-login"]
        for argv, _ in calls[1:]
    )
    assert all(
        argv[argv.index("--command-id") + 1] == "owned-command" for argv, _ in calls
    )
    assert calls[3][0][calls[3][0].index("--reason") + 1] != module.CANCEL_REASON
    assert calls[3][1] == 5


@pytest.mark.parametrize(
    "fault",
    [
        "preflight_success",
        "foreign_command",
        "different_sequence",
        "conflict_success",
        "unrelated_error",
    ],
)
def test_coding_cancellation_replay_rejects_false_proof(tmp_path, fault):
    module, common = shipped_script(tmp_path, "hosted_coding")
    calls = []

    class Cli:
        def json(self, argv, expected):
            calls.append(argv)
            if len(calls) == 1:
                return {
                    "status": "pending",
                    "detail": {
                        "task_id": "tsk-owned",
                        **cancellation_cli_response()["data"]["receipt"],
                    },
                }
            response = cancellation_cli_response()
            if len(calls) == 3:
                if fault == "preflight_success":
                    response = {
                        "type": "abort_confirmed",
                        "data": {"terminal_cancellation_confirmed": True},
                    }
                elif fault == "foreign_command":
                    response["data"]["receipt"]["command_id"] = "foreign"
                elif fault == "different_sequence":
                    response["data"]["receipt"]["command_sequence"] = 4
            if len(calls) == 4:
                if fault == "conflict_success":
                    return response
                return {"status": "failed", "error": {"code": "task_access_denied"}}
            return response

    with pytest.raises(common.RemoteError):
        module.cancel_with_replay(Cli(), "tsk-owned", "owned-command")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing_receipt",
        "duplicate_receipt",
        "wrong_status",
        "wrong_kind",
        "wrong_sequence",
        "child_running",
        "recovery",
        "queue_pending",
        "foreign_task",
        "completed",
    ],
)
def test_coding_cancellation_requires_terminal_command_and_queue_ack(
    tmp_path, monkeypatch, fault
):
    module, common = shipped_script(tmp_path, "hosted_coding")
    receipt = cancellation_cli_response("cancelled")["data"]["receipt"]
    original = {
        key: receipt[key]
        for key in ("command_id", "kind", "command_sequence", "accepted_at")
    }
    detail = {
        "task_id": "tsk-owned",
        "status": "cancelled",
        "queue_ack_status": "confirmed",
        "recovery_required": False,
        "error": {"child_exit_confirmed": True, "recovery_required": False},
        "command_receipts": [receipt],
    }
    if fault == "missing_receipt":
        detail["command_receipts"] = []
    if fault == "duplicate_receipt":
        detail["command_receipts"] *= 2
    if fault == "wrong_status":
        receipt["status"] = "accepted"
    if fault == "wrong_kind":
        receipt["kind"] = "input"
    if fault == "wrong_sequence":
        receipt["command_sequence"] += 1
    if fault == "child_running":
        detail["error"]["child_exit_confirmed"] = False
    if fault == "recovery":
        detail["recovery_required"] = True
    if fault == "queue_pending":
        detail["queue_ack_status"] = "pending"
    if fault == "foreign_task":
        detail["task_id"] = "tsk-foreign"
    if fault == "completed":
        detail["status"] = "completed"
    ticks = iter([0, 31])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))

    class Cli:
        def json(self, argv, expected):
            assert argv == module.cancellation_command("tsk-owned", "owned-command")
            assert expected == 4
            return cancellation_cli_response("cancelled")

    if fault:
        with pytest.raises(common.RemoteError):
            module.confirm_cancellation(
                Cli(), detail, "tsk-owned", "owned-command", original
            )
    else:
        _, evidence = module.confirm_cancellation(
            Cli(), detail, "tsk-owned", "owned-command", original
        )
        assert evidence["queue_ack_status"] == "confirmed"
        assert evidence["terminal_same_payload_replay"] == "confirmed"


@pytest.mark.parametrize("fault", ["foreign_task", "missing_ack", "different_identity"])
def test_coding_agent_cancel_must_bind_initial_task_and_replayed_receipt(
    tmp_path, fault
):
    module, common = shipped_script(tmp_path, "hosted_coding")
    calls = []

    class Cli:
        def json(self, argv, expected):
            calls.append(argv)
            if len(calls) == 1:
                detail = {
                    "task_id": "tsk-owned",
                    **cancellation_cli_response()["data"]["receipt"],
                }
                if fault == "foreign_task":
                    detail["task_id"] = "tsk-foreign"
                if fault == "different_identity":
                    detail["command_sequence"] = 99
                return {
                    "status": "failed" if fault == "missing_ack" else "pending",
                    "detail": detail,
                }
            return cancellation_cli_response()

    with pytest.raises(common.RemoteError):
        module.cancel_with_replay(Cli(), "tsk-owned", "owned-command")
    assert len(calls) == (2 if fault == "different_identity" else 1)
    assert calls[0][:2] == ["agent", "abort"]


@pytest.mark.parametrize("fault", [None, "login", "owner", "tenant", "membership"])
@pytest.mark.parametrize("selected_run", [False, True])
def test_usage_tenant_fixture_checks_identity_before_exports(
    tmp_path, monkeypatch, fault, selected_run
):
    script, common = shipped_script(tmp_path, "story_reads")
    cfg = {
        "mode": "usage",
        "cli_path": "/served/adp",
        "gateway_url": "https://gateway.example",
        "region": "us-east-1",
        "sts_endpoint": "https://sts.us-east-1.amazonaws.com",
        "org_id": "native",
        "test_user_id": "login",
        "usage_tenant": {
            "login_user_id": "login",
            "canonical_user_id": "owner",
            "tenant_id": "selected",
        },
    }
    if selected_run:
        cfg["usage_tenant"]["usage_run_id"] = "57e3ed64-0794-4df0-828f-565479f9ddac"
    if fault == "login":
        cfg["test_user_id"] = "wrong"
    monkeypatch.setattr(common, "load_session", lambda _: {"org_id": "native"})
    monkeypatch.setattr(script, "_write_session", lambda *args: None)
    observed = []

    class Cli:
        def __init__(self, binary, env, transcript, **kwargs):
            assert env["ADP_TENANT"] == "selected"
            assert "ADP_TENANT_ID" not in env
            assert env["BG_CONFIG_DIR"].startswith(env["HOME"])

        def json(self, args):
            observed.append(args)
            if fault == "membership":
                raise common.RemoteError("Membership refused")
            return {
                "status": "ok",
                "detail": {
                    "principal_id": "wrong" if fault == "owner" else "owner",
                    "tenant_id": "wrong" if fault == "tenant" else "selected",
                },
            }

    monkeypatch.setattr(common, "Cli", Cli)
    monkeypatch.setitem(
        script.SCENARIOS, "usage", lambda cli, ev: observed.append("exports")
    )
    evidence = {"transcript": []}
    if fault:
        with pytest.raises(common.RemoteError):
            script.execute(cfg, evidence)
        assert "exports" not in observed
    else:
        script.execute(cfg, evidence)
        assert observed == [["models", "mappings", "list"], "exports"]
        assert evidence["tenant_selection"] == "verified_existing_membership_fixture"
        assert evidence["usage_owner"] == {"org_id": "selected", "user_id": "owner"}
        assert evidence.get("usage_run_id") == cfg["usage_tenant"].get("usage_run_id")
    assert cfg["org_id"] == "native"


@pytest.mark.parametrize("fault", [None, "missing", "extra", "secret"])
def test_usage_tenant_fixture_allows_only_explicit_identity(fault):
    from tests.e2e.cli_uplift.fixtures import parse

    fixture = {
        "login_user_id": "login",
        "canonical_user_id": "owner",
        "tenant_id": "selected",
    }
    if fault == "missing":
        fixture.pop("canonical_user_id")
    elif fault == "extra":
        fixture["org_id"] = "override"
    elif fault == "secret":
        fixture["access_token"] = "value"
    if fault:
        with pytest.raises(config.ConfigError):
            parse(json.dumps({"usage_tenant": fixture}))
    else:
        assert parse(json.dumps({"usage_tenant": fixture}))["usage_tenant"] == fixture


@pytest.mark.parametrize(
    "scope",
    [
        {"org_id": "wrong", "user_id": "owner"},
        {"org_id": "selected", "user_id": "wrong"},
    ],
)
def test_usage_verified_owner_refuses_scope_drift(tmp_path, scope):
    script, common = shipped_script(tmp_path, "story_reads")
    with pytest.raises(common.RemoteError, match="verified owner"):
        script.require_usage_owner(
            {"scope": scope},
            {"usage_owner": {"org_id": "selected", "user_id": "owner"}},
        )


@pytest.mark.parametrize(
    "run_id",
    [
        "57e3ed64-0794-4df0-828f-565479f9ddac",
        "",
        "not-a-uuid",
        "tsk_123",
        "57E3ED64-0794-4DF0-828F-565479F9DDAC",
        None,
    ],
)
def test_usage_run_fixture_requires_exact_invocation_uuid(run_id):
    from tests.e2e.cli_uplift.fixtures import parse

    fixture = {
        "login_user_id": "login",
        "canonical_user_id": "owner",
        "tenant_id": "tenant",
        "usage_run_id": run_id,
    }
    if run_id == "57e3ed64-0794-4df0-828f-565479f9ddac":
        assert parse(json.dumps({"usage_tenant": fixture}))["usage_tenant"] == fixture
    else:
        with pytest.raises(config.ConfigError):
            parse(json.dumps({"usage_tenant": fixture}))


@pytest.mark.parametrize("fault", [None, "coverage", "request_row", "export_row"])
def test_usage_run_filters_every_read_and_refuses_unrelated_rows(
    tmp_path, monkeypatch, fault
):
    script, common = shipped_script(tmp_path, "story_reads")
    monkeypatch.setitem(script.require_selected_run.__globals__, "common", common)
    run = "57e3ed64-0794-4df0-828f-565479f9ddac"
    calls = []

    def reply(args, export=False):
        calls.append(args)
        assert args[args.index("--run") + 1] == run
        row = args[:2] in (["usage", "requests"], ["logs", "list"], ["logs", "export"])
        wrong = (export and fault == "export_row") or (
            not export and fault == "request_row"
        )
        return {
            "status": "ok",
            "detail": {
                "scope": {
                    "kind": "own",
                    "org_id": "tenant",
                    "user_id": "owner",
                    "coverage": "direct_identity_records"
                    if fault == "coverage"
                    else "selected_run",
                },
                "items": [{"invocation_id": "other" if wrong else run}]
                if row
                else [{"count": 1}],
                "complete": True,
            },
        }

    cli = Mock()
    cli.json.side_effect = reply
    cli.run.side_effect = lambda args, **kwargs: (0, reply(args, export=True))
    serialized = []
    monkeypatch.setattr(
        script,
        "exercise_usage_exports",
        lambda cli, flags, evidence: serialized.append(flags),
    )
    evidence = {
        "usage_run_id": run,
        "usage_owner": {"org_id": "tenant", "user_id": "owner"},
    }
    if fault:
        with pytest.raises(common.RemoteError):
            script.usage(cli, evidence)
        assert not serialized
    else:
        script.usage(cli, evidence)
        assert len(calls) == 6
        assert serialized[0][serialized[0].index("--run") + 1] == run


def test_coding_command_ids_match_task_uuid4_contract_and_retain_identity():
    import uuid
    from tests.e2e.cli_uplift.remote.coding_plan import recovery_plan

    config = {
        "evaluation_id": "coding-command-contract",
        "gateway_url": "https://gateway",
        "human_task_coding": diagnostic_fixture("human_task_coding"),
    }
    plan = recovery_plan(config)
    assert plan == recovery_plan(config)
    assert plan["schema"] == "hosted-coding-recovery-v2"
    for key in ("command_id", "cleanup_command_id"):
        value = uuid.UUID(plan[key])
        assert value.version == 4 and value.variant == uuid.RFC_4122
        assert str(value) == plan[key]
    assert plan["command_id"] != plan["cleanup_command_id"]
    different = recovery_plan({**config, "evaluation_id": "another-run"})
    assert different["command_id"] != plan["command_id"]
    assert different["cleanup_command_id"] != plan["cleanup_command_id"]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_artifact_download_recovers_bounded_transient_edge_error(monkeypatch, status):
    import io
    import urllib.error
    from tests.e2e.cli_uplift import ports

    attempts, delays = [], []

    class Response(io.BytesIO):
        status = 200

    def download(url, **kwargs):
        attempts.append(url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(
                url,
                status,
                "edge unavailable",
                {},
                io.BytesIO(b"private provider body"),
            )
        return Response(b"immutable CLI artifact")

    monkeypatch.setattr(ports.urllib.request, "urlopen", download)
    monkeypatch.setattr(ports.time, "sleep", delays.append)
    assert (
        ports.HttpPort().get_bytes("https://gateway/cli/adp")
        == b"immutable CLI artifact"
    )
    assert len(attempts) == 2 and delays == [1]


@pytest.mark.parametrize(
    "status,expected_attempts",
    [(502, 3), (503, 3), (504, 3), (403, 1), (404, 1), (500, 1)],
)
def test_artifact_download_persistent_failure_stays_failed(
    monkeypatch, status, expected_attempts
):
    import io
    import urllib.error
    from tests.e2e.cli_uplift import ports

    attempts, delays = [], []

    def download(url, **kwargs):
        attempts.append(url)
        raise urllib.error.HTTPError(
            url, status, "edge unavailable", {}, io.BytesIO(b"private provider body")
        )

    monkeypatch.setattr(ports.urllib.request, "urlopen", download)
    monkeypatch.setattr(ports.time, "sleep", delays.append)
    with pytest.raises(ports.PortError, match=f"HTTP {status}") as error:
        ports.HttpPort().get_bytes("https://gateway/cli/adp")
    assert len(attempts) == expected_attempts
    assert delays == ([1, 2] if expected_attempts == 3 else [])
    assert "private provider body" not in str(error.value)


def test_preproduction_bindings_keep_evaluation_and_cleanup_in_target_account():
    cfg = config.from_environment(
        {
            "CLI_UPLIFT_EVAL_BINDINGS": "tests/e2e/cli_uplift/bindings.pre-production.json",
            "CLI_UPLIFT_EVAL_EXPECTED_REVISION": "a" * 40,
        }
    )
    assert cfg["platform_account"] == "615296308642"
    assert cfg["gateway_url"] == "https://dw4gomrsecmzn.cloudfront.net/api"
    assert cfg["state_bucket"].endswith(cfg["platform_account"])
    assert cfg["cognito_user_pool_id"] == "us-east-1_PpSUITvxb"
    document, _ = workflow()
    for job in ("evaluate", "recover"):
        guard = next(
            step
            for step in document["jobs"][job]["steps"]
            if step.get("name")
            == "Assert the role resolved to the approved platform account"
        )
        assert 'config.from_environment(os.environ)["platform_account"]' in guard["run"]
        assert "config.example.json" not in guard["run"]
