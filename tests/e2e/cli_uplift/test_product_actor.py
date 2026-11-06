"""A separate Product actor is provisioned only for a verified run-owned workspace."""

import json
import subprocess

import pytest

from tests.e2e.cli_uplift import cleanup, live, ports


RUN_ID = "adp-e2e-20261020-113000-abcdef"
WORKSPACE = RUN_ID + "-product-" + "a" * 24
ACCOUNT = "000000000000"
REGION = "us-east-1"


class RecordingAws:
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.fail_operation = None

    def call(self, service, operation, **kwargs):
        self.events.append(("call", operation))
        self.calls.append((service, operation, kwargs))
        if operation == self.fail_operation:
            raise RuntimeError("AWS response lost")
        if operation == "create_secret":
            return {
                "ARN": "arn:aws:secretsmanager:us-east-1:000000000000:secret:fixture"
            }
        return {}


@pytest.fixture
def prepared(tmp_path):
    events = []
    manifest = cleanup.Manifest(
        tmp_path / "run.json",
        RUN_ID,
        on_change=lambda document, *, critical: events.append(
            ("persist", document["resources"][-1]["kind"], critical)
        ),
    )
    manifest.record("adp_org", WORKSPACE, account=ACCOUNT, region=REGION)
    events.clear()
    aws = RecordingAws(events)
    cfg = {
        "cognito_user_pool_id": "us-east-1_example",
        "platform_account": ACCOUNT,
        "region": REGION,
        "instance_profile": "example-eval-role",
        "fixture_org_id": "shared-platform",
    }
    ctx = {
        "evaluation_id": RUN_ID,
        "manifest": manifest,
        "product_workspace_evidence": {
            "success": True,
            "stage": "complete",
            "workspace_id": WORKSPACE,
            "team_id": "owned-team",
        },
    }
    return aws, events, cfg, ctx


def test_product_actor_uses_owned_tenant_and_private_secret(prepared):
    aws, events, cfg, ctx = prepared
    fixture = live._admin_fixtures(aws, cfg, product_workspace_id=WORKSPACE)(ctx)
    username = RUN_ID + "-product-admin@adp-eval.example"
    assert fixture == {
        "created_username": username,
        "credential_secret": f"adp/cli-uplift-eval/{RUN_ID}-product-fixtures",
    }
    created = next(kwargs for _, op, kwargs in aws.calls if op == "admin_create_user")
    attributes = {item["Name"]: item["Value"] for item in created["UserAttributes"]}
    assert attributes["custom:org_id"] == WORKSPACE
    assert attributes["custom:role"] == "platform_admin"
    assert created["Username"] == username
    assert created["MessageAction"] == "SUPPRESS"
    secret = next(kwargs for _, op, kwargs in aws.calls if op == "create_secret")
    document = json.loads(secret["SecretString"])
    assert set(document) == {"admin_username", "admin_password", "admin_new_password"}
    assert document["admin_username"] == username
    assert document["admin_password"] not in json.dumps(fixture)
    assert secret["Tags"] == [{"Key": cleanup.OWNER_TAG, "Value": RUN_ID}]
    assert [operation for _, operation, _ in aws.calls] == [
        "admin_create_user",
        "admin_add_user_to_group",
        "create_secret",
        "put_resource_policy",
    ]
    assert events.index(("persist", "cognito_user", True)) < events.index(
        ("call", "admin_create_user")
    )
    assert events.index(("persist", "secret", True)) < events.index(
        ("call", "create_secret")
    )
    assert [entry["kind"] for entry in ctx["manifest"].outstanding()] == [
        "secret",
        "cognito_user",
        "adp_org",
    ]


def test_denial_fixture_provisions_separate_run_owned_non_admin(prepared):
    aws, events, cfg, ctx = prepared
    cfg["product_cases"] = "api_product_lifecycle,api_denial_oracle"
    fixture = live._admin_fixtures(aws, cfg, product_workspace_id=WORKSPACE)(ctx)
    username = RUN_ID + "-product-user@adp-eval.example"
    assert fixture["non_admin_username"] == username
    created = [
        kwargs for _, operation, kwargs in aws.calls if operation == "admin_create_user"
    ]
    assert len(created) == 2
    assert created[1]["Username"] == username
    attributes = {item["Name"]: item["Value"] for item in created[1]["UserAttributes"]}
    assert attributes["custom:org_id"] == WORKSPACE
    assert "custom:role" not in attributes
    assert created[1]["MessageAction"] == "SUPPRESS"
    password_change = next(
        kwargs
        for _, operation, kwargs in aws.calls
        if operation == "admin_set_user_password"
    )
    assert password_change["Username"] == username
    assert password_change["Permanent"] is True
    secret = next(
        kwargs for _, operation, kwargs in aws.calls if operation == "create_secret"
    )
    document = json.loads(secret["SecretString"])
    assert document["non_admin_username"] == username
    assert document["non_admin_password"] == password_change["Password"]
    assert document["non_admin_password"] not in json.dumps(fixture)
    assert events.index(("persist", "cognito_user", True)) < events.index(
        ("call", "admin_create_user")
    )
    assert [
        entry["id"]
        for entry in ctx["manifest"].outstanding()
        if entry["kind"] == "cognito_user"
    ] == [
        f"us-east-1_example/{RUN_ID}-product-admin@adp-eval.example",
        f"us-east-1_example/{username}",
    ]


def test_product_actor_is_distinct_from_existing_e02_administrator(prepared):
    aws, _events, cfg, ctx = prepared
    ordinary = live._admin_fixtures(aws, cfg)(ctx)
    product = live._admin_fixtures(aws, cfg, product_workspace_id=WORKSPACE)(ctx)
    assert ordinary["created_username"] == RUN_ID + "-admin@adp-eval.example"
    assert ordinary["non_admin_username"] == RUN_ID + "-user@adp-eval.example"
    assert ordinary["credential_secret"] == f"adp/cli-uplift-eval/{RUN_ID}-fixtures"
    assert product["created_username"] != ordinary["created_username"]
    assert product["credential_secret"] != ordinary["credential_secret"]
    users = {
        kwargs["Username"]: {
            attr["Name"]: attr["Value"] for attr in kwargs["UserAttributes"]
        }
        for _, op, kwargs in aws.calls
        if op == "admin_create_user"
    }
    assert users[ordinary["created_username"]]["custom:org_id"] == "shared-platform"
    assert users[product["created_username"]]["custom:org_id"] == WORKSPACE
    assert len([op for _, op, _ in aws.calls if op == "create_secret"]) == 2


@pytest.mark.parametrize(
    "wrong",
    [
        "workspace",
        "evidence",
        "team",
        "run",
        "manifest",
        "durability",
        "account",
        "replay",
    ],
)
def test_product_actor_refuses_unverified_or_previously_attempted_workspace(
    prepared, wrong, tmp_path
):
    aws, _events, cfg, ctx = prepared
    workspace = WORKSPACE
    if wrong == "workspace":
        workspace = "shared-platform"
    elif wrong == "evidence":
        ctx["product_workspace_evidence"]["workspace_id"] = "other"
    elif wrong == "team":
        ctx["product_workspace_evidence"]["team_id"] = ""
    elif wrong == "run":
        ctx["evaluation_id"] = "adp-e2e-20261020-113000-bbbbbb"
    elif wrong == "manifest":
        ctx["manifest"] = None
    elif wrong == "durability":
        replacement = cleanup.Manifest(tmp_path / "non-durable.json", RUN_ID)
        replacement.record("adp_org", WORKSPACE, account=ACCOUNT, region=REGION)
        ctx["manifest"] = replacement
    elif wrong == "account":
        ctx["manifest"] = cleanup.Manifest(
            tmp_path / "foreign.json",
            RUN_ID,
            on_change=lambda *_args, **_kwargs: None,
        )
        ctx["manifest"].record(
            "adp_org", WORKSPACE, account="000000000001", region=REGION
        )
    else:
        ctx["manifest"].record(
            "cognito_user",
            f"{cfg['cognito_user_pool_id']}/{RUN_ID}-product-admin@adp-eval.example",
        )
    with pytest.raises(ports.PortError, match="Product actor"):
        live._admin_fixtures(aws, cfg, product_workspace_id=workspace)(ctx)
    assert aws.calls == []


def test_lost_response_keeps_ownership_and_refuses_second_create(prepared):
    aws, _events, cfg, ctx = prepared
    aws.fail_operation = "admin_create_user"
    actor = live._admin_fixtures(aws, cfg, product_workspace_id=WORKSPACE)
    with pytest.raises(RuntimeError, match="response lost"):
        actor(ctx)
    assert [entry["kind"] for entry in ctx["manifest"].outstanding()] == [
        "cognito_user",
        "adp_org",
    ]
    with pytest.raises(ports.PortError, match="Product actor"):
        actor(ctx)
    assert len(aws.calls) == 1


def _actor_dispatch(prepared, cases):
    aws, _events, cfg, ctx = prepared
    cfg["gateway_url"] = "https://gateway.example.invalid"
    cfg["product_cases"] = cases
    fixture = live._admin_fixtures(aws, cfg, product_workspace_id=WORKSPACE)(ctx)
    ctx["manifest"].record(
        "ec2_instance", "owned-instance", account=ACCOUNT, region=REGION
    )
    if "api_denial_oracle" in cases.split(","):
        ctx["manifest"].record(
            "adp_user_email",
            WORKSPACE + "/" + fixture["non_admin_username"],
            account=ACCOUNT,
            region=REGION,
        )
    payload = {
        "evaluation_id": RUN_ID,
        "product_cases": cases,
        "product_workspace_id": WORKSPACE,
        "product_team_id": ctx["product_workspace_evidence"]["team_id"],
        "platform_account": ACCOUNT,
        "region": REGION,
        "gateway_url": cfg["gateway_url"],
        "instance_id": "owned-instance",
        "session_ref": "/home/ec2-user/adp-eval/session.json",
        "work_dir": "/home/ec2-user/adp-eval/product-actor",
        "cognito_user_pool_id": cfg["cognito_user_pool_id"],
        "expected_hashes": {"install.sh": "a" * 64},
        "login_required": True,
        "admin_challenges_required": True,
        "admin_setup_required": False,
        **fixture,
    }
    return aws, cfg, ctx, payload


@pytest.fixture
def dispatch(prepared):
    return _actor_dispatch(prepared, "api_product_lifecycle")


@pytest.fixture
def denial_dispatch(prepared):
    return _actor_dispatch(prepared, "api_product_lifecycle,api_denial_oracle")


def actor_receipt(payload):
    return {
        "success": True,
        "session": {
            "created_username": payload["created_username"],
            "session_ref": "/home/ec2-user/adp-eval/product-actor/session.json",
            "org_id": WORKSPACE,
            # The authenticated Cognito subject and the ADP `users` row this run
            # registered are two different identifiers. The receipt publishes
            # both so ownership can be keyed on the row while attribution stays
            # keyed on the subject.
            "user_id": "new-actor-subject",
            "adp_user_id": "new-actor-row",
        },
        "login": {
            "org_id": WORKSPACE,
            "user_id": "new-actor-subject",
            "role": "platform_admin",
        },
        "onboard": {
            "org_id": WORKSPACE,
            "team_id": "owned-team",
            "user_id": "new-actor-row",
            "cognito_sub_bound": True,
        },
    }


def test_protected_actor_dispatch_reads_verified_result_and_keeps_passwords_local(
    dispatch,
):
    from tests.e2e.cli_uplift import bundle

    aws, cfg, ctx, payload = dispatch
    calls = []

    class Ssm:
        def json_result(self, instance, commands, *, purpose, timeout):
            calls.append((instance, commands, purpose))
            return {"Status": "Success"}, actor_receipt(payload)

    runner = live._run_worker(Ssm(), cfg, lambda *_args: calls.append("install"))
    assert (
        runner("owned-instance", "product_actor", payload, manifest=ctx["manifest"])[
            "success"
        ]
        is True
    )
    assert calls[0] == "install"
    assert calls[1][0] == "owned-instance" and calls[1][2] == "product_actor"
    assert "product_actor" in bundle.purposes()
    commands = " ".join(calls[1][1])
    secret = next(
        kwargs for _, operation, kwargs in aws.calls if operation == "create_secret"
    )
    for key in ("admin_password", "admin_new_password"):
        assert json.loads(secret["SecretString"])[key] not in commands
    assert payload["credential_secret"] in commands
    assert commands.index("trap ") < commands.index("runuser ")
    assert "rm -f" in commands


def test_protected_actor_dispatch_refuses_failed_command_even_with_success_receipt(
    dispatch,
    tmp_path,
    monkeypatch,
):
    from tests.e2e.cli_uplift import bundle

    _aws, cfg, ctx, payload = dispatch
    monkeypatch.setattr(bundle, "REMOTE_DIR", str(tmp_path))
    payload["session_ref"] = str(tmp_path / "session.json")
    payload["work_dir"] = str(tmp_path / "product-actor")

    class Ssm:
        def json_result(self, _instance, commands, **_kwargs):
            failed = subprocess.run(
                ["sh", "-c", "\n".join([*commands[:4], "false"])],
                capture_output=True,
                text=True,
                check=False,
            )
            assert failed.returncode != 0
            assert not (tmp_path / "product_actor.json").exists()
            return {"Status": "Failed"}, actor_receipt(payload)

    runner = live._run_worker(Ssm(), cfg, lambda *_args: None)
    with pytest.raises(ports.PortError, match="did not complete successfully"):
        runner("owned-instance", "product_actor", payload, manifest=ctx["manifest"])


@pytest.mark.parametrize(
    "bad",
    [
        "manifest",
        "unowned_instance",
        "workspace",
        "unhashable_workspace",
        "selection",
        "origin",
        "actor",
        "secret",
        "session",
        "team",
        "challenge",
    ],
)
def test_protected_actor_dispatch_refuses_unowned_or_mismatched_payload_before_ssm(
    dispatch, bad
):
    from tests.e2e.cli_uplift import bundle

    _aws, cfg, ctx, payload = dispatch
    if bad == "manifest":
        ctx["manifest"] = None
    elif bad == "unowned_instance":
        ctx["manifest"].mark("ec2_instance", "owned-instance", cleanup.DELETED)
    elif bad == "workspace":
        payload["product_workspace_id"] = "shared-platform"
    elif bad == "unhashable_workspace":
        payload["product_workspace_id"] = []
    elif bad == "selection":
        payload["product_cases"] = "api_denial_oracle"
    elif bad == "origin":
        payload["gateway_url"] = "https://other.example.invalid"
    elif bad == "actor":
        payload["created_username"] = "shared@adp-eval.example"
    elif bad == "secret":
        payload["credential_secret"] = "other-secret"
    elif bad == "session":
        payload["work_dir"] = bundle.REMOTE_DIR
    elif bad == "team":
        payload["product_team_id"] = ""
    else:
        payload["admin_challenges_required"] = False
    touched = []
    runner = live._run_worker(object(), cfg, lambda *_args: touched.append("install"))
    with pytest.raises(ports.PortError, match="Product actor dispatch"):
        runner("owned-instance", "product_actor", payload, manifest=ctx["manifest"])
    assert touched == []


@pytest.mark.parametrize(
    "bad",
    [
        "wrong_org",
        "wrong_team",
        "wrong_actor",
        "failed",
        "missing_row",
        "row_is_subject",
        "unmapped_row",
        "foreign_row",
    ],
)
def test_protected_actor_dispatch_refuses_unverified_instance_receipt(dispatch, bad):
    _aws, cfg, ctx, payload = dispatch
    receipt = actor_receipt(payload)
    if bad == "wrong_org":
        receipt["session"]["org_id"] = "foreign"
    elif bad == "wrong_team":
        receipt["onboard"]["team_id"] = "foreign"
    elif bad == "wrong_actor":
        receipt["login"]["user_id"] = "foreign"
    elif bad == "missing_row":
        # Without the row id nothing the actor owns can be cleaned up later.
        del receipt["session"]["adp_user_id"]
    elif bad == "row_is_subject":
        # The repaired defect: the subject published as the row identifier.
        receipt["session"]["adp_user_id"] = receipt["session"]["user_id"]
    elif bad == "unmapped_row":
        # A row id the registration step never confirmed belongs to this login.
        receipt["onboard"]["user_id"] = "another-row"
    elif bad == "foreign_row":
        receipt["session"]["adp_user_id"] = "another-row"
    else:
        receipt["success"] = False

    class Ssm:
        def json_result(self, *_args, **_kwargs):
            return {"Status": "Success"}, receipt

    runner = live._run_worker(Ssm(), cfg, lambda *_args: None)
    with pytest.raises(ports.PortError, match="Product actor login"):
        runner("owned-instance", "product_actor", payload, manifest=ctx["manifest"])


def test_protected_denial_actor_dispatch_requires_owned_intent_and_verified_receipt(
    denial_dispatch,
):
    _aws, cfg, ctx, payload = denial_dispatch
    receipt = actor_receipt(payload)
    receipt["denial_actor"] = {
        "org_id": WORKSPACE,
        "user_id": "ordinary-actor",
        "username": payload["non_admin_username"],
        "cognito_sub_bound": True,
    }
    calls = []

    class Ssm:
        def json_result(self, _instance, _commands, *, purpose, timeout):
            calls.append(purpose)
            return {"Status": "Success"}, receipt

    runner = live._run_worker(Ssm(), cfg, lambda *_args: None)
    assert (
        runner("owned-instance", "product_actor", payload, manifest=ctx["manifest"])
        == receipt
    )
    assert calls == ["product_actor"]


@pytest.mark.parametrize(
    "wrong",
    [
        "intent",
        "cognito",
        "actor_name",
        "selection",
        "missing_receipt",
        "wrong_org",
        "wrong_name",
        "same_actor",
        "same_row",
        "wrong_sub",
    ],
)
def test_protected_denial_actor_refuses_missing_ownership_or_receipt(
    denial_dispatch, wrong
):
    _aws, cfg, ctx, payload = denial_dispatch
    receipt = actor_receipt(payload)
    receipt["denial_actor"] = {
        "org_id": WORKSPACE,
        "user_id": "ordinary-actor",
        "username": payload["non_admin_username"],
        "cognito_sub_bound": True,
    }
    if wrong == "intent":
        ctx["manifest"].mark(
            "adp_user_email",
            WORKSPACE + "/" + payload["non_admin_username"],
            cleanup.DELETED,
        )
    elif wrong == "cognito":
        ctx["manifest"].mark(
            "cognito_user",
            cfg["cognito_user_pool_id"] + "/" + payload["non_admin_username"],
            cleanup.DELETED,
        )
    elif wrong == "actor_name":
        payload["non_admin_username"] = "foreign-user@adp-eval.example"
    elif wrong == "selection":
        cfg["product_cases"] = "api_product_lifecycle"
    elif wrong == "missing_receipt":
        receipt.pop("denial_actor")
    elif wrong == "wrong_org":
        receipt["denial_actor"]["org_id"] = "foreign"
    elif wrong == "wrong_name":
        receipt["denial_actor"]["username"] = "foreign-user@adp-eval.example"
    elif wrong == "same_actor":
        receipt["denial_actor"]["user_id"] = receipt["session"]["user_id"]
    elif wrong == "same_row":
        # The denial oracle needs a genuinely separate account: reusing the
        # admin actor's database row is as useless as reusing its subject.
        receipt["denial_actor"]["user_id"] = receipt["session"]["adp_user_id"]
    else:
        receipt["denial_actor"]["cognito_sub_bound"] = False
    calls = []

    class Ssm:
        def json_result(self, *_args, **_kwargs):
            calls.append("ssm")
            return {"Status": "Success"}, receipt

    runner = live._run_worker(Ssm(), cfg, lambda *_args: calls.append("install"))
    with pytest.raises(ports.PortError, match="Product actor"):
        runner("owned-instance", "product_actor", payload, manifest=ctx["manifest"])
    if wrong in {"intent", "cognito", "actor_name", "selection"}:
        assert calls == []


def test_failed_install_auth_reports_its_own_error_before_session_retention():
    """A failed setup must not be reported as a missing session.

    Retaining the product session first asked `product_sessions.binding` for an
    ownership record that a FAILED install/auth never created, so the stage
    aborted with "Product recovery session has no original actor ownership" and
    the worker's real error was lost. The recorded case evidence must keep the
    worker's error, and retention must not run for a setup that did not complete.
    """
    from types import SimpleNamespace

    from tests.e2e.cli_uplift import cases as cases_module, stages

    recorded = []
    retained = []
    failure = {
        "success": False,
        "stage": "onboard",
        "error": "The organization's complete team inventory is unavailable: HTTP 404",
        "transcript": ["adp admin login"],
        "install": {"success": True, "hashes_match": True, "executable": True},
        "login": {"authenticated": True},
    }
    cfg = {
        "cognito_user_pool_id": "us-east-1_example",
        "platform_account": ACCOUNT,
        "region": REGION,
    }
    ctx = {
        "evaluation_id": RUN_ID,
        "document": {"instance_id": "i-0123456789abcdef0"},
        "preflight": {"expected_release": {"files": {"install.sh": "a" * 64}}},
        "matrix": {
            "C01": {"status": cases_module.NOT_RUN},
            "PR03-C01": {"status": cases_module.NOT_RUN},
        },
        "transcript": [],
        "manifest": SimpleNamespace(record=lambda *_args, **_kwargs: None),
        "record": lambda case_id, status, detail=None: recorded.append(
            (case_id, status, detail)
        ),
    }
    stage_ports = {
        "admin_fixtures": lambda _ctx: {},
        "worker_config": lambda *_args: {},
        "run_worker": lambda *_args, **_kwargs: failure,
        "retain_product_session": lambda _ctx: retained.append("retained"),
    }
    with pytest.raises(stages.StageError, match="HTTP 404"):
        stages.install_auth_stage(cfg, stage_ports)(ctx)
    assert retained == []
    assert ("C01", cases_module.FAILED) in [
        (case_id, status) for case_id, status, _detail in recorded
    ]
    assert any(
        (detail or {}).get("error") == failure["error"]
        for _case_id, _status, detail in recorded
    )


def test_successful_install_auth_still_retains_the_product_session():
    from types import SimpleNamespace

    from tests.e2e.cli_uplift import cases as cases_module, stages

    retained = []
    evidence = {
        "success": True,
        "install": {"success": True, "hashes_match": True, "executable": True},
        "login": {
            "authenticated": True,
            "bad_credentials_rejected": True,
            "refresh_succeeded": True,
            "user_id": "owned-sub",
        },
        "resources": [["adp_user", "owned-org/owned-user"]],
    }
    cfg = {
        "cognito_user_pool_id": "us-east-1_example",
        "platform_account": ACCOUNT,
        "region": REGION,
    }
    ctx = {
        "evaluation_id": RUN_ID,
        "document": {"instance_id": "i-0123456789abcdef0"},
        "preflight": {"expected_release": {"files": {"install.sh": "a" * 64}}},
        "matrix": {
            "C01": {"status": cases_module.NOT_RUN},
            "PR03-C01": {"status": cases_module.NOT_RUN},
        },
        "transcript": [],
        "manifest": SimpleNamespace(record=lambda *_args, **_kwargs: None),
        "record": lambda *_args, **_kwargs: None,
    }
    stage_ports = {
        "admin_fixtures": lambda _ctx: {},
        "worker_config": lambda *_args: {},
        "run_worker": lambda *_args, **_kwargs: evidence,
        "retain_product_session": lambda _ctx: retained.append("retained"),
    }
    stages.install_auth_stage(cfg, stage_ports)(ctx)
    assert retained == ["retained"]
