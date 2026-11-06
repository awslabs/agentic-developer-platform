"""The metadata bootstrap produces usable configuration without asserting consent."""

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location("atlassian_metadata_bootstrap", ROOT / "scripts/prepare-atlassian-mcp.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
ACCOUNT = "123456789012"
REGION = "us-east-1"
REVISION = "a" * 40
PREFIX = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:adp/test/connections/atlassian-mcp/"
PROTECTED = {
    "resource": bootstrap.RESOURCE,
    "authorization_servers": ["https://auth.atlassian.com/test-issuer"],
    "scopes_supported": sorted(bootstrap.REQUIRED_SCOPES),
}
AUTHORIZATION = {
    "issuer": PROTECTED["authorization_servers"][0],
    "authorization_endpoint": "https://auth.atlassian.com/test-issuer/authorize",
    "token_endpoint": "https://auth.atlassian.com/test-issuer/token",
    "registration_endpoint": "https://auth.atlassian.com/test-issuer/dcr/register",
    "code_challenge_methods_supported": ["S256"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
}


class MissingVersionError(Exception):
    response = {"Error": {"Code": "ResourceNotFoundException"}}


class Store:
    def __init__(self):
        self.plan = None
        self.writes = []

    def describe_secret(self, **kwargs):
        secret_id = kwargs["SecretId"]
        kind = secret_id.rsplit("/", 1)[-1]
        return {"Name": secret_id, "ARN": PREFIX + kind + "-ABCdef"}

    def get_secret_value(self, **kwargs):
        assert kwargs["SecretId"] == PREFIX + "config-ABCdef", "The bootstrap must not read user or registration credentials"
        if self.plan is None:
            raise MissingVersionError()
        return {"SecretString": json.dumps(self.plan)}

    def put_secret_value(self, **kwargs):
        assert kwargs["SecretId"] == PREFIX + "config-ABCdef", "Only the provider plan may be written"
        self.writes.append(kwargs)
        self.plan = json.loads(kwargs["SecretString"])


@pytest.fixture
def context(tmp_path):
    args = SimpleNamespace(
        account_id=ACCOUNT,
        region=REGION,
        environment="test",
        gateway_url="https://gateway.example.test",
        contract_revision=REVISION,
        output_dir=str(tmp_path / "private"),
        publish=False,
    )
    sts = SimpleNamespace(get_caller_identity=lambda: {"Account": ACCOUNT})
    store = Store()
    requests = []

    def fetch(url):
        requests.append(url)
        return copy.deepcopy(PROTECTED if url == bootstrap.METADATA_URL else AUTHORIZATION)

    return args, sts, store, fetch, requests


def prepare(context):
    args, sts, store, fetch, _ = context
    return bootstrap.prepare(args, sts=sts, store=store, fetch=fetch)


def test_prepares_real_loader_contract_and_private_terraform_pins(context, monkeypatch):
    result = prepare(context)
    args, _, store, _, requests = context
    directory = Path(args.output_dir)
    plan = json.loads((directory / "provider-plan.json").read_text())
    pins = json.loads((directory / "atlassian-mcp.tfvars.json").read_text())
    assert result["status"] == "prepared" and store.writes == []
    assert requests == [
        bootstrap.METADATA_URL,
        "https://auth.atlassian.com/.well-known/oauth-authorization-server/test-issuer",
    ]
    assert pins == {
        "atlassian_mcp_contract_revision": REVISION,
        "atlassian_mcp_metadata_sha256": plan["metadata_sha256"],
    }
    assert directory.stat().st_mode & 0o077 == 0
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())
    assert "compatibility" not in json.dumps(plan) and "policy_result" not in plan
    monkeypatch.syspath_prepend(str(ROOT / "modules/gateway"))
    from src.shared.identity.oauth_setup import atlassian_mcp_config

    store.plan = plan
    settings = SimpleNamespace(
        atlassian_config_ref=PREFIX + "config-ABCdef",
        atlassian_contract_revision=REVISION,
        atlassian_metadata_sha256=plan["metadata_sha256"],
        aws_region=REGION,
    )
    assert atlassian_mcp_config(settings, store) == plan


def test_publishes_empty_plan_once_and_reuses_identical_configuration(context):
    args, _, store, _, _ = context
    args.publish = True
    assert prepare(context)["status"] == "published"
    assert prepare(context)["status"] == "unchanged"
    assert len(store.writes) == 1
    assert len(store.writes[0]["ClientRequestToken"]) == 64
    assert store.plan["client_id"] == ""
    assert store.plan["registration_mode"] == "dynamic-client-registration"


def test_changed_retained_plan_is_never_overwritten(context):
    args, _, store, _, _ = context
    args.publish = True
    prepare(context)
    original = copy.deepcopy(store.plan)
    args.contract_revision = "b" * 40
    with pytest.raises(bootstrap.PreparationError, match="different retained plan"):
        prepare(context)
    assert store.plan == original and len(store.writes) == 1


def test_retained_pins_survive_unrelated_source_changes_without_metadata_requests(context):
    args, sts, store, _, _ = context
    args.publish = True
    prepare(context)
    args.publish = False
    args.use_retained = True
    args.contract_revision = "b" * 40

    def unavailable(_url):
        pytest.fail("A retained plan must not need live provider metadata")

    result = bootstrap.prepare(args, sts=sts, store=store, fetch=unavailable)
    assert result["status"] == "prepared"
    assert result["atlassian_mcp_contract_revision"] == REVISION
    assert json.loads((Path(args.output_dir) / "atlassian-mcp.tfvars.json").read_text()) == {
        "atlassian_mcp_contract_revision": REVISION,
        "atlassian_mcp_metadata_sha256": store.plan["metadata_sha256"],
    }
    assert len(store.writes) == 1


def test_retained_reader_does_not_create_pins_for_an_empty_container(context):
    args, sts, store, _, _ = context
    args.use_retained = True
    result = bootstrap.prepare(args, sts=sts, store=store, fetch=lambda _: pytest.fail("Unexpected provider request"))
    assert result["status"] == "unconfigured"
    assert list(Path(args.output_dir).iterdir()) == [] and store.writes == []


def test_retained_reader_fails_closed_on_changed_plan_or_gateway_origin(context):
    args, sts, store, _, _ = context
    args.publish = True
    prepare(context)
    args.publish = False
    args.use_retained = True
    store.plan["metadata_sha256"] = "0" * 64
    with pytest.raises(bootstrap.PreparationError, match="different retained plan"):
        prepare(context)
    assert store.writes and len(store.writes) == 1
    store.plan["metadata_sha256"] = json.loads((Path(args.output_dir) / "provider-plan.json").read_text())["metadata_sha256"]
    args.gateway_url = "https://other.example.test"
    with pytest.raises(bootstrap.PreparationError, match="different retained plan"):
        prepare(context)


def test_retained_reader_cannot_publish(context):
    args, _, store, _, _ = context
    args.use_retained = True
    args.publish = True
    with pytest.raises(bootstrap.PreparationError, match="cannot publish"):
        prepare(context)
    assert store.writes == []


def test_wrong_account_stops_before_metadata_or_secrets(context):
    args, sts, store, _, requests = context
    args.publish = True
    sts.get_caller_identity = lambda: {"Account": "999999999999"}
    with pytest.raises(bootstrap.PreparationError, match="active AWS identity"):
        prepare(context)
    assert requests == [] and store.writes == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "https://auth.atlassian.com/another-issuer"),
        ("authorization_endpoint", "https://attacker.example/authorize"),
        ("token_endpoint", "http://auth.atlassian.com/token"),
        (
            "registration_endpoint",
            "https://auth.atlassian.com/register?redirect=elsewhere",
        ),
        ("code_challenge_methods_supported", ["plain"]),
        ("grant_types_supported", ["authorization_code"]),
        ("token_endpoint_auth_methods_supported", ["none"]),
        ("grant_types_supported", "authorization_code"),
    ],
)
def test_unqualified_provider_metadata_never_publishes(context, field, value):
    args, sts, store, fetch, _ = context
    args.publish = True

    def changed(url):
        result = fetch(url)
        if url != bootstrap.METADATA_URL:
            result[field] = value
        return result

    with pytest.raises(bootstrap.PreparationError):
        bootstrap.prepare(args, sts=sts, store=store, fetch=changed)
    assert store.writes == [] and list(Path(args.output_dir).iterdir()) == []


def test_missing_write_scope_is_not_treated_as_full_contract(context):
    args, sts, store, fetch, _ = context
    args.publish = True

    def missing(url):
        result = fetch(url)
        if url == bootstrap.METADATA_URL:
            result["scopes_supported"].remove("write:jira:agent-interface")
        return result

    with pytest.raises(bootstrap.PreparationError, match="required MCP OAuth contract"):
        bootstrap.prepare(args, sts=sts, store=store, fetch=missing)
    assert store.writes == []


@pytest.mark.parametrize("change", ["wrong-account", "wrong-name", "deleting"])
def test_only_existing_target_owned_secret_containers_are_used(context, change):
    args, _, store, _, _ = context
    args.publish = True
    original = store.describe_secret

    def changed(**kwargs):
        result = original(**kwargs)
        if change == "wrong-account":
            result["ARN"] = result["ARN"].replace(ACCOUNT, "999999999999")
        elif change == "wrong-name":
            result["Name"] = "another-container"
        else:
            result["DeletedDate"] = "pending"
        return result

    store.describe_secret = changed
    with pytest.raises(bootstrap.PreparationError, match="Terraform secret containers"):
        prepare(context)
    assert store.writes == []


def test_lost_publish_reply_reconciles_without_rewriting(context):
    args, _, store, _, _ = context
    args.publish = True
    original = store.put_secret_value

    def timeout_after_write(**kwargs):
        original(**kwargs)
        raise TimeoutError()

    store.put_secret_value = timeout_after_write
    with pytest.raises(TimeoutError):
        prepare(context)
    assert prepare(context)["status"] == "unchanged" and len(store.writes) == 1


def test_existing_output_symlink_cannot_replace_another_file(context, tmp_path):
    args, _, store, _, _ = context
    args.publish = True
    directory = Path(args.output_dir)
    directory.mkdir(mode=0o700)
    foreign = tmp_path / "foreign"
    foreign.write_text("retain")
    (directory / "provider-plan.json").symlink_to(foreign)
    with pytest.raises(bootstrap.PreparationError, match="conflicting files"):
        prepare(context)
    assert foreign.read_text() == "retain" and store.writes == []


@pytest.mark.parametrize(
    "status,body",
    [(302, b""), (200, b"x" * (bootstrap.MAX_METADATA_BYTES + 1)), (200, b"not-json")],
)
def test_metadata_transport_rejects_redirects_oversize_and_invalid_json(monkeypatch, status, body):
    class Response:
        def __init__(self):
            self.status = status
            self.offset = 0

        def read(self, count):
            chunk = body[self.offset : self.offset + count]
            self.offset += len(chunk)
            return chunk

    class Connection:
        def __init__(self, host, timeout):
            assert host == "mcp.atlassian.com" and timeout == 5

        def request(self, method, path, headers):
            assert method == "GET" and "Authorization" not in headers

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(bootstrap.http.client, "HTTPSConnection", Connection)
    with pytest.raises(bootstrap.PreparationError):
        bootstrap.fetch_metadata(bootstrap.METADATA_URL)
