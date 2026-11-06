"""Read-only observation of the schema the deployed release actually serves.

The serving pods admit no inbound connection from the evaluator's instance --
see the note at the top of `remote/product_probe.py` -- so these tests pin the
one path that is admitted: an authenticated read of the served schema document
through the reviewed gateway origin, once per inventoried serving pod, refused
unless every read serves the selected Product API and every read serves the
same one -- compared over that selected surface, canonically, because one
image serves one contract and not one byte sequence.
"""

import ast
import errno
import json
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path

import pytest

from tests.e2e.cli_uplift import bundle, cleanup, live, ports

sys.path.insert(0, str(Path(__file__).resolve().parent / "remote"))
import common
import product_probe


SOURCE = "a" * 40
DIGEST = "sha256:" + "b" * 64
GATEWAY = "https://gateway.example.invalid"
TOKEN = "session-access-token"
SCHEMA = {
    "openapi": "3.1.0",
    "info": {"title": "BedrockGateway"},
    "paths": {},
}
for (path, method), operation in product_probe.OPERATIONS.items():
    SCHEMA["paths"].setdefault(path, {})[method] = {"operationId": operation}


def test_required_probe_operations_match_product_route_source():
    source = (
        Path(__file__).resolve().parents[3] / "modules/gateway/src/products/routes.py"
    )
    tree = ast.parse(source.read_text())
    operations = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(
                decorator.func, ast.Attribute
            ):
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "operation_id" and isinstance(
                    keyword.value, ast.Constant
                ):
                    operations[
                        ("/v1/products" + decorator.args[0].value, decorator.func.attr)
                    ] = keyword.value.value
    assert product_probe.OPERATIONS.items() <= operations.items()


class Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


@pytest.fixture
def probe(monkeypatch, tmp_path):
    reads = []
    schemas = [deepcopy(SCHEMA), deepcopy(SCHEMA)]
    inventory = {
        "source_revision": SOURCE,
        "digest": DIGEST,
        "pods": [
            {"uid": "pod-first", "address": "10.1.1.10", "digest": DIGEST},
            {"uid": "pod-second", "address": "10.1.1.11", "digest": DIGEST},
        ],
    }
    # The real on-instance session vault, 0600 under this run's own private
    # directory: the probe resolves it through `common.load_session`, so the
    # read is authenticated as the run's own administrator and nothing else.
    work_dir = tmp_path / "adp-eval"
    work_dir.mkdir()
    vault = common.save_session({"access_token": TOKEN}, work_dir=work_dir)

    class Opener:
        def open(self, request, *, timeout):
            reads.append((request, timeout))
            return Response(schemas[min(len(reads) - 1, len(schemas) - 1)])

    def build(*handlers):
        assert len(handlers) == 1
        assert isinstance(handlers[0], urllib.request.ProxyHandler)
        assert handlers[0].proxies == {}
        return Opener()

    monkeypatch.setattr(common.urllib.request, "build_opener", build)
    return (
        {
            "product_cases": "api_product_lifecycle",
            "expected_revision": SOURCE,
            "product_inventory": inventory,
            "gateway_url": GATEWAY,
            "work_dir": str(work_dir),
            "session_ref": vault["session_ref"],
        },
        schemas,
        reads,
    )


def test_served_schema_is_read_once_per_serving_pod_through_the_origin(probe):
    config, _schemas, reads = probe
    evidence = {}
    product_probe.execute(config, evidence)
    assert evidence["success"] is True
    assert evidence["source_revision"] == SOURCE
    assert evidence["schema_source"] == "gateway_origin"
    assert len(evidence["pods"]) == 2
    assert {item["uid"] for item in evidence["pods"]} == {"pod-first", "pod-second"}
    assert len({item["schema_sha256"] for item in evidence["pods"]}) == 1
    assert [request.full_url for request, _timeout in reads] == [
        GATEWAY + "/openapi.json",
        GATEWAY + "/openapi.json",
    ]
    assert all(request.get_method() == "GET" for request, _timeout in reads)
    assert "access_token" not in json.dumps(common.redact(evidence))


def test_schema_read_is_authenticated_as_this_runs_own_session(probe):
    config, _schemas, reads = probe
    product_probe.execute(config, {})
    headers = [request.get_header("Authorization") for request, _t in reads]
    assert headers == ["Bearer " + TOKEN, "Bearer " + TOKEN]


def test_schema_read_without_this_runs_session_never_reaches_the_network(probe):
    config, _schemas, reads = probe
    config.pop("session_ref")
    with pytest.raises(common.RemoteError):
        product_probe.execute(config, {})
    assert reads == []


@pytest.fixture
def dispatch(probe, monkeypatch, tmp_path):
    config, _schemas, reads = probe
    run_id = "adp-e2e-20261020-113000-abcdef"
    instance_id = "i-owned-instance"
    account = "000000000000"
    region = "us-east-1"
    manifest = cleanup.Manifest(
        tmp_path / "run.json", run_id, on_change=lambda *_args, **_kwargs: None
    )
    manifest.record("ec2_instance", instance_id, account=account, region=region)
    cfg = {
        "gateway_url": GATEWAY,
        "platform_account": account,
        "region": region,
        "expected_revision": SOURCE,
        "product_cases": config["product_cases"],
    }
    payload = {
        **config,
        "instance_id": instance_id,
        "evaluation_id": run_id,
        "gateway_url": cfg["gateway_url"],
        "platform_account": account,
        "region": region,
        # The dispatched payload names the instance's own directories, which do
        # not exist in this process, so the vault lookup is substituted here --
        # these cases are about what the orchestrator refuses to dispatch and
        # refuses to believe, not about the instance's session file.
        "work_dir": bundle.REMOTE_DIR,
        "session_ref": bundle.REMOTE_DIR + "/session.json",
    }
    monkeypatch.setattr(
        product_probe.common, "load_session", lambda _config: {"access_token": TOKEN}
    )
    return cfg, payload, manifest, reads


def test_protected_process_probe_dispatch_checks_instance_and_receipt(dispatch):
    cfg, payload, manifest, reads = dispatch
    sent = []

    class Ssm:
        def json_result(self, instance, commands, *, purpose, timeout):
            sent.append((instance, commands, purpose))
            receipt = {}
            product_probe.execute(payload, receipt)
            return {"Status": "Success"}, receipt

    runner = live._run_worker(Ssm(), cfg, lambda *_args: sent.append("install"))
    result = runner(payload["instance_id"], "product_probe", payload, manifest=manifest)
    assert result["success"] is True and len(result["pods"]) == len(reads) == 2
    assert sent[0] == "install"
    assert sent[1][0] == payload["instance_id"]
    assert sent[1][2] == "product_probe"
    assert "product_probe" in bundle.purposes()


@pytest.mark.parametrize(
    "change",
    [
        "no_manifest",
        "wrong_instance",
        "wrong_source",
        "wrong_selection",
        "wrong_account",
        "wrong_origin",
        "missing_inventory",
        "missing_uid",
        "duplicate_uid",
        "wrong_digest",
    ],
)
def test_protected_process_probe_refuses_unowned_payload_before_ssm(dispatch, change):
    cfg, payload, manifest, _reads = dispatch
    if change == "no_manifest":
        manifest = None
    elif change == "wrong_instance":
        payload["instance_id"] = "i-foreign-instance"
    elif change == "wrong_source":
        payload["product_inventory"]["source_revision"] = "c" * 40
    elif change == "wrong_selection":
        payload["product_cases"] = "api_denial_oracle"
    elif change == "wrong_account":
        payload["platform_account"] = "000000000001"
    elif change == "wrong_origin":
        payload["gateway_url"] = "https://foreign.example.invalid"
    elif change == "missing_uid":
        payload["product_inventory"]["pods"][0].pop("uid")
    elif change == "duplicate_uid":
        payload["product_inventory"]["pods"][1]["uid"] = "pod-first"
    elif change == "wrong_digest":
        payload["product_inventory"]["pods"][0]["digest"] = "sha256:" + "f" * 64
    else:
        del payload["product_inventory"]
    sent = []
    runner = live._run_worker(object(), cfg, lambda *_args: sent.append("install"))
    with pytest.raises(ports.PortError, match="Product process probe dispatch"):
        runner("i-owned-instance", "product_probe", payload, manifest=manifest)
    assert sent == []


@pytest.mark.parametrize(
    "change",
    ["failed", "wrong_revision", "missing_pod", "wrong_uid", "different_schema"],
)
def test_protected_process_probe_refuses_false_receipt(dispatch, change):
    cfg, payload, manifest, _reads = dispatch
    receipt = {
        "success": True,
        "source_revision": SOURCE,
        "pods": [
            {"uid": pod["uid"], "schema_sha256": "a" * 64}
            for pod in payload["product_inventory"]["pods"]
        ],
    }
    if change == "failed":
        receipt["success"] = False
    elif change == "wrong_revision":
        receipt["source_revision"] = "c" * 40
    elif change == "missing_pod":
        receipt["pods"].pop()
    elif change == "wrong_uid":
        receipt["pods"][1]["uid"] = "foreign-pod"
    else:
        receipt["pods"][1]["schema_sha256"] = "b" * 64

    class Ssm:
        def json_result(self, *args, **kwargs):
            return {"Status": "Success"}, receipt

    runner = live._run_worker(Ssm(), cfg, lambda *_args: None)
    with pytest.raises(ports.PortError, match="every selected serving pod"):
        runner(payload["instance_id"], "product_probe", payload, manifest=manifest)


@pytest.mark.parametrize(
    "change",
    [
        "wrong_case",
        "wrong_revision",
        "missing_pods",
        "public_ip",
        "loopback",
        "duplicate_ip",
        "duplicate_uid",
        "wrong_digest",
        "malformed_address",
    ],
)
def test_invalid_inventory_never_reaches_the_network(probe, change):
    config, _schemas, reads = probe
    inventory = config["product_inventory"]
    if change == "wrong_case":
        config["product_cases"] = "unreviewed_case"
    elif change == "wrong_revision":
        inventory["source_revision"] = "c" * 40
    elif change == "missing_pods":
        inventory["pods"] = []
    elif change == "public_ip":
        inventory["pods"][0]["address"] = "8.8.8.8"
    elif change == "loopback":
        inventory["pods"][0]["address"] = "127.0.0.1"
    elif change == "duplicate_ip":
        inventory["pods"][1]["address"] = "10.1.1.10"
    elif change == "duplicate_uid":
        inventory["pods"][1]["uid"] = "pod-first"
    elif change == "wrong_digest":
        inventory["pods"][1]["digest"] = "sha256:" + "f" * 64
    else:
        inventory["pods"][0]["address"] = "10.1.1.10/path"
    with pytest.raises(common.RemoteError):
        product_probe.execute(config, {})
    assert reads == []


@pytest.mark.parametrize(
    ("change", "expected_reads"),
    [("missing_route", 1), ("wrong_title", 1), ("mismatched_schema", 2)],
)
def test_wrong_or_disagreeing_served_schema_refuses_proof(
    probe, change, expected_reads
):
    config, schemas, reads = probe
    if change == "missing_route":
        schemas[0]["paths"].pop("/v1/products")
    elif change == "wrong_title":
        schemas[0]["info"]["title"] = "OtherService"
    else:
        schemas[1]["paths"]["/v1/products"]["post"]["deprecated"] = True
    with pytest.raises(common.RemoteError, match="schema"):
        product_probe.execute(config, {})
    assert len(reads) == expected_reads


def test_a_disagreement_names_the_selected_parts_that_differ(probe):
    """A refusal has to name the part, not just assert a disagreement."""
    config, schemas, reads = probe
    schemas[1]["paths"]["/v1/products"]["post"]["deprecated"] = True
    schemas[1]["components"] = {"schemas": {"Product": {"title": "Other"}}}
    with pytest.raises(common.RemoteError) as refusal:
        product_probe.execute(config, {})
    message = str(refusal.value)
    assert "POST /v1/products" in message
    assert "components.schemas" in message
    assert len(reads) == 2


def test_incidental_document_variation_is_not_a_fleet_disagreement(probe):
    """One image serves one contract, not one byte sequence.

    Deploy run 39950515367 refused a healthy fleet because the whole served
    document was digested byte-for-byte: a per-request `servers` entry, build
    text in `info`, and the listing order of set-derived members such as
    `required` or `tags` are not the selected Product API.
    """
    config, schemas, reads = probe
    first, second = schemas
    first["servers"] = [{"url": "https://first.invalid/api"}]
    second["servers"] = [{"url": "https://second.invalid/api"}]
    first["info"]["version"] = "1.0.0+first"
    second["info"]["version"] = "1.0.0+second"
    first["components"] = {"schemas": {"Product": {"required": ["name", "owner"]}}}
    second["components"] = {"schemas": {"Product": {"required": ["owner", "name"]}}}
    first["paths"]["/v1/products"]["post"]["tags"] = ["products", "write"]
    second["paths"]["/v1/products"]["post"]["tags"] = ["write", "products"]
    evidence = {}
    product_probe.execute(config, evidence)
    assert evidence["success"] is True
    assert len({item["schema_sha256"] for item in evidence["pods"]}) == 1
    assert len(reads) == 2


def test_unreachable_origin_names_the_transport_and_never_quotes_the_peer(
    probe, monkeypatch
):
    """A refusal has to say WHY, without publishing a single peer-supplied byte.

    "Product pod schema is unavailable" on its own cost a whole live run: it
    cannot distinguish a closed network path from a process that is not
    listening from a non-2xx answer, so the next repair is a guess.
    """
    config, _schemas, _reads = probe

    def unreachable(*_handlers):
        class Opener:
            def open(self, _request, *, timeout):
                raise urllib.error.URLError(
                    ConnectionRefusedError(errno.ECONNREFUSED, "token-bearing text")
                )

        return Opener()

    monkeypatch.setattr(common.urllib.request, "build_opener", unreachable)
    with pytest.raises(common.RemoteError) as refusal:
        product_probe.execute(config, {})
    message = str(refusal.value)
    assert message.startswith("/openapi.json unreachable: URLError")
    assert "token-bearing text" not in message


def test_refused_schema_status_names_the_status_and_never_quotes_the_peer(
    probe, monkeypatch
):
    config, _schemas, _reads = probe

    def refused(*_handlers):
        class Opener:
            def open(self, _request, *, timeout):
                return Response({"detail": "token-bearing text"}, status=404)

        return Opener()

    monkeypatch.setattr(common.urllib.request, "build_opener", refused)
    with pytest.raises(common.RemoteError) as refusal:
        product_probe.execute(config, {})
    message = str(refusal.value)
    assert "/openapi.json returned HTTP 404" in message
    assert "token-bearing text" not in message


def test_denial_selection_reads_same_private_serving_pods(probe):
    config, _schemas, reads = probe
    config["product_cases"] = "api_product_lifecycle,api_denial_oracle"
    evidence = {}
    product_probe.execute(config, evidence)
    assert evidence["product_cases"] == config["product_cases"]
    assert evidence["success"] is True
    assert len(reads) == len(config["product_inventory"]["pods"])


def test_protected_denial_probe_requires_matched_selection_and_receipt(dispatch):
    cfg, payload, manifest, reads = dispatch
    cfg["product_cases"] = "api_product_lifecycle,api_denial_oracle"
    payload["product_cases"] = cfg["product_cases"]

    class Ssm:
        def json_result(self, *_args, **_kwargs):
            evidence = {}
            product_probe.execute(payload, evidence)
            return {"Status": "Success"}, evidence

    runner = live._run_worker(Ssm(), cfg, lambda *_args: None)
    result = runner(payload["instance_id"], "product_probe", payload, manifest=manifest)
    assert result["product_cases"] == payload["product_cases"]
    assert result["success"] is True
    assert len(result["pods"]) == len(reads)


@pytest.mark.parametrize("wrong", ["selection", "receipt"])
def test_protected_denial_probe_refuses_case_mismatch(dispatch, wrong):
    cfg, payload, manifest, _reads = dispatch
    payload["product_cases"] = "api_product_lifecycle,api_denial_oracle"
    cfg["product_cases"] = (
        "api_product_lifecycle"
        if wrong == "selection"
        else "api_product_lifecycle,api_denial_oracle"
    )
    calls = []
    receipt = {
        "success": True,
        "source_revision": SOURCE,
        "product_cases": "api_product_lifecycle",
        "pods": [
            {"uid": pod["uid"], "schema_sha256": "a" * 64}
            for pod in payload["product_inventory"]["pods"]
        ],
    }

    class Ssm:
        def json_result(self, *_args, **_kwargs):
            calls.append("dispatched")
            return {"Status": "Success"}, receipt

    runner = live._run_worker(Ssm(), cfg, lambda *_args: None)
    with pytest.raises(ports.PortError, match="Product process probe"):
        runner(payload["instance_id"], "product_probe", payload, manifest=manifest)
    assert calls == ([] if wrong == "selection" else ["dispatched"])
