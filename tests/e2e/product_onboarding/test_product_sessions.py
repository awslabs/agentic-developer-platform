"""Private cancellation-session retention, renewal and wrong-owner refusals."""

from copy import deepcopy
from email.message import Message
import hashlib
import io
import json
from types import SimpleNamespace
import urllib.request

import pytest

from tests.e2e.cli_uplift import bundle, cleanup, ports, stages, statestore
from tests.e2e.product_onboarding import product_execution, product_sessions


@pytest.fixture
def recovery_session(tmp_path, monkeypatch):
    evaluation_id = "adp-e2e-20261020-113000-abcdef"
    cfg = {
        # The reviewed base URL shape the run config publishes: a gateway origin
        # that carries the external `/api` prefix CloudFront strips. A fixture
        # using a bare host could not observe the base-path refusal that failed
        # install_auth in run adp-e2e-20261022-200315-8f1eb8.
        "gateway_url": "https://gateway.example.invalid/api",
        "platform_account": "000000000000",
        "region": "us-east-1",
        "expected_revision": "a" * 40,
    }
    # Two identity namespaces that are never interchangeable: the Cognito
    # SUBJECT the gateway attributes the session to (`/auth/cli/admin-session`
    # answers with it) and the ADP `users` ROW the run created and therefore
    # owns and cleans up. Fixtures that reuse one string for both cannot see the
    # ownership defect that failed adp-e2e-20261022-122136-174d4b.
    session = {
        "user_id": "bootstrap-subject",
        "adp_user_id": "bootstrap-row",
        "org_id": "bootstrap-tenant",
        "created_username": evaluation_id + "-admin@adp-eval.example",
        "session_ref": bundle.REMOTE_DIR + "/session.json",
    }
    manifest = cleanup.Manifest(tmp_path / "manifest.json", evaluation_id)
    manifest.record("adp_user", "bootstrap-tenant/bootstrap-row")
    ctx = {
        "evaluation_id": evaluation_id,
        "manifest": manifest,
        "document": {"instance_id": "i-00000001", "session": session},
    }
    private = {
        **session,
        "access_token": "private-access",
        "refresh_token": "private-refresh",
    }
    objects = {}
    writes = []

    def aws_call(service, operation, **kwargs):
        assert service == "s3"
        if operation == "put_object":
            assert kwargs["ServerSideEncryption"] == "aws:kms"
            assert kwargs["ACL"] == "bucket-owner-full-control"
            writes.append(kwargs["Key"])
            objects[kwargs["Key"]] = kwargs["Body"]
            return {}
        assert operation == "get_object"
        if kwargs["Key"] not in objects:
            raise ports.PortError("NoSuchKey")
        return {"Body": io.BytesIO(objects[kwargs["Key"]])}

    store = statestore.S3StateStore(SimpleNamespace(call=aws_call), "private-bucket")
    ssm = SimpleNamespace(
        json_result=lambda *_args, **_kwargs: ({"Status": "Success"}, private)
    )

    def request(_cfg, path, *, token=None, body=None):
        assert path == "/auth/cli/admin-session" and token == private["access_token"]
        return 200, {
            "verified": True,
            "role": "platform_admin",
            "user_id": session["user_id"],
            "org_id": session["org_id"],
        }

    monkeypatch.setattr(product_sessions, "request", request)
    return cfg, ctx, store, ssm, objects, writes, private


class _Response:
    """A minimal observed response: the helper reads `status` and a bounded body."""

    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self, limit=None):
        return self._body[:limit] if limit else self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _opener(sent, response):
    class Opener:
        def __init__(self, handlers):
            self.handlers = handlers

        def open(self, outgoing, timeout=None):
            sent.append((outgoing, timeout, self.handlers))
            return response

    return lambda *handlers: Opener(handlers)


@pytest.mark.parametrize(
    "gateway_url,expected",
    [
        (
            "https://gateway.example.invalid/api",
            "https://gateway.example.invalid/api/auth/cli/admin-session",
        ),
        (
            "https://gateway.example.invalid/api/",
            "https://gateway.example.invalid/api/auth/cli/admin-session",
        ),
        (
            "https://gateway.example.invalid/api/v2",
            "https://gateway.example.invalid/api/v2/auth/cli/admin-session",
        ),
        (
            "https://gateway.example.invalid",
            "https://gateway.example.invalid/auth/cli/admin-session",
        ),
    ],
)
def test_request_sends_mounted_paths_under_the_reviewed_base(
    monkeypatch, gateway_url, expected
):
    # The reviewed CLI configuration contract publishes a gateway BASE URL that
    # may carry a path (`config.example.json` and the private pre-production
    # bindings both publish `https://<host>/api`, which CloudFront strips), and
    # every consumer composes `base + <mounted app path>`. These assertions pin
    # the URL actually transmitted, so neither refusing the reviewed base path
    # (which failed install_auth in adp-e2e-20261022-200315-8f1eb8) nor silently
    # discarding it can pass again.
    sent = []
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        _opener(sent, _Response(body=b'{"verified": true}')),
    )
    status, document = product_sessions.request(
        {"gateway_url": gateway_url},
        "/auth/cli/admin-session",
        token="private-access",
    )
    assert (status, document) == (200, {"verified": True})
    assert len(sent) == 1
    outgoing, timeout, handlers = sent[0]
    assert outgoing.full_url == expected
    assert outgoing.get_header("Authorization") == "Bearer private-access"
    assert timeout == 30
    # Retained transport scope: no proxy may interpose on the bearer token and a
    # redirect is never followed to another origin.
    proxies = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
    redirects = [
        h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
    ]
    assert len(proxies) == 1 and proxies[0].proxies == {}
    assert len(redirects) == 1
    assert (
        redirects[0].redirect_request(
            outgoing,
            _Response(status=302),
            302,
            "Found",
            Message(),
            "https://elsewhere.invalid/auth/cli/admin-session",
        )
        is None
    )


@pytest.mark.parametrize(
    "gateway_url",
    [
        "http://gateway.example.invalid/api",
        "https://user:pw@gateway.example.invalid/api",
        "https://gateway.example.invalid/api?probe=1",
        "https://gateway.example.invalid/api#fragment",
        "https://gateway.example.invalid/api/../admin",
        "https://gateway.example.invalid//api",
        "https://gateway.example.invalid/api%2f..%2fadmin",
        "https://gateway.example.invalid/api admin",
        "https:///api",
        "",
    ],
)
def test_request_refuses_origins_outside_the_reviewed_contract(
    monkeypatch, gateway_url
):
    # Honouring the reviewed base path is not the same as accepting any origin:
    # a plaintext scheme, embedded credentials, a query, a fragment, traversal,
    # an empty segment, a percent-encoded separator, whitespace and a missing
    # host must still never carry a private session token, and the refusal has
    # to happen before any request is opened.
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: pytest.fail("A refused Gateway origin reached the network"),
    )
    with pytest.raises(ports.PortError, match="reviewed Gateway origin"):
        product_sessions.request(
            {"gateway_url": gateway_url},
            "/auth/cli/admin-session",
            token="private-access",
        )


def test_private_session_survives_instance_loss_without_entering_report(
    recovery_session,
):
    cfg, ctx, store, ssm, objects, writes, private = recovery_session
    before = deepcopy(ctx["document"])
    product_execution.recovery_available(store)
    product_sessions.retain(cfg, ctx, store, ssm)
    restored = product_sessions.restore(cfg, ctx, store)
    assert restored["access_token"] == private["access_token"]
    retained = store._get(product_sessions.key(ctx["evaluation_id"]))["binding"]
    # Both identifiers are retained, and the ownership key is the database row.
    assert retained["user_id"] == "bootstrap-subject"
    assert retained["adp_user_id"] == "bootstrap-row"
    assert retained["org_id"] + "/" + retained["adp_user_id"] == (
        "bootstrap-tenant/bootstrap-row"
    )
    assert ctx["document"] == before
    assert private["access_token"] not in json.dumps(ctx["document"])
    assert writes == [product_sessions.key(ctx["evaluation_id"])]
    product_sessions.clear(ctx, store)
    assert private["access_token"].encode() not in objects[writes[0]]


def test_expired_session_is_renewed_and_verified_before_cleanup(
    recovery_session, monkeypatch
):
    cfg, ctx, store, ssm, _objects, _writes, private = recovery_session
    product_sessions.retain(cfg, ctx, store, ssm)
    calls = []

    def request(_cfg, path, *, token=None, body=None):
        calls.append(path)
        if path == "/auth/cli/refresh":
            assert body == {"refresh_token": private["refresh_token"]}
            return 200, {
                "access_token": "renewed-access",
                "refresh_token": "renewed-refresh",
            }
        if token == private["access_token"]:
            return 401, {}
        assert (
            store._get(product_sessions.key(ctx["evaluation_id"]))["refresh_token"]
            == "renewed-refresh"
        )
        return 200, {
            "verified": True,
            "role": "platform_admin",
            "user_id": "bootstrap-subject",
            "org_id": "bootstrap-tenant",
        }

    monkeypatch.setattr(product_sessions, "request", request)
    assert product_sessions.restore(cfg, ctx, store)["access_token"] == "renewed-access"
    assert calls == [
        "/auth/cli/admin-session",
        "/auth/cli/refresh",
        "/auth/cli/admin-session",
    ]


def test_product_owner_backup_is_separate_from_bootstrap(recovery_session, monkeypatch):
    cfg, ctx, store, ssm, _objects, _writes, _private = recovery_session
    product_sessions.retain(cfg, ctx, store, ssm)
    bootstrap = store._get(product_sessions.key(ctx["evaluation_id"]))
    owner = {
        "user_id": "owned-subject",
        "adp_user_id": "owned-row",
        "org_id": "owned-workspace",
        "created_username": ctx["evaluation_id"] + "-product-admin@adp-eval.example",
        "session_ref": bundle.REMOTE_DIR + "/product-actor/session.json",
    }
    ctx["document"]["product_actor_session"] = owner
    ctx["manifest"].record("adp_user", "owned-workspace/owned-row")
    private = {
        **owner,
        "access_token": "owner-private-access",
        "refresh_token": "owner-private-refresh",
    }
    ssm = SimpleNamespace(
        json_result=lambda *_args, **_kwargs: ({"Status": "Success"}, private)
    )
    monkeypatch.setattr(
        product_sessions,
        "request",
        lambda *_args, **_kwargs: (
            200,
            {
                "verified": True,
                "role": "platform_admin",
                "user_id": owner["user_id"],
                "org_id": owner["org_id"],
            },
        ),
    )
    product_sessions.retain(cfg, ctx, store, ssm, owner=True)
    restored = product_sessions.restore(cfg, ctx, store, owner=True)
    assert restored["user_id"] == "owned-subject"
    assert restored["adp_user_id"] == "owned-row"
    assert restored["access_token"] == private["access_token"]
    owner_binding = store._get(product_sessions.key(ctx["evaluation_id"], owner=True))[
        "binding"
    ]
    assert owner_binding["user_id"] == "owned-subject"
    assert owner_binding["adp_user_id"] == "owned-row"
    assert owner_binding["adp_user_id"] != bootstrap["binding"]["adp_user_id"]
    assert store._get(product_sessions.key(ctx["evaluation_id"])) == bootstrap
    assert "owner-private" not in json.dumps(ctx["document"])


@pytest.mark.parametrize(
    "change",
    [
        "instance",
        "actor",
        "source",
        "missing",
        "write_lost",
        "wrong_row",
        "wrong_org",
        "missing_row",
        "subject_as_row",
    ],
)
def test_wrong_or_unretained_session_refuses(recovery_session, monkeypatch, change):
    cfg, ctx, store, ssm, objects, _writes, _private = recovery_session
    if change == "write_lost":
        monkeypatch.setattr(store, "_put", lambda *_args: None)
        with pytest.raises(ports.PortError, match="durably retained"):
            product_sessions.retain(cfg, ctx, store, ssm)
        return
    product_sessions.retain(cfg, ctx, store, ssm)
    session = ctx["document"]["session"]
    if change == "instance":
        ctx["document"]["instance_id"] = "i-00000002"
    elif change == "actor":
        session["user_id"] = "another-subject"
    elif change == "source":
        cfg["expected_revision"] = "b" * 40
    elif change == "wrong_row":
        # A row this run never created: claiming somebody else's account is
        # worse than claiming none, so refuse before any credential is used.
        session["adp_user_id"] = "another-row"
    elif change == "wrong_org":
        session["org_id"] = "another-tenant"
    elif change == "missing_row":
        del session["adp_user_id"]
    elif change == "subject_as_row":
        # The original defect: the Cognito subject published where the ADP row
        # id belongs. It must refuse instead of silently claiming ownership.
        session["adp_user_id"] = session["user_id"]
    else:
        objects.clear()
    monkeypatch.setattr(
        product_sessions,
        "request",
        lambda *_args, **_kwargs: pytest.fail("mismatched session used"),
    )
    with pytest.raises(ports.PortError, match="ownership|matching private session"):
        product_sessions.restore(cfg, ctx, store)


@pytest.mark.parametrize("attributed", ["bootstrap-row", "another-subject"])
def test_retention_refuses_session_attributed_to_another_subject(
    recovery_session, monkeypatch, attributed
):
    cfg, ctx, store, ssm, _objects, _writes, _private = recovery_session
    # `/auth/cli/admin-session` answers with TokenContext.user_id, the Cognito
    # subject. An answer carrying the ADP row id is not the owner either: the
    # subject check has to stay a subject check instead of accepting whichever
    # identifier happens to be echoed back.
    monkeypatch.setattr(
        product_sessions,
        "request",
        lambda *_args, **_kwargs: (
            200,
            {
                "verified": True,
                "role": "platform_admin",
                "user_id": attributed,
                "org_id": "bootstrap-tenant",
            },
        ),
    )
    with pytest.raises(ports.PortError, match="attribution differs"):
        product_sessions.retain(cfg, ctx, store, ssm)


@pytest.mark.parametrize("change", ["missing_row", "missing_subject", "swapped"])
def test_retention_refuses_instance_document_that_loses_an_identifier(
    recovery_session, change
):
    cfg, ctx, store, ssm, _objects, _writes, private = recovery_session
    # The on-instance vault is the only place that holds both identities, so a
    # document that drops or interchanges them cannot be retained for recovery.
    if change == "missing_row":
        del private["adp_user_id"]
    elif change == "missing_subject":
        del private["user_id"]
    else:
        private["user_id"], private["adp_user_id"] = (
            private["adp_user_id"],
            private["user_id"],
        )
    with pytest.raises(ports.PortError, match="could not be verified"):
        product_sessions.retain(cfg, ctx, store, ssm)
    assert store._get(product_sessions.key(ctx["evaluation_id"])) is None


def test_missing_recovery_store_keeps_write_guard():
    with pytest.raises(ValueError, match="writes refuse"):
        product_execution.recovery_available(None)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_recovery_session_never_follows_authenticated_redirects(monkeypatch, status):
    transport = product_sessions.urllib.request
    original_opener = transport.build_opener
    requests = []

    class Redirect(transport.BaseHandler):
        handler_order = 100

        def https_open(self, request):
            requests.append(request)
            headers = Message()
            headers["Location"] = "https://other.example.invalid/stolen"
            return self.parent.error(
                "http", request, io.BytesIO(b""), status, "redirect", headers
            )

    monkeypatch.setattr(
        transport,
        "build_opener",
        lambda *handlers: original_opener(Redirect(), *handlers),
    )
    with pytest.raises(ports.PortError, match="authentication was unavailable"):
        product_sessions.request(
            {"gateway_url": "https://gateway.example.invalid"},
            "/auth/cli/admin-session",
            token="private-access",
        )
    assert len(requests) == 1
    assert (
        requests[0].full_url == "https://gateway.example.invalid/auth/cli/admin-session"
    )
    assert requests[0].get_header("Authorization") == "Bearer private-access"


@pytest.mark.parametrize(
    "failure", [None, "not_terminated", "missing_session", "archive_failed"]
)
def test_cancellation_stops_worker_then_retires_and_cleans_owned_resources(
    recovery_session,
    monkeypatch,
    failure,
):
    cfg, ctx, store, ssm, objects, _writes, _private = recovery_session
    workflow_id = "product-123456-2"
    evaluation_id = ctx["evaluation_id"]
    cfg.update(
        product_run_id=workflow_id,
        product_cases="api_product_lifecycle",
        internal_invoke_url="https://private.execute-api.us-east-1.amazonaws.com/dev",
    )
    workspace = (
        evaluation_id
        + "-product-"
        + hashlib.sha256((evaluation_id + ":product-workspace").encode()).hexdigest()[
            :24
        ]
    )
    ctx.update(fault=None, matrix={})
    manifest = ctx["manifest"]
    location = {"account": cfg["platform_account"], "region": cfg["region"]}
    for kind, identifier in [
        ("ec2_instance", "i-00000001"),
        ("adp_org", workspace),
        ("adp_user", workspace + "/owned-row"),
        ("cognito_user", "owned-pool/owned-user"),
        ("secret", "owned-secret"),
    ]:
        manifest.record(kind, identifier, **location)
    manifest.record("adp_org", "shared-organization", reused=True, **location)
    manifest.record(
        "product_fixture",
        workflow_id,
        detail={
            "tenant_id": workspace,
            "actor_id": "owned-subject",
            "instance_id": "i-00000001",
            "source_revision": cfg["expected_revision"],
            "idempotency_key": "product-create-"
            + hashlib.sha256((workflow_id + ":" + evaluation_id).encode()).hexdigest(),
        },
        **location,
    )
    product_sessions.retain(cfg, ctx, store, ssm)
    if failure == "missing_session":
        objects.clear()
    actions = []

    def caller(service, operation, **_kwargs):
        if service == "sts":
            return {
                "Account": cfg["platform_account"],
                "Arn": "arn:aws:sts::000000000000:assumed-role/adp-cli-uplift-eval-orchestrator/recovery",
            }
        assert (service, operation) == ("ec2", "describe_instances")
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-00000001",
                            "State": {
                                "Name": "running"
                                if failure == "not_terminated"
                                else "terminated"
                            },
                        }
                    ]
                }
            ]
        }

    def deleters(_cfg, context):
        assert context["product_recovery_session"]["access_token"] == "private-access"

        def delete(identifier, **_location):
            assert identifier != "shared-organization"
            actions.append(identifier)
            if failure == "archive_failed" and identifier == workspace:
                raise ports.PortError("workspace unavailable")

        return {
            kind: delete
            for kind in (
                "ec2_instance",
                "adp_org",
                "adp_user",
                "secret",
                "cognito_user",
            )
        }

    wired = {
        "aws": SimpleNamespace(call=caller),
        "deleters": deleters,
        "run_worker": lambda *_args, **_kwargs: pytest.fail(
            "cancelled worker was reused"
        ),
    }
    monkeypatch.setattr(product_execution.evaluator, "wire", lambda *_args: wired)
    monkeypatch.setattr(
        product_execution.stages,
        "build_stages",
        lambda selected, ports: {"cleanup": stages.cleanup_stage(selected, ports)},
    )

    def retire(_cfg, _aws, intent):
        assert actions == ["i-00000001"]
        actions.append("retired")
        return {
            "success": True,
            "stage": "complete",
            "detail": {
                "run_id": intent["run_id"],
                "fixture_removed": True,
                "product_id": "owned-product",
                "project_id": "owned-project",
            },
        }

    monkeypatch.setattr(product_execution, "retire_independently", retire)
    mapping = product_execution.build_stages(
        cfg,
        {"source_revision": cfg["expected_revision"]},
        workflow_id,
        store=store,
        recovering=True,
    )
    if failure in {"missing_session", "not_terminated"}:
        with pytest.raises(ports.PortError):
            mapping["cleanup"](ctx)
        assert "retired" not in actions
        assert any(
            entry["kind"] == "product_fixture" for entry in manifest.outstanding()
        )
    else:
        assert mapping["cleanup"](ctx) is (failure is None)
        if failure == "archive_failed":
            assert "bootstrap-tenant/bootstrap-row" not in actions
            assert "owned-pool/owned-user" not in actions
            assert "owned-secret" not in actions
        else:
            # Owned ADP rows are retired by their database-row identifier; a
            # sweep keyed on the Cognito subject would report a clean run while
            # leaving the real row behind.
            assert workspace + "/owned-row" in actions
            assert "bootstrap-tenant/bootstrap-row" in actions
            assert not [entry for entry in actions if "subject" in entry]
            assert manifest.outstanding() == []
            assert store._get(product_sessions.key(evaluation_id)) == {"retired": True}
