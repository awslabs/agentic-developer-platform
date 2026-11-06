"""Product HTTP on the evaluator uses only the private run-created session."""

import ast
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "remote"))
import common
import install_auth
import product_http

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
LIVE = HERE / "live.py"
EXECUTION = REPO / "tests" / "e2e" / "product_onboarding" / "product_execution.py"
PROFILE = HERE / "remote" / "product_profile.py"
INSTALL_AUTH = HERE / "remote" / "install_auth.py"
INSTALLED_CLI = REPO / "modules" / "gateway" / "cli" / "adp-product.py"
APPROVED_BASE = "https://gateway.example.invalid/api"

# Every remote module is dispatched with a private session reference, a work
# directory and a region, and every one builds its child environment with
# `common.clean_env`. A payload of that shape therefore has to carry the
# approved endpoints as well; the successful `install_auth` baseline always did,
# while the Product payloads did not and died on the instance with
# `KeyError: sts_endpoint` (evaluation adp-e2e-20261022-210646-18ee57).
DISPATCH_KEYS = {"work_dir", "region", "session_ref"}
APPROVED_ENDPOINTS = {"sts_endpoint", "secrets_endpoint"}
RECEIPT_KEYS = {"tenant_id", "actor_id", "actor_row_id"}


def _dict_keys(path):
    """String key sets of every dict literal in a shipped source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            yield {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }


def _route_literals(path):
    """Leading literal text of every absolute path spelled in a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        text = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr) and node.values:
            head = node.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                text = head.value
        if isinstance(text, str) and text.startswith("/"):
            yield text


@pytest.mark.parametrize("source", [LIVE, EXECUTION], ids=["live", "execution"])
def test_remote_dispatch_payloads_carry_the_approved_endpoints(source):
    payloads = [keys for keys in _dict_keys(source) if DISPATCH_KEYS <= keys]
    assert payloads, f"no remote dispatch payload recognised in {source.name}"
    for keys in payloads:
        missing = sorted(APPROVED_ENDPOINTS - keys)
        assert not missing, f"{source.name} dispatch payload omits {missing}"


def test_clean_env_pins_the_approved_sts_endpoint_and_keeps_it_required():
    config = {
        "region": "us-east-1",
        "sts_endpoint": "https://sts-fips.us-east-1.amazonaws.com",
        "secrets_endpoint": "https://secretsmanager-fips.us-east-1.amazonaws.com",
    }
    env = common.clean_env(config, ADP_SESSION_REF="ref")
    assert env["AWS_ENDPOINT_URL_STS"] == config["sts_endpoint"]
    assert env["AWS_DEFAULT_REGION"] == "us-east-1"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["ADP_SESSION_REF"] == "ref"
    # The contract stays strict: a dispatch payload without the approved
    # endpoint is a setup failure, never a fall back to a public endpoint.
    with pytest.raises(KeyError):
        common.clean_env({"region": "us-east-1"})


def test_product_profile_addresses_the_installed_cli_product_routes():
    installed = INSTALLED_CLI.read_text(encoding="utf-8")
    assert "/v1/products" in installed, "installed CLI product prefix not found"
    assert "/api/v1/products" not in installed
    spelled = [path for path in _route_literals(PROFILE) if "products" in path]
    assert spelled, "no Product route spelled in product_profile.py"
    for path in spelled:
        # The readback appends this to the approved base, which already carries
        # the external `/api` prefix CloudFront strips, and the fault relay
        # matches it against what the installed CLI actually sends.
        assert path.startswith("/v1/products"), path
        assert "/api/api" not in APPROVED_BASE + path


def test_identity_routes_keep_their_real_api_mount():
    identity = [
        path for path in _route_literals(INSTALL_AUTH) if "admin/identity" in path
    ]
    assert identity, "the identity routes vanished from install_auth"
    for path in identity:
        # The identity router genuinely mounts under `/api`, as the successful
        # install_auth baseline proves. Only the Product routes were double
        # prefixed, so nothing here may be stripped.
        assert path.startswith("/api/admin/identity"), path


def test_v01_receipt_guard_matches_the_real_producer_detail():
    produced = [keys for keys in _dict_keys(EXECUTION) if RECEIPT_KEYS <= keys]
    expected = [keys for keys in _dict_keys(LIVE) if RECEIPT_KEYS <= keys]
    assert len(produced) == 1, "ambiguous product_fixture receipt producer"
    assert len(expected) == 1, "ambiguous product_fixture receipt consumer"
    # Both identity namespaces survive: the Cognito subject the Product API
    # attributed the create to, and the owned ADP `users` row the sweep has to
    # delete. The guard still compares exactly, so an unexpected extra field
    # fails it; this keeps producer and consumer from drifting apart again.
    assert "actor_id" in produced[0] and "actor_row_id" in produced[0]
    assert produced[0] == expected[0]


@pytest.fixture
def session_config(tmp_path):
    tmp_path.chmod(0o700)
    run_id = "adp-e2e-20261019-000000-abcdef"
    identity = {
        "created_username": f"{run_id}-product-admin@adp-eval.example",
        "user_id": "owned-actor",
        "org_id": "owned-tenant",
        "access_token": "private-session",
    }
    bootstrap = common.save_session(
        {
            "created_username": f"{run_id}-admin@adp-eval.example",
            "user_id": "bootstrap-admin",
            "org_id": "shared-bootstrap",
            "access_token": "bootstrap-token",
        },
        work_dir=tmp_path,
    )
    actor_dir = tmp_path / "product-actor"
    actor_dir.mkdir(mode=0o700)
    actor = common.save_session(identity, work_dir=actor_dir)
    return {
        "work_dir": str(tmp_path),
        "session_ref": bootstrap["session_ref"],
        "product_actor_session_ref": actor["session_ref"],
        "evaluation_id": run_id,
        # The reviewed run config publishes a gateway BASE URL carrying the
        # external `/api` prefix CloudFront strips, so the on-instance client
        # built by `local_owner` has to accept and keep that base.
        "gateway_url": "https://gateway.example.invalid/api",
    }


def test_instance_session_and_workspace_bind_product_http(session_config, monkeypatch):
    reads = []

    def read(config, path, token, *, expect):
        reads.append((path, token))
        if path == "/auth/cli/admin-session":
            return 200, {
                "verified": True,
                "role": "platform_admin",
                "user_id": "owned-actor",
                "org_id": "owned-tenant",
            }
        return 200, {
            "id": "owned-tenant",
            "name": f"eval-regression-{session_config['evaluation_id']}",
        }

    monkeypatch.setattr(common, "api", read)
    session, factory = product_http.local_owner(session_config)
    assert session["user_id"] == "owned-actor"
    assert (
        json.loads(Path(session_config["session_ref"]).read_text())["access_token"]
        == "bootstrap-token"
    )
    assert session_config["session_ref"] != session_config["product_actor_session_ref"]
    assert reads == [
        ("/auth/cli/admin-session", "private-session"),
        ("/api/admin/identity/organizations/owned-tenant", "private-session"),
    ]
    with factory(session["access_token"]) as client:
        assert client.origin == session_config["gateway_url"]
    with pytest.raises(common.RemoteError, match="differs"):
        with factory("other-token"):
            pytest.fail("Foreign actor reached the Product API")


@pytest.mark.parametrize(
    "tamper", [None, "wrong_tenant", "wrong_user", "revoked", "shared"]
)
def test_foreign_product_session_requires_matching_run_created_actor(
    session_config, monkeypatch, tamper
):
    session_config["other_tenant_id"] = "shared-bootstrap"
    if tamper == "shared":
        bootstrap = Path(session_config["session_ref"])
        document = json.loads(bootstrap.read_text())
        document["created_username"] = "shared-operator@example.invalid"
        bootstrap.write_text(json.dumps(document))
    if tamper == "wrong_tenant":
        session_config["other_tenant_id"] = "different-tenant"
    reads = []

    def read(_config, path, token, *, expect):
        reads.append((path, token))
        if path == "/auth/cli/admin-session":
            if token == "bootstrap-token":
                if tamper == "revoked":
                    return 401, {}
                return 200, {
                    "verified": True,
                    "role": "platform_admin",
                    "user_id": "wrong-user"
                    if tamper == "wrong_user"
                    else "bootstrap-admin",
                    "org_id": "shared-bootstrap",
                }
            return 200, {
                "verified": True,
                "role": "platform_admin",
                "user_id": "owned-actor",
                "org_id": "owned-tenant",
            }
        return 200, {
            "id": "owned-tenant",
            "name": f"eval-regression-{session_config['evaluation_id']}",
        }

    monkeypatch.setattr(common, "api", read)
    if tamper:
        with pytest.raises(common.RemoteError, match="Foreign Product actor"):
            product_http.local_owner(session_config, allow_foreign=True)
        return
    session, client_factory = product_http.local_owner(
        session_config, allow_foreign=True
    )
    with client_factory(session["access_token"]) as owner:
        assert owner.token == "private-session"
    with client_factory("bootstrap-token") as foreign:
        assert foreign.token == "bootstrap-token"
    with pytest.raises(common.RemoteError, match="differs"):
        with client_factory("revoked-token"):
            pytest.fail("Unverified actor reached the Product API")
    assert reads[-1] == ("/auth/cli/admin-session", "bootstrap-token")


def test_installed_actor_session_reaches_product_without_replacing_bootstrap(
    session_config, tmp_path, monkeypatch
):
    bootstrap_file = Path(session_config["session_ref"])
    bootstrap_bytes = bootstrap_file.read_bytes()
    actor_file = Path(session_config["product_actor_session_ref"])
    actor_file.unlink()

    home = tmp_path / "actor-home"
    tokens = home / ".bedrock-gateway"
    tokens.mkdir(parents=True)
    (tokens / "tokens.json").write_text('{"access_token":"private-session"}')
    (tokens / "config.json").write_text("{}")
    prefix = tmp_path / "installed-cli"
    prefix.mkdir()
    binary = prefix / "adp"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)

    actor_config = {
        **session_config,
        "work_dir": str(actor_file.parent),
        "created_username": (
            f"{session_config['evaluation_id']}-product-admin@adp-eval.example"
        ),
    }
    evidence = {
        "login": {
            "username": actor_config["created_username"],
            "user_id": "owned-actor",
            "org_id": "owned-tenant",
        }
    }
    install_auth._session_document(
        actor_config, evidence, prefix, home, actor_file.parent
    )
    session_config["product_actor_session_ref"] = evidence["session"]["session_ref"]
    assert actor_file.read_bytes() != bootstrap_bytes
    assert bootstrap_file.read_bytes() == bootstrap_bytes
    assert json.loads(actor_file.read_text())["access_token"] == "private-session"
    assert actor_file.stat().st_mode & 0o777 == 0o600
    assert "private-session" not in json.dumps(evidence)
    assert "private-session" not in json.dumps(common.redact(evidence))
    assert "bootstrap-token" not in json.dumps(common.redact(evidence))

    def read(_config, path, token, *, expect):
        assert token == "private-session" and expect == (200,)
        if path == "/auth/cli/admin-session":
            return 200, {
                "verified": True,
                "role": "platform_admin",
                "user_id": "owned-actor",
                "org_id": "owned-tenant",
            }
        assert path == "/api/admin/identity/organizations/owned-tenant"
        return 200, {
            "id": "owned-tenant",
            "name": f"eval-regression-{session_config['evaluation_id']}",
        }

    monkeypatch.setattr(common, "api", read)
    session, factory = product_http.local_owner(session_config)
    assert session["user_id"] == "owned-actor"
    assert session["created_username"] == actor_config["created_username"]
    with factory(session["access_token"]) as client:
        assert client.origin == session_config["gateway_url"]


@pytest.mark.parametrize(
    "bad",
    [
        "shared",
        "bootstrap",
        "foreign_user",
        "foreign_tenant",
        "foreign_workspace",
        "wrong_role",
    ],
)
def test_refuses_stale_or_unowned_actor_before_product_http(
    session_config, monkeypatch, bad
):
    identity = json.loads(Path(session_config["product_actor_session_ref"]).read_text())
    if bad in {"shared", "bootstrap"}:
        identity["created_username"] = (
            f"{session_config['evaluation_id']}-admin@adp-eval.example"
            if bad == "bootstrap"
            else "shared-login@adp-eval.example"
        )
        common.save_session(
            identity, work_dir=Path(session_config["work_dir"]) / "product-actor"
        )

    def read(_config, path, _token, *, expect):
        if bad in {"shared", "bootstrap"}:
            pytest.fail("Non-Product actor caused Gateway read")
        if path == "/auth/cli/admin-session":
            return 200, {
                "verified": True,
                "role": "org_admin" if bad == "wrong_role" else "platform_admin",
                "user_id": "other" if bad == "foreign_user" else "owned-actor",
                "org_id": "other" if bad == "foreign_tenant" else "owned-tenant",
            }
        return 200, {
            "id": "owned-tenant",
            "name": (
                "shared-platform"
                if bad == "foreign_workspace"
                else f"eval-regression-{session_config['evaluation_id']}"
            ),
        }

    monkeypatch.setattr(common, "api", read)
    with pytest.raises(common.RemoteError, match="actor|owner|workspace"):
        product_http.local_owner(session_config)


@pytest.mark.parametrize(
    "bad",
    [
        "missing_actor_ref",
        "foreign_actor_ref",
        "bootstrap_reused",
        "missing_bootstrap",
        "actor_symlink",
        "actor_world_readable",
        "actor_dir_world_readable",
    ],
)
def test_actor_vault_refuses_missing_or_unprotected_session(
    session_config, monkeypatch, bad
):
    actor_file = Path(session_config["product_actor_session_ref"])
    if bad == "missing_actor_ref":
        del session_config["product_actor_session_ref"]
    elif bad == "foreign_actor_ref":
        session_config["product_actor_session_ref"] = session_config["session_ref"]
    elif bad == "bootstrap_reused":
        actor = json.loads(actor_file.read_text())
        actor.update(
            user_id="bootstrap-admin",
            org_id="shared-bootstrap",
            access_token="bootstrap-token",
        )
        actor_file.write_text(json.dumps(actor))
    elif bad == "missing_bootstrap":
        Path(session_config["session_ref"]).unlink()
    elif bad == "actor_symlink":
        actor_file.unlink()
        actor_file.symlink_to(session_config["session_ref"])
    elif bad == "actor_world_readable":
        actor_file.chmod(0o644)
    else:
        actor_file.parent.chmod(0o755)
    monkeypatch.setattr(
        common,
        "api",
        lambda *_args, **_kwargs: pytest.fail("Unsafe Product vault reached Gateway"),
    )
    with pytest.raises(common.RemoteError, match="private session|fresh"):
        product_http.local_owner(session_config)


def test_product_client_keeps_bearer_local_and_forwards_keys(monkeypatch):
    sent = []

    class Response:
        status = 201
        headers = {"Profile-Revision": "1"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _limit):
            return b'{"revision":1}'

    class Opener:
        def open(self, request, *, timeout):
            sent.append((request, timeout))
            return Response()

    monkeypatch.setattr(
        product_http.urllib.request, "build_opener", lambda *_args: Opener()
    )
    # The reviewed base URL the run config publishes: it already carries the
    # external `/api` prefix CloudFront strips, so the mounted app paths below
    # are appended to it unchanged and the transmitted URLs asserted here are
    # the ones the gateway actually serves.
    client = product_http.ProductClient(
        "https://gateway.example.invalid/api", "private-session"
    )
    result = client.post(
        "/v1/products",
        json={"name": "owned"},
        headers={"Idempotency-Key": "original-key"},
    )
    assert result.status_code == 201 and result.headers["Profile-Revision"] == "1"
    assert result.json() == {"revision": 1}
    request, timeout = sent[0]
    assert timeout == 60
    assert request.full_url == "https://gateway.example.invalid/api/v1/products"
    assert request.get_header("Authorization") == "Bearer private-session"
    assert request.get_header("Idempotency-key") == "original-key"
    assert json.loads(request.data) == {"name": "owned"}
    updated = client.patch(
        "/v1/products/owned-product/projects/owned-project",
        json={"name": "updated"},
        headers={"Idempotency-Key": "edit-original", "If-Profile-Revision": "1"},
    )
    assert updated.status_code == 201
    assert sent[1][0].get_method() == "PATCH"
    assert sent[1][0].get_header("If-profile-revision") == "1"
    client.get("/v1/products/owned-product/projects/owned-project/changes")
    client.get("/v1/products/owned-product/projects/owned-project/revisions/1")
    assert [request.get_method() for request, _ in sent[2:]] == ["GET", "GET"]


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_product_client_does_not_follow_bearer_redirects(monkeypatch, status):
    requests = []
    build_opener = urllib.request.build_opener

    class RedirectingHTTPS(urllib.request.HTTPSHandler):
        def https_open(self, request):
            requests.append(request)
            response = urllib.response.addinfourl(
                io.BytesIO(b""),
                {"Location": "https://other.example.invalid/collect"},
                request.full_url,
                code=status,
            )
            response.msg = "redirect"
            return response

    monkeypatch.setattr(
        product_http.urllib.request,
        "build_opener",
        lambda *handlers: build_opener(*handlers, RedirectingHTTPS()),
    )
    client = product_http.ProductClient(
        "https://gateway.example.invalid/api", "private-session"
    )
    result = client.get("/v1/products")
    assert result.status_code == status
    assert len(requests) == 1
    assert requests[0].full_url == "https://gateway.example.invalid/api/v1/products"
    assert requests[0].get_header("Authorization") == "Bearer private-session"


@pytest.mark.parametrize(
    "method,path,headers",
    [
        ("DELETE", "/v1/products", {}),
        ("GET", "/v1/products/../admin", {}),
        ("GET", "/v1/products?other=true", {}),
        ("GET", "/v1/products/owned/projects/owned/revisions/../admin", {}),
        ("POST", "/v1/products/owned/projects/owned", {}),
        ("PATCH", "/v1/products/owned/projects/owned/../admin", {}),
        ("POST", "/v1/products", {"Authorization": "Bearer foreign"}),
    ],
)
def test_product_http_refuses_requests_outside_lifecycle_scope(
    method, path, headers, monkeypatch
):
    monkeypatch.setattr(
        product_http.urllib.request,
        "build_opener",
        lambda *_args: pytest.fail("Out-of-scope Product request was transmitted"),
    )
    client = product_http.ProductClient(
        "https://gateway.example.invalid", "private-session"
    )
    with pytest.raises(common.RemoteError, match="outside|headers"):
        client.request(
            method,
            path,
            json_body={"name": "owned"} if method in {"POST", "PATCH"} else None,
            headers=headers,
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://gateway.example.invalid/api",
        "https://user:pw@gateway.example.invalid/api",
        "https://gateway.example.invalid/api?probe=1",
        "https://gateway.example.invalid/api#fragment",
        "https://gateway.example.invalid/api/../admin",
        "https://gateway.example.invalid//api",
        "https://gateway.example.invalid/api%2f..%2fadmin",
        "https:///api",
    ],
)
def test_product_client_refuses_origins_outside_the_reviewed_contract(origin):
    # The reviewed base path is honoured (the URL assertions above transmit it),
    # but only a reviewed one: a plaintext scheme, embedded credentials, a query,
    # a fragment, traversal, an empty segment, a percent-encoded separator and a
    # missing host must never become the Product API origin.
    with pytest.raises(common.RemoteError, match="HTTPS Gateway origin"):
        product_http.ProductClient(origin, "private-session")


def test_product_http_refuses_wrong_origin_path_or_unobserved_response(monkeypatch):
    client = product_http.ProductClient(
        "https://gateway.example.invalid/api", "private-session"
    )
    with pytest.raises(common.RemoteError, match="outside"):
        client.get("/v1/admin/identity")

    class Opener:
        def open(self, _request, *, timeout):
            raise urllib.error.URLError("no response")

    monkeypatch.setattr(
        product_http.urllib.request, "build_opener", lambda *_args: Opener()
    )
    with pytest.raises(OSError, match="not observed"):
        client.get("/v1/products")
    with pytest.raises(common.RemoteError, match="response exceeds"):
        product_http.ProductResponse(200, {}, io.BytesIO(b"x" * 65537).read())
