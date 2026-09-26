"""Every request the CLI can emit, checked against the servers that answer it (#5637).

This suite exists because `adp superplane` shipped with advertised commands that
could not work — not "worked differently than documented", could not reach a
route. Five emitted paths were invented by the helper, so the gateway's forwarding
allowlist 404'd them before the domain saw the request, and several bodies named
fields the domain's request models do not declare, which are 422s. The tests at the
time passed because each of them substituted a double for the thing that decides:
a recording API accepts any path, and a local `http.server` serves any path it is
told to.

So the checks here are made against the real authorities, imported rather than
restated:

* **Reachability** — `src.domain_proxy.superplane.ROUTES`, the gateway's own
  compiled allowlist, and `src.auth.vault_routes.router`, whose FastAPI path
  regexes decide ADP's own vault paths. Not copies: the same objects the running
  service matches against, so a path they would refuse is a path this suite
  refuses.
* **Acceptance** — the domain application's own Pydantic request models, imported
  from `modules/domain-apps/superplane/src/superplane-api`. A body is validated by
  the class the server validates it with, so a renamed or missing required field
  fails here with the same error the server would raise.
* **Query and path parameters** — read from the domain routers' source by AST,
  because those modules cannot be imported here (they pull in `jose`, which is not
  a gateway dependency). Reading the real `Query(...)` declarations is still the
  server's own statement of what it accepts; a restated list would be exactly the
  mistake this file exists to catch.

Requests are collected by RUNNING each command, not by reading the CLI's source. A
path assembled at runtime from a resolved identifier is the thing that has to be
correct, and only executing the verb produces it.

The second half of the file is one test per original mismatch (AC-04). Each asserts
both that the CLI now emits the right thing AND that what it used to emit is
rejected by the real authority, so reintroducing any single mismatch fails a named
test rather than a vague aggregate.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).parents[4]
CLI_DIR = REPO / "modules/gateway/cli"
ALLOWLIST_FILE = REPO / "modules/gateway/src/domain_proxy/superplane_routes.json"
DOMAIN_APP = REPO / "modules/domain-apps/superplane/src/superplane-api"
DOMAIN_ROUTERS = DOMAIN_APP / "app/routers"


@contextlib.contextmanager
def domain_app_importable():
    """Make the domain application's own schema modules importable here.

    Two obstacles, both incidental to what is being tested:

    * The domain app is a separate distribution with its own top-level `app`
      package, so its directory has to be on the path — but only for the
      duration of the import. It must not be left there: that directory also
      contains a `scripts/` package, which shadows the gateway's own `scripts/`
      for every later import in the same process. Leaving it on the path made
      `tests/lambda/test_mantle_budget_recovery.py` fail collection with
      `No module named 'scripts.backfill_mantle_budget_usage'` whenever this
      file was collected first.
    * `app.schemas.account` imports a validator from `app.models`, which imports
      `app.database`, which builds a SQLAlchemy engine at module scope with
      `pool_size`/`max_overflow`. Those are Postgres pool arguments that SQLite's
      StaticPool rejects with a TypeError, and this suite runs under a conftest
      that points DATABASE_URL at in-memory SQLite for the gateway's own tests.

    So the URL is restored to a Postgres one for the duration of the import.
    Nothing connects — `create_async_engine` only parses the URL and picks a
    dialect — and the original value is put back, so the gateway's own fixtures
    are unaffected.
    """
    added = str(DOMAIN_APP) not in sys.path
    if added:
        sys.path.insert(0, str(DOMAIN_APP))
    import_environment = {
        "DATABASE_URL": "postgresql+asyncpg://unused@127.0.0.1/unused",
        "SUPERPLANE_DATABASE_ALLOW_UNVERIFIED_LOCAL_TLS": "true",
    }
    previous = {key: os.environ.get(key) for key in import_environment}
    os.environ.update(import_environment)
    try:
        yield
    finally:
        if added:
            sys.path.remove(str(DOMAIN_APP))
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


import pydantic  # noqa: E402

with domain_app_importable():
    from app.schemas.account import RegisterAccountRequest, RegisterCredentialRequest
    from app.schemas.proxy import CreateDeploymentRequest, DeleteDeploymentRequest
    from app.schemas.quota import SetWorkspaceQuotaRequest
    from app.schemas.workspace import CreateWorkspaceRequest, KubeconfigResponse

from src.auth import vault_routes  # noqa: E402
from src.auth.vault_schemas import CredentialCreate, CredentialResponse  # noqa: E402

# The gateway's live matcher: (method, compiled pattern) over the native domain
# path, exactly as `forward()` applies it.
from src.domain_proxy.superplane import ROUTES  # noqa: E402
from src.domain_proxy.superplane import router as superplane_router  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CLI_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load("adp_superplane_contract", "adp-superplane.py")
CURRENT_RECOVERY_CONTEXT = cli.current_recovery_context
# The helper does `import adp_common as common`, so this is the module it really
# uses; loading a second copy would give CliError a different identity and make
# `pytest.raises` miss.
common = cli.common

# Identifiers of the shape each route actually declares. Workspace, account and
# credential path parameters are all `uuid.UUID`, which is why the CLI has to
# resolve a name before it can address anything.
WORKSPACE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
WORKSPACE_NAME = "prod"
ACCOUNT_RECORD = "33333333-4444-5555-6666-777777777777"
ACCOUNT_NUMBER = "123456789012"
DOMAIN_RECORD = "11111111-2222-3333-4444-555555555555"
VAULT_REFERENCE = "66666666-7777-4888-8999-aaaaaaaaaaaa"
DEPLOY_REQUEST = "77777777-8888-4999-aaaa-bbbbbbbbbbbb"
DEPLOY_APPROVAL = "88888888-9999-4aaa-bbbb-cccccccccccc"
DEPLOY_REVISION = "a" * 64
DEPLOY_AUTH_FLAGS = ["--operation-id", DEPLOY_REQUEST, "--approval-id", DEPLOY_APPROVAL, "--plan-revision", DEPLOY_REVISION]
DEPLOY_CREATE_FLAGS = [*DEPLOY_AUTH_FLAGS, "--profile-id", "fixture-serving"]

DOMAIN_LIST = cli.API_BASE + cli.DOMAIN_CREDENTIALS


# --- the authorities ----------------------------------------------------------


def forwardable(method: str, path: str) -> bool:
    """Would the gateway forward this to the domain?

    `path` is as the CLI emits it — relative to the `/api` mount that
    `adp_common.gateway_url()` appends — so a domain request starts with API_BASE.
    Anything else is not a domain request and is judged by :func:`gateway_owned`.
    """
    if not (path == cli.API_BASE or path.startswith(cli.API_BASE + "/")):
        return False
    native = path[len(cli.API_BASE) :].split("?", 1)[0] or "/"
    return any(verb == method and pattern.fullmatch(native) for verb, pattern in ROUTES)


def gateway_owned(method: str, path: str) -> bool:
    """ADP's own vault endpoints, which the gateway serves itself.

    Matched with FastAPI's own compiled path regexes from the mounted router, so
    `{credential_id}` matching is the server's rather than an approximation. These
    are deliberately absent from the domain allowlist: the vault is ADP's, and
    keeping the two apart is the point of the reference architecture.
    """
    bare = path.split("?", 1)[0]
    routes = [*vault_routes.router.routes, *superplane_router.routes]
    return any(method in getattr(route, "methods", ()) and route.path_regex.fullmatch(bare) for route in routes)


def route_signature(module: str, method: str, path: str) -> dict[str, tuple[str | None, dict]]:
    """A domain route's parameters, read from its source and keyed by its ROUTE.

    Returns `{parameter: (annotation, declaration keywords)}`, where the keywords
    come from the parameter's `Query(...)`/`Depends(...)` call.

    Looked up by the decorator's method and path — the same pair the gateway
    allowlists — rather than by handler name, because the route is what the CLI
    addresses and the handler name is an implementation detail this suite has no
    business knowing. The router's own `prefix=` is honoured so `path` can be
    written exactly as it appears in the allowlist.

    Read from source rather than imported: these modules pull in `jose`, which is
    not a gateway dependency. It is still the server's own declaration, which is
    the part that matters.
    """
    tree = ast.parse((DOMAIN_ROUTERS / module).read_text())
    prefix = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "APIRouter":
            prefix = next((k.value.value for k in node.keywords if k.arg == "prefix"), "")

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        declared = {
            (decorator.func.attr.upper(), prefix + decorator.args[0].value)
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.args
        }
        if (method, path) not in declared:
            continue
        arguments = node.args.args
        defaults = [None] * (len(arguments) - len(node.args.defaults)) + list(node.args.defaults)
        signature = {}
        for argument, default in zip(arguments, defaults, strict=True):
            keywords: dict = {}
            if isinstance(default, ast.Call):
                keywords["declared_by"] = default.func.id if isinstance(default.func, ast.Name) else default.func.attr
                keywords.update({keyword.arg: keyword.value.value for keyword in default.keywords if isinstance(keyword.value, ast.Constant)})
            signature[argument.arg] = (ast.unparse(argument.annotation) if argument.annotation else None, keywords)
        return signature
    raise AssertionError(f"{module} serves no {method} {path}")


def declared_query_parameters(module: str, method: str, path: str) -> set[str]:
    """The query-string names a route accepts, under their wire names."""
    return {
        keywords.get("alias", name) for name, (_, keywords) in route_signature(module, method, path).items() if keywords.get("declared_by") == "Query"
    }


def leaf_actions(*path: str):
    """The argparse actions of one leaf command, read from the real parser."""
    parser = cli.parser()
    for name in path:
        parser = parser._subparsers._group_actions[0].choices[name]
    return parser._actions


def choices_of(dest: str, *path: str):
    return next(action.choices for action in leaf_actions(*path) if action.dest == dest)


# --- running the commands -----------------------------------------------------


class Recorder:
    """Records requests and answers with the servers' real response shapes.

    Response bodies use the field names of the actual response models
    (`WorkspaceListResponse`, `CredentialListResponse`, `AccountListResponse`,
    `KubeconfigResponse`), because the CLI reads them and a plausible-but-wrong key
    would let a misreading pass.
    """

    base = "https://gateway.example.test/api"

    def __init__(self, overrides=None):
        self.sent: list[tuple[str, str, object]] = []
        self.overrides = dict(overrides or {})

    def request(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, body))
        base = path.split("?", 1)[0]
        if (method, base) in self.overrides:
            return self.overrides[(method, base)]
        return self._default(method, base, body)

    def _default(self, method, base, body=None):
        api = cli.API_BASE
        if (method, base) == ("GET", api + "/workspaces"):
            return {
                "workspaces": [
                    {"id": WORKSPACE_ID, "name": WORKSPACE_NAME, "display_name": f"{WORKSPACE_NAME} (dedicated)"},
                    {"id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff", "name": "other", "display_name": "other (namespace)"},
                ],
                "total": 2,
            }
        if (method, base) == ("GET", cli.ACCOUNT_ADAPTER_SUPPORT):
            return {"version": 2, "features": ["account-vault-reference-v1"]}
        if (method, base) == ("GET", cli.DOMAIN_CAPABILITIES):
            return {"version": 1, "features": ["create-operation-id-v1"]}
        if (method, base) == ("POST", api + "/workspaces"):
            return {
                "id": WORKSPACE_ID,
                "name": (body or {}).get("name"),
                "status": "Active",
            }
        if method == "POST" and (base.endswith("/deployments/preview") or base.endswith("/teardown-preview")):
            return {
                "deployment_id": DOMAIN_RECORD,
                "request_id": body["operation_id"],
                "revision": DEPLOY_REVISION,
                "allocation_id": ACCOUNT_RECORD,
                "controller_plan": {"kind": "controller-workload", "profile_id": "fixture-serving"},
                "approval_request": {
                    "workspace_id": WORKSPACE_ID,
                    "action": "teardown" if base.endswith("/teardown-preview") else "provision",
                    "idempotency_key": body["operation_id"],
                    "parameters": {"allocation_id": ACCOUNT_RECORD, "controller_plan": "immutable-fixture"},
                },
            }
        if (method, base) == ("POST", api + "/operation-approvals"):
            return {"approval_id": DEPLOY_APPROVAL, "state": "pending"}
        if method == "DELETE" and "/deployments/" in base:
            return {"name": "llama-8b", "status": "Deleted", "operation_id": DEPLOY_REQUEST, "operation_state": "succeeded"}
        if method == "POST" and base.endswith("/deployments"):
            return {
                "deployment_id": DOMAIN_RECORD,
                "name": (body or {}).get("name"),
                "status": "Pending",
                "operation_id": DEPLOY_REQUEST,
                "operation_state": "pending",
                "provider_uid": None,
            }
        if (method, base) == ("GET", api + "/accounts"):
            return {"accounts": [{"id": ACCOUNT_RECORD, "account_id": ACCOUNT_NUMBER, "name": "prod"}], "total": 1}
        if (method, base) == ("POST", api + "/accounts"):
            return {
                "id": ACCOUNT_RECORD,
                "account_id": ACCOUNT_NUMBER,
                "name": (body or {}).get("name"),
                "provider": "aws",
                "adp_credential_ids": [(body or {}).get("adp_credential_id")],
                "status": "Active",
            }
        if (method, base) == ("GET", DOMAIN_LIST):
            return {
                "credentials": [
                    {
                        "id": DOMAIN_RECORD,
                        "name": "prod",
                        "provider": "nebius",
                        "credential_type": "api_key",
                        "adp_credential_id": VAULT_REFERENCE,
                        "status": "active",
                    }
                ],
                "total": 1,
            }
        if method == "PUT" and base == f"{cli.VAULT_CREDENTIALS}/{VAULT_REFERENCE}":
            return {
                "id": VAULT_REFERENCE,
                "service": "nebius",
                "label": "prod",
                "credential_type": (body or {}).get("credential_type"),
                "scope": "user",
            }
        if base.endswith("/kubeconfig"):
            return {"kubeconfig": "apiVersion: v1\n", "expires_at": "2026-09-21T12:00:00Z"}
        return {}

    def paths(self):
        return [(method, path.split("?", 1)[0]) for method, path, _ in self.sent]

    def body(self, method, path):
        for sent_method, sent_path, body in self.sent:
            if sent_method == method and sent_path.split("?", 1)[0] == path:
                return body
        raise AssertionError(f"{method} {path} was never sent; got {self.paths()}")

    def query(self, method, path) -> set[str]:
        """The query parameter NAMES sent with a request."""
        for sent_method, sent_path, _ in self.sent:
            if sent_method == method and sent_path.split("?", 1)[0] == path:
                _, _, raw = sent_path.partition("?")
                return {pair.split("=", 1)[0] for pair in raw.split("&") if pair}
        raise AssertionError(f"{method} {path} was never sent; got {self.paths()}")


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    """A sandboxed home with `prod` selected BY NAME.

    That is the realistic state — `workspace use prod` records what the user typed
    — and it is the state in which the unresolved-name defect reached the server.
    """
    for key in list(os.environ):
        if key.startswith("ADP_DEPLOYMENT") or key in {"ADP_HOME", "ADP_LEGACY_CONFIG_DIR", "BG_CONFIG_DIR"}:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(common, "_deployment", common._UNRESOLVED)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(common, "access_token", lambda: "synthetic-session-token")
    monkeypatch.setattr(
        cli,
        "current_recovery_context",
        lambda api=None: {"deployment_id": "test", "gateway": "test", "principal": "user", "tenant": "org"},
    )
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: uuid.UUID(VAULT_REFERENCE))
    common.write_state(cli.STATE, {"workspace": WORKSPACE_NAME})
    return tmp_path


def run(argv, overrides=None) -> tuple[Recorder, dict]:
    api = Recorder(overrides)
    return api, cli.run(cli.parser().parse_args(argv), api)


def _jwt(claims):
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_recovery_context_uses_selected_tenant_from_envelope(monkeypatch):
    monkeypatch.setattr(common, "deployment_stamp", lambda: {"deployment_id": "deployment-1"})
    monkeypatch.setattr(common, "gateway_url", lambda: "https://gateway.example.test/api")
    original = _jwt({"sub": "user-1", "org_id": "home", "custom:org_id": "home"})
    lease = _jwt({"tenant": "selected"})
    monkeypatch.setattr(common, "access_token", lambda: "adpctx1~" + lease + "~" + original)
    result = CURRENT_RECOVERY_CONTEXT()
    assert result["principal"] == "user-1"
    assert result["tenant"] == "selected"


def test_orgless_password_session_cannot_create_workspaces_or_deployments(
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "current_recovery_context", CURRENT_RECOVERY_CONTEXT)
    monkeypatch.setattr(
        common,
        "deployment_stamp",
        lambda: {"deployment_id": "deployment-1", "deployment": "test"},
    )
    monkeypatch.setattr(common, "gateway_url", lambda: "https://gateway.example.test/api")
    monkeypatch.setattr(common, "access_token", lambda: _jwt({"sub": "user-1"}))

    commands = [
        ["workspace", "create", "--name", "new-ws", "--yes"],
        [
            "deploy",
            "create",
            *DEPLOY_CREATE_FLAGS,
            "--name",
            "llama-8b",
            "--model",
            "meta-llama/Llama-3-8B",
            "--yes",
        ],
    ]
    for argv in commands:
        api = Recorder()
        with pytest.raises(cli.CliError) as raised:
            cli.run(cli.parser().parse_args(argv), api)
        assert raised.value.code == "tenant_bound_token_required"
        assert not any(method in {"POST", "PUT", "PATCH", "DELETE"} for method, _ in api.paths())
    assert cli.create_recoveries() == {}


def test_missing_tenant_refusal_agrees_with_the_real_domain_token_policy(monkeypatch):
    monkeypatch.syspath_prepend(str(DOMAIN_APP.parents[1] / "auth"))
    from superplane_auth.policy import TRUSTED_VALIDATION_PATH, DomainTokenPolicy, TokenRejectedError

    claims = {
        "sub": "user-1",
        "token_use": "access",
        "client_id": "fixture-client",
        "iss": "https://issuer.example.test",
        "custom:account_type": "human",
    }
    policy = DomainTokenPolicy(["fixture-client"], "https://issuer.example.test")
    # This exercises policy on supplied claims, not JWT signature verification.
    with pytest.raises(TokenRejectedError, match="no organization claim"):
        policy.admit(claims, validation_path=TRUSTED_VALIDATION_PATH)
    monkeypatch.setattr(common, "deployment_stamp", lambda: {"deployment_id": "test", "deployment": "test"})
    monkeypatch.setattr(common, "gateway_url", lambda: "https://gateway.example.test/api")
    monkeypatch.setattr(common, "access_token", lambda: _jwt(claims))
    with pytest.raises(cli.CliError) as raised:
        CURRENT_RECOVERY_CONTEXT()
    assert raised.value.code == "tenant_bound_token_required"


# Every advertised command form that reaches a server, with arguments a user would
# really supply. The purely local and refused verbs are absent by design and are
# asserted separately.
COMMANDS = [
    (["workspace", "list"], "workspace list"),
    (["workspace", "create", "--name", "new-ws", "--yes"], "workspace create"),
    (["workspace", "describe"], "workspace describe"),
    (["workspace", "kubeconfig"], "workspace kubeconfig"),
    (["node"], "node list"),
    (["quota", "show"], "quota show"),
    (["quota", "set", "--max-gpus", "4", "--yes"], "quota set"),
    (["cost"], "cost workspace"),
    (["cost", "--org"], "cost --org"),
    (["events", "--resource-type", "workspace", "--limit", "10"], "events"),
    (["deploy", "list"], "deploy list"),
    (["deploy", "create", *DEPLOY_CREATE_FLAGS, "--name", "llama-8b", "--model", "meta-llama/Llama-3-8B", "--yes"], "deploy create"),
    (["deploy", "delete", *DEPLOY_AUTH_FLAGS, "--id", DOMAIN_RECORD, "--yes"], "deploy delete"),
    (
        [
            "account",
            "onboard",
            "--name",
            "prod",
            "--provider",
            "aws",
            "--account-id",
            ACCOUNT_NUMBER,
            "--credential-id",
            VAULT_REFERENCE,
            "--yes",
        ],
        "account onboard",
    ),
    (["account", "list"], "account list"),
    (["account", "delete", ACCOUNT_NUMBER, "--yes"], "account delete"),
    (["provider", "list"], "provider list"),
    (["provider", "delete", DOMAIN_RECORD, "--yes"], "provider delete"),
]


@pytest.mark.parametrize("argv,label", COMMANDS, ids=[label for _, label in COMMANDS])
def test_every_request_a_command_emits_is_one_a_server_answers(argv, label) -> None:
    """AC-01: no advertised command may emit an unroutable request.

    The gateway matches each forwarded path against its allowlist and 404s anything
    else, so an invented path is not a cosmetic mismatch — the command cannot work
    at all. Five of them could not.
    """
    api, _ = run(argv)
    assert api.sent, f"{label} emitted no request"
    for method, path in api.paths():
        assert forwardable(method, path) or gateway_owned(method, path), (
            f"{label} emits {method} {path}, which nothing would answer: it is in neither the Superplane "
            f"route allowlist ({ALLOWLIST_FILE.name}) nor ADP's own vault routes, so it can only 404."
        )


def test_provider_add_reaches_both_stores_and_only_through_real_routes(monkeypatch) -> None:
    """`provider add` is the one form that writes to both stores.

    Kept out of the table above only because it needs stdin. The reachability rule
    is the same, and this is the verb where getting it wrong stored a secret against
    a registration that could never land.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-synthetic-value\n"))
    api, result = run(
        ["provider", "add", "--name", "prod", "--provider", "nebius", "--stdin", "--yes"],
        {
            ("POST", DOMAIN_LIST): {
                "id": DOMAIN_RECORD,
                "name": "prod",
                "provider": "nebius",
                "credential_type": "api_key",
                "adp_credential_id": VAULT_REFERENCE,
            }
        },
    )

    assert api.paths() == [
        ("PUT", f"{cli.VAULT_CREDENTIALS}/{VAULT_REFERENCE}"),
        ("POST", DOMAIN_LIST),
    ]
    assert gateway_owned("PUT", f"{cli.VAULT_CREDENTIALS}/{VAULT_REFERENCE}")
    assert forwardable("POST", DOMAIN_LIST)
    assert result["detail"]["adp_credential_id"] == VAULT_REFERENCE
    assert result["detail"]["domain_record"] == DOMAIN_RECORD


def test_importing_the_domain_schemas_leaves_no_directory_on_the_path() -> None:
    """The domain app's directory must not outlive its own import.

    It carries a top-level `scripts/` package that shadows the gateway's, so a
    leaked `sys.path` entry breaks unrelated suites collected after this one --
    `tests/lambda/test_mantle_budget_recovery.py` imports
    `scripts.backfill_mantle_budget_usage` and stopped resolving it. The failure
    appears in another file, so assert the invariant at its source.
    """
    assert str(DOMAIN_APP) not in sys.path

    with domain_app_importable():
        assert str(DOMAIN_APP) == sys.path[0]
    assert str(DOMAIN_APP) not in sys.path

    # The gateway's own `scripts` package is the one that still resolves.
    spec = importlib.util.find_spec("scripts.backfill_mantle_budget_usage")
    assert spec is not None


def test_the_verbs_that_write_nothing_emit_nothing() -> None:
    api, _ = run(["workspace", "use", WORKSPACE_NAME])
    assert api.sent == []
    for verb in cli.REDIRECTED:
        assert cli.redirect(verb)["status"] == "unavailable"


# --- bodies, validated by the classes the server validates them with ----------


def test_the_deployment_body_is_accepted_by_the_servers_own_model() -> None:
    """AC-01/AC-04: `model_name`, and a `name` the user must supply.

    The helper sent `model` and documented `--name` as generated when omitted. Both
    are 422s against `CreateDeploymentRequest`: there is no `model` field, and
    `name` is required with no default, so nothing generated anything.
    """
    api, _ = run(["deploy", "create", *DEPLOY_CREATE_FLAGS, "--name", "llama-8b", "--model", "meta-llama/Llama-3-8B", "--precision", "bf16", "--yes"])
    body = api.body("POST", f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/deployments")

    validated = CreateDeploymentRequest.model_validate(body)
    assert validated.name == "llama-8b"
    assert validated.model_name == "meta-llama/Llama-3-8B"
    assert validated.precision == "bf16"
    assert "model" not in body, "the server has no `model` field; sending it is how the request became a 422"

    # Optional settings the user did not choose are ABSENT from the body, so the
    # server's own defaults stay authoritative instead of being overwritten by the
    # CLI's guess at them.
    for unset in ("serving_framework", "replicas", "gpu_per_replica", "tensor_parallel_size", "max_model_len", "namespace"):
        assert unset not in body
    assert validated.serving_framework == "vllm" and validated.replicas == 1


def test_every_precision_the_server_accepts_is_reachable() -> None:
    """The offered choices are the server's set, not a subset of it.

    `awq` and `int8` were unreachable through the CLI while the server accepted
    them — a capability quietly withheld rather than a broken request, but still the
    CLI disagreeing with the schema. The same check catches the opposite error: a
    choice the server would reject.
    """
    pattern = next(item.pattern for item in CreateDeploymentRequest.model_fields["precision"].metadata if hasattr(item, "pattern"))
    alternation = re.fullmatch(r"\^\((.+)\)\$", pattern)
    assert alternation, f"precision is no longer a simple alternation ({pattern}); update this check"
    assert set(choices_of("precision", "deploy", "create")) == set(alternation.group(1).split("|"))


def test_the_credential_registration_body_is_accepted_by_the_servers_own_model(monkeypatch) -> None:
    """AC-02: the reference, under the field name the domain declares.

    `RegisterCredentialRequest` takes `adp_credential_id`; the helper's
    `credential_id` is not a field of it. Its `credential_type` pattern is also
    narrower than ADP's vault enum, so an untranslated `config_file` is a 422 AFTER
    the secret has been stored — the worst possible moment, because the caller now
    owns an untracked credential.
    """
    key = '{\n  "type": "service_account"\n}'
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    api, _ = run(
        ["provider", "add", "--name", "prod", "--provider", "nebius", "--type", "config_file", "--stdin", "--yes"],
        {
            ("POST", DOMAIN_LIST): {
                "id": DOMAIN_RECORD,
                "name": "prod",
                "provider": "nebius",
                "credential_type": "service_account",
                "adp_credential_id": VAULT_REFERENCE,
            }
        },
    )

    domain_body = api.body("POST", DOMAIN_LIST)
    assert domain_body == {
        "name": "prod",
        "provider": "nebius",
        "credential_type": "service_account",
        "adp_credential_id": VAULT_REFERENCE,
    }
    assert RegisterCredentialRequest.model_validate(domain_body).adp_credential_id == VAULT_REFERENCE

    # The vault half keeps ADP's own vocabulary for the same credential, validated
    # by ADP's own model. One store's word for a type is not the other's.
    stored = CredentialCreate.model_validate(api.body("PUT", f"{cli.VAULT_CREDENTIALS}/{VAULT_REFERENCE}"))
    assert stored.credential_type.value == "config_file"
    assert stored.value == key, "the whole multi-line key must reach the vault"


def test_every_credential_type_the_cli_offers_maps_to_one_each_store_accepts() -> None:
    """The translation is complete, and checked at both ends.

    A type the CLI offers but the domain rejects is a 422 the user cannot avoid; a
    mapping entry for a type the CLI does not offer is dead code that hides the
    gap. Both ends are validated by the real models rather than by re-reading the
    pattern.
    """
    assert set(choices_of("type", "provider", "add")) == set(cli.DOMAIN_CREDENTIAL_TYPES), "every offered type needs exactly one mapping"

    for offered, translated in cli.DOMAIN_CREDENTIAL_TYPES.items():
        CredentialCreate.model_validate({"service": "nebius", "label": "prod", "credential_type": offered, "value": "synthetic"})
        RegisterCredentialRequest.model_validate(
            {"name": "prod", "provider": "nebius", "credential_type": translated, "adp_credential_id": VAULT_REFERENCE}
        )

    # And an untranslated value really is rejected, so the mapping is load-bearing
    # rather than decorative.
    with pytest.raises(pydantic.ValidationError):
        RegisterCredentialRequest.model_validate(
            {"name": "prod", "provider": "nebius", "credential_type": "config_file", "adp_credential_id": VAULT_REFERENCE}
        )


def test_the_workspace_and_quota_bodies_are_accepted_by_their_real_models() -> None:
    """AC-04: the forms that already worked must keep working.

    Included because a repair aimed only at the broken verbs would be free to break
    these, and nothing else here would notice.
    """
    api, _ = run(["workspace", "create", "--name", "new-ws", "--isolation", "namespace", "--budget-gpus", "4", "--yes"])
    created = CreateWorkspaceRequest.model_validate(api.body("POST", cli.API_BASE + "/workspaces"))
    assert (created.name, created.isolation_mode, created.budget_max_gpus) == ("new-ws", "namespace", 4)

    api, _ = run(["quota", "set", "--max-gpus", "8", "--allowed-clouds", "aws, lambda", "--yes"])
    quota = SetWorkspaceQuotaRequest.model_validate(api.body("PATCH", f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/quota"))
    assert quota.max_gpus == 8
    assert quota.allowed_clouds == ["aws", "lambda"], "the comma-separated flag must arrive as a list, not a string"


@pytest.mark.parametrize(
    ("argv", "post_path"),
    [
        (["workspace", "create", "--name", "new-ws", "--yes"], cli.API_BASE + "/workspaces"),
        (
            ["deploy", "create", *DEPLOY_CREATE_FLAGS, "--workspace", WORKSPACE_ID, "--name", "llama-8b", "--model", "model", "--yes"],
            f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/deployments",
        ),
    ],
)
def test_create_refuses_an_old_domain_before_mutation(argv, post_path) -> None:
    api = Recorder({("GET", cli.DOMAIN_CAPABILITIES): {"version": 1, "features": []}})

    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), api)

    assert raised.value.code == "create_idempotency_unavailable"
    assert all(not (method == "POST" and path == post_path) for method, path in api.paths())


def test_uncertain_workspace_create_reuses_its_persisted_operation_id() -> None:
    class LostResponse(Recorder):
        def request(self, method, path, body=None, **kwargs):
            if (method, path) == ("POST", cli.API_BASE + "/workspaces"):
                self.sent.append((method, path, body))
                raise cli.CliError("connection closed", "gateway_unavailable")
            return super().request(method, path, body, **kwargs)

    argv = ["workspace", "create", "--name", "new-ws", "--yes"]
    first = LostResponse()
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), first)
    assert raised.value.code == "create_delivery_uncertain"
    first_operation = first.body("POST", cli.API_BASE + "/workspaces")["operation_id"]

    retry, result = run(argv)
    assert retry.body("POST", cli.API_BASE + "/workspaces")["operation_id"] == first_operation
    assert result["status"] == "ok"
    assert cli.create_recoveries()[first_operation]["phase"] == "completed"
    assert cli.create_recoveries()[first_operation]["resource_id"] == WORKSPACE_ID


@pytest.mark.parametrize("features", ["create-operation-id-v1", "prefix-create-operation-id-v1-suffix", {"create-operation-id-v1": True}, None])
def test_create_rejects_malformed_capability_features_before_writing(features) -> None:
    api = Recorder({("GET", cli.DOMAIN_CAPABILITIES): {"version": 1, "features": features}})
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(["workspace", "create", "--name", "new-ws", "--yes"]), api)
    assert raised.value.code == "create_idempotency_unavailable"
    assert not any(method == "POST" for method, _ in api.paths())
    assert cli.create_recoveries() == {}


def test_renaming_deployment_preserves_an_unresolved_request_identity(monkeypatch) -> None:
    context = {"deployment_id": "test", "deployment": "old-name", "gateway": "test", "principal": "user", "tenant": "org"}
    monkeypatch.setattr(cli, "current_recovery_context", lambda api=None: dict(context))
    path = cli.API_BASE + "/workspaces"
    body = {"name": "new-ws", "isolation_mode": "namespace"}
    first = cli._prepare_create_recovery("superplane workspace create", path, body)
    context["deployment"] = "new-name"
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: uuid.UUID("11111111-2222-4333-8444-555555555555"))
    retry = cli._prepare_create_recovery("superplane workspace create", path, body)
    assert retry["operation_id"] == first["operation_id"]
    assert len(cli.create_recoveries()) == 1


@pytest.mark.parametrize("reported_status", ["Reconciling", "NewServerState", "   "])
def test_unrecognized_resource_status_preserves_identity_for_reconciliation(reported_status) -> None:
    path = cli.API_BASE + "/workspaces"
    argv = ["workspace", "create", "--name", "new-ws", "--yes"]
    first = Recorder({("POST", path): {"id": WORKSPACE_ID, "name": "new-ws", "status": reported_status}})
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), first)
    assert raised.value.code == "create_delivery_uncertain"
    identity = first.body("POST", path)["operation_id"]
    assert identity in cli.create_recoveries()
    retry, _ = run(argv)
    assert retry.body("POST", path)["operation_id"] == identity


@pytest.mark.parametrize("reported_status", ["Active", "Ready", "Created"])
def test_completed_create_retains_its_operation_id(monkeypatch, reported_status) -> None:
    operation_ids = iter(
        (
            "11111111-2222-4333-8444-555555555555",
            "22222222-3333-4444-8555-666666666666",
        )
    )
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: uuid.UUID(next(operation_ids)))
    argv = ["workspace", "create", "--name", "new-ws", "--yes"]

    path = cli.API_BASE + "/workspaces"
    overrides = {("POST", path): {"id": WORKSPACE_ID, "name": "new-ws", "status": reported_status}}
    first, _ = run(argv, overrides)
    second, _ = run(argv, overrides)

    identity = first.body("POST", path)["operation_id"]
    assert identity == second.body("POST", path)["operation_id"]
    assert cli.create_recoveries()[identity]["phase"] == "completed"
    assert cli.create_recoveries()[identity]["resource_id"] == WORKSPACE_ID


@pytest.mark.parametrize("resource", ["workspace", "deployment"])
def test_completed_create_with_lost_output_reuses_the_original_resource(monkeypatch, resource) -> None:
    path = cli.API_BASE + "/workspaces"
    argv = ["workspace", "create", "--name", "new-ws", "--yes"]
    id_field, resource_id, name = "id", WORKSPACE_ID, "new-ws"
    if resource == "deployment":
        path += f"/{WORKSPACE_ID}/deployments"
        argv = ["deploy", "create", *DEPLOY_CREATE_FLAGS, "--workspace", WORKSPACE_ID, "--name", "llama-8b", "--model", "model", "--yes"]
        id_field, resource_id, name = "deployment_id", DOMAIN_RECORD, "llama-8b"
    overrides = {("POST", path): {id_field: resource_id, "name": name, "status": "Active"}}
    first = Recorder(overrides)
    envelope = cli.common.envelope

    def lost_output(*args, **kwargs):
        raise BrokenPipeError("caller disconnected before the result was emitted")

    monkeypatch.setattr(cli.common, "envelope", lost_output)
    with pytest.raises(BrokenPipeError):
        cli.run(cli.parser().parse_args(argv), first)
    identity = first.body("POST", path)["operation_id"]
    monkeypatch.setattr(cli.common, "envelope", envelope)

    retry, result = run(argv, overrides)

    assert retry.body("POST", path)["operation_id"] == identity
    assert result["detail"][id_field] == resource_id
    assert cli.create_recoveries()[identity]["resource_id"] == resource_id


def test_completed_create_rejects_a_replay_that_changes_resource_identity() -> None:
    argv = ["workspace", "create", "--name", "new-ws", "--yes"]
    first, _ = run(argv)
    path = cli.API_BASE + "/workspaces"
    identity = first.body("POST", path)["operation_id"]
    changed = Recorder({("POST", path): {"id": DOMAIN_RECORD, "name": "new-ws", "status": "Active"}})

    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), changed)

    assert raised.value.code == "create_delivery_uncertain"
    assert changed.body("POST", path)["operation_id"] == identity
    assert cli.create_recoveries()[identity]["resource_id"] == WORKSPACE_ID


def test_create_after_delete_cannot_replay_the_deleted_resource(monkeypatch) -> None:
    operation_ids = iter(
        (
            "11111111-2222-4333-8444-555555555555",
            "22222222-3333-4444-8555-666666666666",
        )
    )
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: uuid.UUID(next(operation_ids)))

    class DeletedResource(Recorder):
        def __init__(self):
            super().__init__()
            self.created_operation = None
            self.deleted = False

        def request(self, method, path, body=None, **kwargs):
            if (method, path) == ("POST", cli.API_BASE + "/workspaces"):
                self.sent.append((method, path, body))
                operation_id = body["operation_id"]
                if self.deleted and operation_id == self.created_operation:
                    status = "Deleted"
                else:
                    self.created_operation = operation_id
                    status = "Active"
                return {"id": WORKSPACE_ID, "name": body["name"], "status": status}
            return super().request(method, path, body, **kwargs)

    api = DeletedResource()
    args = cli.parser().parse_args(["workspace", "create", "--name", "new-ws", "--yes"])
    first = cli.run(args, api)
    api.deleted = True
    with pytest.raises(cli.CliError) as raised:
        cli.run(args, api)
    assert raised.value.code == "create_operation_retired"
    with pytest.raises(cli.CliError) as raised:
        cli.run(args, api)
    assert raised.value.code == "create_operation_retired"

    operations = [body["operation_id"] for method, _, body in api.sent if method == "POST"]
    assert first["detail"]["status"] == "Active"
    assert len(operations) == 2
    assert operations[0] == operations[1]
    assert cli.create_recoveries()[operations[0]]["phase"] == "retired"


@pytest.mark.parametrize("reported_status", ["Deleted", "Deleting", "Teardown"])
def test_legacy_receipt_for_a_retired_resource_blocks_new_intent(monkeypatch, reported_status) -> None:
    operation_ids = iter(
        (
            "11111111-2222-4333-8444-555555555555",
            "22222222-3333-4444-8555-666666666666",
        )
    )
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: uuid.UUID(next(operation_ids)))
    path = cli.API_BASE + "/workspaces"
    body = {"name": "new-ws", "isolation_mode": "namespace"}
    receipt = cli._prepare_create_recovery("superplane workspace create", path, body)
    api = Recorder(
        {
            ("POST", path): {
                "id": WORKSPACE_ID,
                "name": "new-ws",
                "status": reported_status,
            }
        }
    )

    with pytest.raises(cli.CliError) as raised:
        cli.replay_safe_create(
            api,
            "superplane workspace create",
            path,
            body,
            expected_name="new-ws",
            id_field="id",
        )

    assert raised.value.code == "create_operation_retired"
    assert cli.create_recoveries()[receipt["operation_id"]]["phase"] == "retired"
    replacement = cli._prepare_create_recovery("superplane workspace create", path, body)
    assert replacement["operation_id"] == receipt["operation_id"]


def test_workspace_create_does_not_render_provisioning_as_completed() -> None:
    path = cli.API_BASE + "/workspaces"
    api = Recorder(
        {
            ("POST", path): {
                "id": WORKSPACE_ID,
                "name": "new-ws",
                "status": "Provisioning",
            }
        }
    )

    result = cli.run(
        cli.parser().parse_args(["workspace", "create", "--name", "new-ws", "--yes"]),
        api,
    )

    assert result["status"] == "pending"
    assert WORKSPACE_ID in result["next_action"]
    receipt = next(iter(cli.create_recoveries().values()))
    assert receipt["phase"] == "accepted_pending"
    assert receipt["resource_id"] == WORKSPACE_ID
    assert receipt["status"] == "Provisioning"


def test_rejected_create_keeps_its_operation_identity_for_partial_write_recovery() -> None:
    class Rejected(Recorder):
        def request(self, method, path, body=None, **kwargs):
            if (method, path) == ("POST", cli.API_BASE + "/workspaces"):
                self.sent.append((method, path, body))
                raise cli.CliError("provisioning refused", "invalid_request", 5, status_code=400)
            return super().request(method, path, body, **kwargs)

    argv = ["workspace", "create", "--name", "new-ws", "--yes"]
    first = Rejected()
    with pytest.raises(cli.CliError):
        cli.run(cli.parser().parse_args(argv), first)
    path = cli.API_BASE + "/workspaces"
    operation_id = first.body("POST", path)["operation_id"]

    retry, _ = run(argv)
    assert retry.body("POST", path)["operation_id"] == operation_id


def test_confirmed_failed_create_is_not_reported_as_uncertain(monkeypatch) -> None:
    class FailedOperation(Recorder):
        def request(self, method, path, body=None, **kwargs):
            if (method, path) == ("POST", cli.API_BASE + "/workspaces"):
                self.sent.append((method, path, body))
                raise cli.CliError(
                    "workspace operation failed",
                    "create_operation_failed",
                    5,
                    status_code=409,
                )
            return super().request(method, path, body, **kwargs)

    operation_ids = iter(
        (
            "11111111-2222-4333-8444-555555555555",
            "22222222-3333-4444-8555-666666666666",
        )
    )
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: uuid.UUID(next(operation_ids)))
    argv = ["workspace", "create", "--name", "new-ws", "--yes"]
    api = FailedOperation()
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), api)

    assert raised.value.code == "create_operation_failed"
    failed_operation = api.body("POST", cli.API_BASE + "/workspaces")["operation_id"]

    retry = Recorder()
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), retry)
    assert raised.value.code == "create_operation_failed"
    assert not any(method == "POST" for method, _ in retry.paths())
    assert cli.create_recoveries()[failed_operation]["phase"] == "failed"


def test_failed_status_in_a_legacy_success_body_is_not_reported_as_ok(monkeypatch) -> None:
    operation_ids = iter(
        (
            "11111111-2222-4333-8444-555555555555",
            "22222222-3333-4444-8555-666666666666",
        )
    )
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: uuid.UUID(next(operation_ids)))
    path = f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/deployments"
    api = Recorder(
        {
            ("POST", path): {
                "deployment_id": DOMAIN_RECORD,
                "name": "llama-8b",
                "status": "Failed",
            }
        }
    )

    argv = [
        "deploy",
        "create",
        *DEPLOY_CREATE_FLAGS,
        "--workspace",
        WORKSPACE_ID,
        "--name",
        "llama-8b",
        "--model",
        "model",
        "--yes",
    ]
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), api)

    assert raised.value.code == "create_operation_failed"
    failed_operation = api.body("POST", path)["operation_id"]

    retry = Recorder()
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), retry)
    assert raised.value.code == "create_operation_failed"
    assert not any(method == "POST" for method, _ in retry.paths())
    assert cli.create_recoveries()[failed_operation]["resource_id"] == DOMAIN_RECORD
    assert cli.create_recoveries()[failed_operation]["phase"] == "failed"


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_governed_deployment_needing_recovery_preserves_identity_and_exposure(state):
    path = f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/deployments"
    result = {
        "deployment_id": DOMAIN_RECORD,
        "name": "llama-8b",
        "status": "NeedsRecovery",
        "operation_id": DEPLOY_REQUEST,
        "operation_state": state,
        "provider_uid": "retained-provider-uid",
    }
    argv = ["deploy", "create", *DEPLOY_CREATE_FLAGS, "--workspace", WORKSPACE_ID, "--name", "llama-8b", "--model", "model", "--yes"]
    for _ in range(2):
        api, response = run(argv, {("POST", path): result})
        assert api.body("POST", path)["operation_id"] == DEPLOY_REQUEST
        assert response["status"] == "pending"
        assert response["detail"] == result
    receipt = cli.create_recoveries()[DEPLOY_REQUEST]
    assert receipt["resource_id"] == DOMAIN_RECORD
    assert receipt["phase"] == "accepted_pending"
    assert len(cli.create_recoveries()) == 1


def test_malformed_deployment_success_keeps_the_same_recovery_receipt() -> None:
    path = f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/deployments"
    argv = [
        "deploy",
        "create",
        *DEPLOY_CREATE_FLAGS,
        "--workspace",
        WORKSPACE_ID,
        "--name",
        "llama-8b",
        "--model",
        "model",
        "--yes",
    ]
    first = Recorder({("POST", path): {"name": "llama-8b"}})
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), first)
    assert raised.value.code == "create_delivery_uncertain"
    operation_id = first.body("POST", path)["operation_id"]

    retry, _ = run(argv)
    assert retry.body("POST", path)["operation_id"] == operation_id


# --- one test per original mismatch (AC-04) ----------------------------------


def test_kubeconfig_is_a_post_and_keeps_the_expiry() -> None:
    """Mismatch 1: `GET /workspaces/{id}/kubeconfig`, with `expires_at` discarded.

    The route is POST because it MINTS short-lived credentials, and only the POST
    pair is allowlisted, so the GET was 404'd at the proxy. `expires_at` is required
    by `KubeconfigResponse`, and dropping it left the caller no way to know when
    their cluster access dies.
    """
    api, result = run(["workspace", "kubeconfig"])
    path = f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/kubeconfig"

    assert api.paths() == [("GET", cli.API_BASE + "/workspaces"), ("POST", path)]
    assert forwardable("POST", path)
    # Reintroducing the GET fails here: the gateway forwards only the POST.
    assert not forwardable("GET", path)
    assert "expires_at" in KubeconfigResponse.model_fields
    assert result["detail"]["expires_at"] == "2026-09-21T12:00:00Z"
    assert result["detail"]["kubeconfig"]


def test_cost_uses_the_two_real_routes_and_not_an_invented_summary() -> None:
    """Mismatch 2: `GET /cost/summary?workspace=...`, a path that never existed.

    The domain has one cost route per workspace and one per organization
    (app/routers/cost.py). A single endpoint taking the workspace as a query
    parameter was the helper's invention, so every `adp superplane cost` 404'd.
    """
    assert not forwardable("GET", cli.API_BASE + "/cost/summary")

    api, _ = run(["cost", "--start-date", "2026-09-01"])
    workspace_cost = f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/cost"
    assert ("GET", workspace_cost) in api.paths()
    assert api.query("GET", workspace_cost) == {"start_date"}
    assert declared_query_parameters("cost.py", "GET", "/workspaces/{workspace_id}/cost") >= {"start_date", "end_date"}

    api, _ = run(["cost", "--org"])
    assert api.paths() == [("GET", cli.API_BASE + "/orgs/cost")]
    # The organization form must not need, or resolve, a workspace at all.
    assert all("workspaces" not in path for _, path in api.paths())


def test_the_two_cost_scopes_are_mutually_exclusive() -> None:
    """Both at once has no meaning, and the server would silently pick one."""
    with pytest.raises(cli.CliError) as raised:
        run(["cost", "--org", "--workspace", WORKSPACE_ID])
    assert raised.value.code == "usage_error"


def test_a_workspace_name_is_resolved_to_the_uuid_the_routes_require() -> None:
    """Mismatch 3: the selected NAME went straight into the path.

    Every workspace path parameter is declared `workspace_id: uuid.UUID`, so the
    server answered 422 for `prod` — which made six commands fail for everyone who
    had selected a workspace the documented way.
    """
    api, _ = run(["node"])

    assert api.paths() == [
        ("GET", cli.API_BASE + "/workspaces"),
        ("GET", f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/nodes"),
    ]
    assert all(WORKSPACE_NAME not in path for _, path in api.paths()), "the name must appear in no path"
    # The reason is the server's own declaration, not a convention.
    assert route_signature("workspaces.py", "GET", "/workspaces/{workspace_id}")["workspace_id"][0] == "uuid.UUID"


def test_an_explicit_id_is_used_without_a_lookup() -> None:
    """Resolution must not become a new permission requirement.

    A caller who already has the id must not be forced to hold list permission just
    to use it, so a UUID skips the lookup entirely.
    """
    api, _ = run(["node", "--workspace", WORKSPACE_ID])
    assert api.paths() == [("GET", f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/nodes")]


def test_an_ambiguous_name_is_refused_rather_than_guessed() -> None:
    """Two workspaces can share a name across isolation modes.

    Picking either would aim a delete or a deployment at a workspace the user did
    not name, so the ids are listed and the choice stays theirs.
    """
    duplicates = {
        ("GET", cli.API_BASE + "/workspaces"): {
            "workspaces": [
                {"id": WORKSPACE_ID, "name": WORKSPACE_NAME, "display_name": "prod (dedicated)"},
                {"id": "cccccccc-dddd-eeee-ffff-000000000000", "name": WORKSPACE_NAME, "display_name": "prod (namespace)"},
            ],
            "total": 2,
        }
    }
    with pytest.raises(cli.CliError) as raised:
        run(["node"], duplicates)
    assert raised.value.code == "workspace_ambiguous"
    assert "Nothing was changed" in str(raised.value)


def test_a_failed_resolution_stops_before_the_operation_it_was_for() -> None:
    """An unknown name must not become an unscoped or partial request.

    Asserted on a DELETE specifically: resolution failing after the destructive
    call would be the worst ordering, and the request list is the only proof of the
    order.
    """
    api = Recorder({("GET", cli.API_BASE + "/workspaces"): {"workspaces": [], "total": 0}})
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(["deploy", "delete", *DEPLOY_AUTH_FLAGS, "--id", DOMAIN_RECORD, "--yes"]), api)
    assert raised.value.code == "workspace_not_found"
    assert api.paths() == [("GET", cli.API_BASE + "/workspaces")], "nothing may be deleted after a failed resolution"


def test_events_filters_only_by_parameters_the_route_declares() -> None:
    """Mismatch 4: `--workspace` was accepted, sent, and dropped server-side.

    `GET /events` declares resource_type, user, action, event_type, start_time,
    end_time, limit and offset. FastAPI ignores an unknown query parameter, so the
    user was shown EVERY workspace's events while believing the list was scoped —
    worse than not offering the filter at all.

    EVERY flag the verb offers is exercised, taken from the parser rather than
    listed here. A hand-written argv would leave a newly-added flag untested, which
    is precisely how `--workspace` survived: the flag existed, the tests never
    passed it, and the parameter it sent was invisible.
    """
    declared = declared_query_parameters("events.py", "GET", "/events")
    offered = [
        action
        for action in leaf_actions("events")
        if action.option_strings and action.dest not in ("help", "json", "workspace", "follow", "after", "timeout", "max_pages")
    ]
    argv = ["events"]
    for action in offered:
        argv += [action.option_strings[0], "10" if action.type is int else "synthetic"]

    api, _ = run(argv)
    emitted = api.query("GET", cli.API_BASE + "/events")

    assert emitted <= declared, f"{sorted(emitted - declared)} would be silently dropped by the server"
    # Every offered filter must actually reach the server, or the CLI is advertising
    # a filter it drops itself.
    assert emitted == {action.dest for action in offered}
    assert "workspace" not in declared


def test_the_retired_event_filter_names_the_problem_it_caused() -> None:
    """Refused as its own error, not as an unknown flag.

    A user with `--workspace` in a script needs to know their results were never
    scoped, not merely that a flag went away — and needs to be told which filters
    do exist.
    """
    with pytest.raises(cli.CliError) as raised:
        cli.reject_ignored_event_filter(["events", "--workspace", WORKSPACE_ID])
    assert raised.value.code == "usage_error"
    assert "--resource-type" in str(raised.value)
    # The `--flag=value` form too, which a bare membership test would miss.
    with pytest.raises(cli.CliError):
        cli.reject_ignored_event_filter(["events", f"--workspace={WORKSPACE_ID}"])
    # And it must not fire for the verbs that legitimately take a workspace.
    cli.reject_ignored_event_filter(["node", "--workspace", WORKSPACE_ID])


def test_event_paging_stays_inside_the_routes_own_bounds() -> None:
    """Paging is part of the contract: `limit` is 1-500, `offset` is >= 0.

    The CLI's default has to be inside the server's range, and the totals have to be
    surfaced — a caller who cannot tell a first page from a whole result set will
    read 50 events as "all of them".
    """
    limit = route_signature("events.py", "GET", "/events")["limit"][1]
    assert (limit["ge"], limit["le"], limit["default"]) == (1, 500, 50)

    api, result = run(["events"], {("GET", cli.API_BASE + "/events"): {"events": [{"id": "e1"}], "total": 137, "offset": 0}})
    assert api.query("GET", cli.API_BASE + "/events") == {"limit"}
    assert "limit=50" in api.sent[0][1]
    assert result["detail"]["total"] == 137
    assert result["detail"]["offset"] == 0


def test_provider_verbs_use_the_domain_credential_lifecycle_not_invented_routes() -> None:
    """Mismatch 5: `/providers` — invented, unallowlisted, unreachable.

    The canonical lifecycle is `/vault/credentials`, which holds the opaque
    reference only. This is the mismatch that made all three provider verbs 404.
    """
    for method, path in (("GET", "/providers"), ("POST", "/providers"), ("DELETE", f"/providers/{DOMAIN_RECORD}")):
        assert not forwardable(method, cli.API_BASE + path), f"{method} {path} is not a route"

    api, result = run(["provider", "list"])
    assert api.paths() == [("GET", DOMAIN_LIST)]
    assert forwardable("GET", DOMAIN_LIST)
    # Read from `credentials`, the field CredentialListResponse actually declares.
    assert result["detail"]["providers"][0]["adp_credential_id"] == VAULT_REFERENCE


def test_provider_delete_addresses_each_store_with_its_own_identifier() -> None:
    """Mismatch 6: one id was sent to both stores.

    The domain row's `id` and the vault reference it points at are different values.
    Sending the domain id to ADP's vault deletes nothing there, so the secret
    survived while the command reported success. The reference must also be read
    BEFORE the record is deleted — afterwards the row carrying it is gone, which is
    the other half of the same defect.
    """
    api, result = run(["provider", "delete", DOMAIN_RECORD, "--yes"])

    assert api.paths() == [
        ("GET", DOMAIN_LIST),
        ("DELETE", f"{DOMAIN_LIST}/{DOMAIN_RECORD}"),
        ("DELETE", f"{cli.VAULT_CREDENTIALS}/{VAULT_REFERENCE}"),
    ]
    assert result["detail"]["domain_record"] == DOMAIN_RECORD
    assert result["detail"]["vault_credential"] == "deleted"
    # The two ids are genuinely different, so a suite that conflated them could not
    # have detected the defect. The domain's is a UUID; the vault's is opaque.
    assert DOMAIN_RECORD != VAULT_REFERENCE
    assert route_signature("accounts.py", "DELETE", "/vault/credentials/{credential_id}")["credential_id"][0] == "uuid.UUID"


def test_account_delete_uses_the_record_uuid_not_the_cloud_account_number() -> None:
    """Mismatch 7: `DELETE /accounts/{account_id}` takes `uuid.UUID`.

    The 12-digit cloud number this verb used to send is not a UUID, so it was a
    422. The number stays acceptable at the CLI and is resolved through the list
    route, because it is the identifier a person actually has.
    """
    assert route_signature("accounts.py", "DELETE", "/accounts/{account_id}")["account_id"][0] == "uuid.UUID"
    assert cli.looks_like_uuid(ACCOUNT_RECORD) and not cli.looks_like_uuid(ACCOUNT_NUMBER)

    api, _ = run(["account", "delete", ACCOUNT_NUMBER, "--yes"])
    assert api.paths() == [
        ("GET", cli.API_BASE + "/accounts"),
        ("DELETE", f"{cli.API_BASE}/accounts/{ACCOUNT_RECORD}"),
    ]


def test_account_registration_uses_the_gateway_adapter_contract() -> None:
    fields = RegisterAccountRequest.model_fields
    assert fields["role_arn"].is_required() and fields["external_id"].is_required()

    api, result = run(
        [
            "aws-onboard",
            "register",
            "--account-id",
            ACCOUNT_NUMBER,
            "--credential-id",
            VAULT_REFERENCE,
            "--yes",
        ]
    )
    body = api.body("POST", cli.API_BASE + "/accounts")
    assert body == {
        "name": ACCOUNT_NUMBER,
        "provider": "aws",
        "account_id": ACCOUNT_NUMBER,
        "adp_credential_id": VAULT_REFERENCE,
    }
    assert "role_arn" not in body and "external_id" not in body
    assert result["detail"]["account"]["id"] == ACCOUNT_RECORD
    assert forwardable("POST", cli.API_BASE + "/accounts")


def test_no_authorized_adp_route_hands_the_client_the_two_missing_fields() -> None:
    """Why refusing is the answer, and not a missing lookup.

    If any authorized ADP route returned `role_arn` and `external_id` for an
    existing connection, the CLI could resolve them and complete the registration.
    None does: `CredentialResponse` is documented as never including secret values
    or ARNs and carries neither field. Pinning that stops the unavailable state from
    being "fixed" by inventing a client-side resolution, which would put
    trust-policy material through the CLI for no gain.
    """
    for forbidden in ("role_arn", "external_id", "secret_arn", "value"):
        assert forbidden not in CredentialResponse.model_fields, f"{forbidden} would be a second route to role material"


def test_the_deployment_name_pattern_is_the_servers_own() -> None:
    """Mismatch 9: `--name` was documented as generated when omitted.

    Nothing generated it, and the name becomes a Kubernetes object name with a
    pattern. Validating locally turns a round trip into an immediate, specific error
    — but only if the local rule IS the server's rule, so the pattern is read from
    the model and every rejected value is checked against it too.
    """
    pattern = next(item.pattern for item in CreateDeploymentRequest.model_fields["name"].metadata if hasattr(item, "pattern"))
    assert pattern == "^[a-z0-9][a-z0-9-]*[a-z0-9]$"
    assert CreateDeploymentRequest.model_fields["name"].is_required()

    for rejected in ("Llama-8B", "-leading", "trailing-", "under_score"):
        with pytest.raises(cli.CliError) as raised:
            cli.deployment_name(rejected)
        assert raised.value.code == "usage_error"
        with pytest.raises(pydantic.ValidationError):
            CreateDeploymentRequest.model_validate({"name": rejected, "model_name": "m"})
    assert cli.deployment_name("llama-8b") == "llama-8b"


def test_a_secret_on_the_command_line_is_still_refused() -> None:
    """Unchanged by this repair, and asserted so it stays that way.

    A value in argv is in the shell history and in every process listing before any
    warning could print, so there is nothing left to un-leak and the only safe
    answer is to refuse it.
    """
    for flag in cli.SECRET_FLAGS:
        for argument in (flag, f"{flag}=synthetic-value"):
            with pytest.raises(cli.CliError) as raised:
                cli.reject_secret_arguments(["provider", "add", argument, "synthetic-value"])
            assert raised.value.code == "secret_in_argv"


@pytest.mark.parametrize(
    "argv",
    [
        ["workspace", "create", "--name", "preview", "--dry-run"],
        ["quota", "set", "--max-gpus", "1", "--dry-run"],
        ["deploy", "create", *DEPLOY_CREATE_FLAGS, "--name", "preview", "--model", "model", "--dry-run"],
        ["deploy", "delete", *DEPLOY_AUTH_FLAGS, "--id", DOMAIN_RECORD, "--dry-run"],
        ["account", "delete", ACCOUNT_NUMBER, "--dry-run"],
        ["provider", "add", "--name", "preview", "--provider", "nebius", "--dry-run"],
        ["provider", "delete", DOMAIN_RECORD, "--dry-run"],
    ],
)
def test_every_remote_mutation_dry_run_sends_no_mutating_request(argv) -> None:
    api, result = run(argv)

    assert result["detail"]["dry_run"] is True
    assert all(method == "GET" for method, _path, _body in api.sent)


def test_noninteractive_mutation_without_yes_is_refused_before_the_write() -> None:
    api = Recorder()
    args = cli.parser().parse_args(["workspace", "create", "--name", "unapproved"])

    with pytest.raises(cli.CliError) as raised:
        cli.run(args, api)

    assert raised.value.code == "usage_error"
    assert api.sent == []


def test_declining_confirmation_sends_no_mutating_request(monkeypatch) -> None:
    class InteractiveInput(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdin", InteractiveInput("no\n"))
    api = Recorder()
    args = cli.parser().parse_args(["workspace", "create", "--name", "declined"])

    with pytest.raises(cli.CliError) as raised:
        cli.run(args, api)

    assert raised.value.code == "cancelled"
    assert api.sent == []


def test_the_allowlist_is_not_widened_to_admit_the_retired_paths() -> None:
    """Out of scope, explicitly: the gateway must not be relaxed to fit the CLI.

    The easy way to make the old requests "work" was to add the invented paths to
    the allowlist. That would expose routes the domain does not serve and
    re-legitimise the wrong contract, so the CLI moved instead. The file and the
    proxy's compiled table are compared too, so this cannot be satisfied by editing
    only one of them.
    """
    allowlist = {tuple(entry) for entry in json.loads(ALLOWLIST_FILE.read_text())}
    assert len(allowlist) == len(ROUTES)
    for retired in (
        ("GET", "/providers"),
        ("POST", "/providers"),
        ("DELETE", "/providers/{credential_id}"),
        ("GET", "/cost/summary"),
        ("GET", "/workspaces/{workspace_id}/kubeconfig"),
        ("GET", "/aws/onboarding-plan"),
        ("POST", "/aws/accounts"),
    ):
        assert retired not in allowlist, f"{retired} must not be admitted to the allowlist"


def test_deployment_delete_uses_uuid_and_reports_pending():
    path = f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/deployments/{DOMAIN_RECORD}"
    api, result = run(
        ["deploy", "delete", *DEPLOY_AUTH_FLAGS, "--id", DOMAIN_RECORD, "--yes"],
        {("DELETE", path): {"name": "llama-8b", "status": "Deleting"}},
    )
    assert any(method == "DELETE" and target == path for method, target, _ in api.sent)
    assert result["status"] == "pending"
    with pytest.raises(cli.CliError, match="deployment UUID"):
        run(["deploy", "delete", *DEPLOY_AUTH_FLAGS, "--id", "llama-8b", "--yes"])


@pytest.mark.parametrize(
    "subcommand,flags",
    [
        ("create", [*DEPLOY_CREATE_FLAGS, "--name", "llama-8b", "--model", "model"]),
        ("preview", ["--operation-id", DEPLOY_REQUEST, "--profile-id", "fixture-serving", "--name", "llama-8b", "--model", "model"]),
        ("list", []),
        ("delete", [*DEPLOY_AUTH_FLAGS, "--id", DOMAIN_RECORD]),
        ("teardown-preview", ["--operation-id", DEPLOY_REQUEST, "--id", DOMAIN_RECORD]),
    ],
)
def test_deployment_namespace_is_server_owned(subcommand, flags):
    if subcommand in {"create", "preview"}:
        parsed = cli.parser().parse_args(["deploy", subcommand, *flags, "--namespace", "kube-system"])
        assert parsed.namespace == "kube-system"  # An expectation, never a namespace override.
    else:
        with pytest.raises(cli.CliError, match="unrecognized arguments"):
            cli.parser().parse_args(["deploy", subcommand, *flags, "--namespace", "kube-system"])


def deployment_preview_arguments(teardown=False):
    flags = ["--workspace", WORKSPACE_ID, "--operation-id", DEPLOY_REQUEST]
    if teardown:
        return ["deploy", "teardown-preview", *flags, "--id", DOMAIN_RECORD]
    return ["deploy", "preview", *flags, "--name", "llama-8b", "--model", "model", "--profile-id", "fixture-serving"]


@pytest.mark.parametrize("teardown", [False, True])
def test_deployment_review_issues_exact_approval_without_deciding_or_dispatching(teardown):
    argv = deployment_preview_arguments(teardown)
    path = f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/deployments"
    path += f"/{DOMAIN_RECORD}/teardown-preview" if teardown else "/preview"
    api, review = run(argv)
    assert api.paths() == [("POST", path)]
    model = DeleteDeploymentRequest if teardown else CreateDeploymentRequest
    assert str(model.model_validate(api.body("POST", path)).operation_id) == DEPLOY_REQUEST
    assert review["detail"]["request_id"] == DEPLOY_REQUEST
    assert review["detail"]["allocation_id"] == ACCOUNT_RECORD

    api, issued = run([*argv, "--request-approval", "--plan-revision", DEPLOY_REVISION, "--yes"])
    assert api.paths() == [("POST", path), ("POST", cli.API_BASE + "/operation-approvals")]
    assert all(forwardable(method, target) for method, target in api.paths())
    assert api.body("POST", cli.API_BASE + "/operation-approvals") == review["detail"]["approval_request"]
    assert issued["detail"]["approval"] == {"approval_id": DEPLOY_APPROVAL, "state": "pending"}
    assert cli.create_recoveries() == {}


@pytest.mark.parametrize("teardown", [False, True])
def test_deployment_submission_preserves_exact_preview_request(teardown):
    argv = deployment_preview_arguments(teardown)
    preview_api, _ = run(argv)
    preview_body = preview_api.sent[-1][2]
    mutation_argv = list(argv)
    mutation_argv[1] = "delete" if teardown else "create"
    api, result = run([*mutation_argv, "--approval-id", DEPLOY_APPROVAL, "--plan-revision", DEPLOY_REVISION, "--yes"])
    method = "DELETE" if teardown else "POST"
    body = next(body for sent_method, _, body in api.sent if sent_method == method)
    assert body == {**preview_body, "approval_id": DEPLOY_APPROVAL, "plan_revision": DEPLOY_REVISION}
    model = DeleteDeploymentRequest if teardown else CreateDeploymentRequest
    validated = model.model_validate(body)
    assert str(validated.approval_id) == DEPLOY_APPROVAL
    assert validated.plan_revision == DEPLOY_REVISION
    assert "operation_id" in result["detail"] and "operation_state" in result["detail"]


@pytest.mark.parametrize("change", ["revision", "request_id", "deployment_id", "approval_request", "approval_key"])
def test_changed_or_malformed_deployment_review_cannot_issue_approval(change):
    path = f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/deployments/preview"
    review = Recorder()._default("POST", path, {"operation_id": DEPLOY_REQUEST})
    if change == "approval_key":
        review["approval_request"]["idempotency_key"] = DEPLOY_APPROVAL
    else:
        review[change] = "b" * 64 if change == "revision" else "changed"
    api = Recorder({("POST", path): review})
    argv = [*deployment_preview_arguments(), "--request-approval", "--plan-revision", DEPLOY_REVISION, "--yes"]
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), api)
    assert raised.value.code == ("plan_changed" if change == "revision" else "invalid_deployment_preview")
    assert api.paths() == [("POST", path)]


@pytest.mark.parametrize("teardown", [False, True])
def test_deployment_preview_dry_run_requests_nothing(teardown):
    api, result = run([*deployment_preview_arguments(teardown), "--request-approval", "--plan-revision", DEPLOY_REVISION, "--dry-run"])
    assert api.sent == []
    assert result["detail"]["performed"] == "nothing"


def test_deployment_request_id_cannot_be_reused_for_changed_inputs():
    argv = ["deploy", "create", *DEPLOY_CREATE_FLAGS, "--workspace", WORKSPACE_ID, "--name", "llama-8b", "--model", "model", "--yes"]
    first, _ = run(argv)
    assert next(body for method, _, body in first.sent if method == "POST")["operation_id"] == DEPLOY_REQUEST
    changed = list(argv)
    changed[changed.index("model")] = "another-model"
    api = Recorder()
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(changed), api)
    assert raised.value.code == "create_identity_conflict"
    assert all(method == "GET" for method, _, _ in api.sent)
    assert len(cli.create_recoveries()) == 1


def test_uncertain_teardown_retry_keeps_original_request_body():
    class LostReply(Recorder):
        def request(self, method, path, body=None, **kwargs):
            if method == "DELETE":
                self.sent.append((method, path, body))
                raise cli.CliError("reply lost", "gateway_unavailable")
            return super().request(method, path, body, **kwargs)

    argv = ["deploy", "delete", *DEPLOY_AUTH_FLAGS, "--workspace", WORKSPACE_ID, "--id", DOMAIN_RECORD, "--yes"]
    first = LostReply()
    with pytest.raises(cli.CliError) as raised:
        cli.run(cli.parser().parse_args(argv), first)
    assert raised.value.code == "teardown_delivery_uncertain"
    assert DEPLOY_REQUEST in str(raised.value)
    retry, _ = run(argv)
    assert retry.sent == first.sent


def test_deployment_list_preserves_durable_operation_and_provider_identity():
    path = f"{cli.API_BASE}/workspaces/{WORKSPACE_ID}/deployments"
    row = {
        "deployment_id": DOMAIN_RECORD,
        "status": "Unknown",
        "operation_id": DEPLOY_REQUEST,
        "operation_state": "unknown",
        "provider_uid": "original-provider-uid",
    }
    _, result = run(["deploy", "list", "--workspace", WORKSPACE_ID], {("GET", path): {"deployments": [row]}})
    assert result["detail"]["deployments"] == [row]


@pytest.mark.parametrize("flag", ["--operation-id", "--profile-id", "--approval-id", "--plan-revision"])
def test_deployment_create_requires_explicit_review_and_identity(flag):
    argv = ["deploy", "create", *DEPLOY_CREATE_FLAGS, "--name", "llama-8b", "--model", "model", "--yes"]
    offset = argv.index(flag)
    del argv[offset : offset + 2]
    with pytest.raises(cli.CliError, match="required"):
        cli.parser().parse_args(argv)
