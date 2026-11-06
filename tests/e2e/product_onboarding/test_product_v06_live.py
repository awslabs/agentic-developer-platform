"""Deployed issue-8256 AC-01 producer, with contract and orchestration checks.

``live_sources_reuse`` is the deployed test producer the completion inventory
selects for 8256/AC-01. It performs real discovery, binding, verification,
independent readback and cleanup against the run-owned Project and applies the
reviewed SCN-01 validator to the report it assembles, so an incomplete, shared
or uncleaned observation fails the producer instead of being published.

The ``test_*`` functions keep that producer honest in ordinary repository runs
at two levels:

* contract checks drive ``build_source_ports`` against a double that answers
  what the deployed gateway actually answers - connection-kind discovery,
  ``items``/``next_cursor`` pages, a flat ``BindingInput``, mandatory
  ``Idempotency-Key`` and ``If-Profile-Revision`` headers, unlinked tombstones
  and the served ``adp product`` commands - so an adapter written against an
  invented shape fails here instead of in the live run;
* orchestration checks prove the producer retires what it created even when it
  refuses the observation, rejects provider mutation and leaked bindings, and
  names a missing endpoint or fixture instead of skipping quietly.

``live_source_status`` is the matching deployed producer for 8256/AC-02: it
observes one owned source before its pinned expiry, at the ``stale_after``
boundary the deployed binding published and through both restricted phases, on
every project surface, and applies the reviewed SCN-02 validator to the report
it assembles.

``live_source_recovery`` is the deployed producer for 8256/AC-03: it interrupts
a bounded ``limit=1`` resource page, resumes it on the ``resource_cursor`` the
deployed read published, replays one verification under a single idempotency key
and unlinks only the binding it owns while a peer binding and the confirmed
Product values stay intact, then applies the reviewed SCN-03 validator to the
report it assembles.
"""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.e2e.product_onboarding.product_v06_live import (
    PREREQUISITES,
    SourcePrerequisite,
    build_recovery_ports,
    build_source_ports,
    build_status_ports,
    flatten_binding,
    pinned_clock,
    pinned_timestamps,
    protected_report,
    recovery_attempt,
    recovery_page,
    run_source_recovery,
    run_source_reuse,
    run_source_status,
    status_attempt,
    surface_views,
)
from tests.e2e.product_onboarding.test_v06 import RECOVERY_PHASES, STATUS_ARTIFACTS, STATUS_PHASES

OWNED_CASE = "evidence"


@contextmanager
def owned_source_run(request, *, builder=build_source_ports, report="sources"):
    """Open the run-owned deployed Product fixture and wire its published ports."""
    from tests.e2e.new_ui.product_release import check_served_assets, private_capture_directory
    from tests.e2e.new_ui.test_product_resume import checked_fixture
    from tests.e2e.product_onboarding.product_v02_record import CaseEvidence

    provider = request.getfixturevalue("product_onboarding_live_provider")
    page = request.getfixturevalue("product_onboarding_live_page")
    record = CaseEvidence()
    record.cli_events = []
    directory = Path(private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"]))
    with provider.fixture(OWNED_CASE, record) as fixture:
        fixture["evidence_record"] = record
        checked_fixture(fixture, os.environ["PRODUCT_EXPECTED_REVISION"])
        check_served_assets(provider.request, provider.base_url, fixture["frontend_release"],
                            fixture["source_revision"])
        ports = builder(provider, fixture, directory, page=page)
        yield fixture, ports, directory / (fixture["run_id"] + "-v06-" + report + "-report.json")


@pytest.mark.live_only
def live_sources_reuse(request, tmp_path):
    """Produce SCN-01: real reuse of the selected sources on every independent surface."""
    if os.environ.get("PRODUCT_V06_LIVE_ENABLED") != "1":
        pytest.skip("Issue-8256 AC-01 needs the authorized deployed Product fixtures")
    for variable in ("PRODUCT_EXPECTED_REVISION", "E2E_SCREENSHOT_DIR"):
        if not os.environ.get(variable):
            raise ValueError("The deployed source producer needs " + variable + " from the pinned workflow")
    with owned_source_run(request) as (fixture, ports, report_path):
        written = run_source_reuse(ports, run_id=fixture["run_id"], tmp_path=tmp_path, report_path=report_path)
        observed = json.loads(Path(written).read_text())
    assert observed["run_id"] == fixture["run_id"] and observed["cleanup"]["remaining_owned_binding_ids"] == []
    assert observed["cleanup"]["tombstoned_binding_ids"] == observed["owned_binding_ids"]


@pytest.mark.live_only
def live_source_status(request, tmp_path):
    """Produce SCN-02: one owned source observed through expiry and restriction."""
    if os.environ.get("PRODUCT_V06_LIVE_ENABLED") != "1":
        pytest.skip("Issue-8256 AC-02 needs the authorized deployed Product fixtures")
    for variable in ("PRODUCT_EXPECTED_REVISION", "E2E_SCREENSHOT_DIR"):
        if not os.environ.get(variable):
            raise ValueError("The deployed status producer needs " + variable + " from the pinned workflow")
    with owned_source_run(request, builder=build_status_ports, report="status") as (fixture, ports, report_path):
        written = run_source_status(ports, run_id=fixture["run_id"], tmp_path=tmp_path, report_path=report_path)
        observed = json.loads(Path(written).read_text())
    assert set(observed["attempts"]) == set(STATUS_PHASES)
    assert observed["attempts"]["at_expiry"]["clock"] == observed["stale_after"]
    assert observed["cleanup"]["tombstoned_binding_ids"] == observed["owned_binding_ids"]


@pytest.mark.live_only
def live_source_recovery(request, tmp_path):
    """Produce SCN-03: an interrupted deployed source run resumed, replayed and partly unlinked."""
    if os.environ.get("PRODUCT_V06_LIVE_ENABLED") != "1":
        pytest.skip("Issue-8256 AC-03 needs the authorized deployed Product fixtures")
    for variable in ("PRODUCT_EXPECTED_REVISION", "E2E_SCREENSHOT_DIR"):
        if not os.environ.get(variable):
            raise ValueError("The deployed recovery producer needs " + variable + " from the pinned workflow")
    with owned_source_run(request, builder=build_recovery_ports,
                          report="recovery") as (fixture, ports, report_path):
        written = run_source_recovery(ports, run_id=fixture["run_id"], tmp_path=tmp_path, report_path=report_path)
        observed = json.loads(Path(written).read_text())
    assert set(observed["attempts"]) == set(RECOVERY_PHASES)
    assert observed["attempts"]["unlinked"]["remaining_binding_ids"] == [observed["preserved_binding_id"]]
    assert observed["cleanup"]["tombstoned_binding_ids"] == observed["owned_binding_ids"]


# --- The deployed contract, as answered by modules/gateway/src/products/source_routes.py ---

ROLE_REF = "arn:aws:iam::000000000000:role/eval-regression-checks"
CALLER_IDENTITY = {"Account": "000000000000", "UserId": "AROAEXAMPLE:product-v06",
                   "Arn": "arn:aws:sts::000000000000:assumed-role/eval-regression-checks/product-v06"}
REPOSITORY_PAYLOAD = {"id": 35, "owner": {"id": 27}, "updated_at": "repo-rev", "name": "adp",
                      "default_branch": "main", "visibility": "private"}
REPOSITORY_RESOURCE = {"kind": "repository", "namespace": "27", "native_id": "35", "name": "adp",
                       "source_revision": "repo-rev", "observed_at": "2026-10-20T00:00:00Z",
                       "default_branch": "main"}
OTHER_REPOSITORY = {"kind": "repository", "namespace": "27", "native_id": "99", "name": "other",
                    "source_revision": "other-rev", "observed_at": "2026-10-20T00:00:00Z"}
VERIFIED_ENVIRONMENT = {"provider": "aws", "account_id": "000000000000", "regions": ["us-east-1"],
                        "role_ref": ROLE_REF, "permission_tier": "unknown",
                        "source_revision": "secret-rev", "verified_at": "2026-10-20T00:00:00Z"}
ENVIRONMENT_RESOURCE = {"kind": "environment", "namespace": "000000000000", "native_id": "us-east-1",
                        "name": "000000000000 account, us-east-1", "source_revision": "secret-rev",
                        "observed_at": "2026-10-20T00:00:00Z", "verified_environment": VERIFIED_ENVIRONMENT}
OTHER_ENVIRONMENT = {**ENVIRONMENT_RESOURCE, "native_id": "eu-west-1",
                     "verified_environment": {**VERIFIED_ENVIRONMENT, "regions": ["eu-west-1"]}}
CONNECTIONS = (
    {"connection_ref": {"kind": "github_installation", "id": "inst-1"}, "provider": "github",
     "label": "org", "health": "unverified", "capabilities": ["repository", "pipeline"],
     "resources": [OTHER_REPOSITORY, REPOSITORY_RESOURCE]},
    {"connection_ref": {"kind": "credential", "id": "cred-1"}, "provider": "aws", "label": "dev",
     "health": "unverified", "capabilities": ["environment"],
     "resources": [OTHER_ENVIRONMENT, ENVIRONMENT_RESOURCE]},
)
FIXTURE = {"product_id": "prod-1", "project_id": "proj-1", "run_id": "example-run",
           "owner": {"access_token": "live-access-token"}}
CONTRACT_ENVIRON = {
    "PRODUCT_V06_REPOSITORY_NATIVE_ID": "35",
    "PRODUCT_V06_ENVIRONMENT_ACCOUNT_ID": "000000000000",
    "PRODUCT_V06_ENVIRONMENT_REGION": "us-east-1",
    "PRODUCT_V06_ENVIRONMENT_ROLE_REF": ROLE_REF,
    "GITHUB_TOKEN": "ghs-example-token",
}
BINDING_FIELDS = {"kind", "provider", "connection_ref", "namespace", "native_id", "environment",
                  "repository_id", "source_revision"}


class FakeResponse:
    def __init__(self, status, body, revision):
        self.status = status
        self._body = body
        self.headers = {"profile-revision": str(revision), "cache-control": "no-store"}

    def json(self):
        return json.loads(json.dumps(self._body))


class FakeGateway:
    """Answer exactly the deployed source contract, and nothing an invented one needs."""

    def __init__(self, *, tombstone="unlinked", keep_tombstone=True):
        self.revision = 7
        self.bindings = {}
        self.counter = 0
        self.calls = []
        self.tombstone = tombstone
        self.keep_tombstone = keep_tombstone

    def _record(self, method, url, headers, body=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "body": json.loads(body) if isinstance(body, str) else body})

    def _authorized(self, headers):
        return (headers or {}).get("Authorization") == "Bearer " + FIXTURE["owner"]["access_token"]

    def _preconditions(self, headers):
        key = (headers or {}).get("Idempotency-Key")
        revision = (headers or {}).get("If-Profile-Revision")
        if not key or revision is None:
            return 428
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            return 422
        if revision != str(self.revision):
            return 412
        return None

    def binding_items(self):
        return [json.loads(json.dumps(view)) for view in self.bindings.values()]

    def source_items(self, kind, identifier):
        return [json.loads(json.dumps(item)) for item in CONNECTIONS
                if item["connection_ref"] == {"kind": kind, "id": identifier}]

    def get(self, url, **options):
        headers = options.get("headers") or {}
        self._record("GET", url, headers)
        parts = urlsplit(url)
        query = {key: value[0] for key, value in parse_qs(parts.query).items()}
        if not self._authorized(headers):
            return FakeResponse(401, {"code": "unauthorized"}, self.revision)
        if parts.path.endswith("/sources"):
            if "connection_kind" in query or "connection_id" in query:
                if query.get("connection_kind") not in ("github_installation", "credential"):
                    return FakeResponse(422, {"code": "validation_error"}, self.revision)
                if ("connection_id" in query) != ("connection_kind" in query):
                    return FakeResponse(400, {"code": "invalid_cursor"}, self.revision)
                items = self.source_items(query["connection_kind"], query["connection_id"])
                if not items:
                    return FakeResponse(400, {"code": "invalid_cursor"}, self.revision)
                return FakeResponse(200, {"items": items, "next_cursor": None, "coverage": "partial"},
                                    self.revision)
            listing = []
            for item in self.source_items("github_installation", "inst-1") + self.source_items("credential", "cred-1"):
                item.pop("resources", None)
                item["coverage"] = "unavailable"
                listing.append(item)
            return FakeResponse(200, {"items": listing, "next_cursor": None, "coverage": "partial"}, self.revision)
        if parts.path.endswith("/bindings"):
            return FakeResponse(200, {"items": self.binding_items(), "next_cursor": None}, self.revision)
        if parts.path.endswith("/projects/" + FIXTURE["project_id"]):
            return FakeResponse(200, {"product_id": FIXTURE["product_id"], "project_id": FIXTURE["project_id"],
                                      "revision": self.revision}, self.revision)
        return FakeResponse(404, {"code": "not_found"}, self.revision)

    def post(self, url, **options):
        headers = options.get("headers") or {}
        self._record("POST", url, headers, options.get("data"))
        parts = urlsplit(url)
        if not self._authorized(headers):
            return FakeResponse(401, {"code": "unauthorized"}, self.revision)
        refusal = self._preconditions(headers)
        if refusal is not None:
            return FakeResponse(refusal, {"code": "precondition"}, self.revision)
        if parts.path.endswith("/verify"):
            binding_id = parts.path.split("/")[-2]
            view = self.bindings.get(binding_id)
            if view is None or view.get("status") == "unlinked":
                return FakeResponse(404, {"code": "not_found"}, self.revision)
            resource = REPOSITORY_RESOURCE if view["selection"]["kind"] == "repository" else ENVIRONMENT_RESOURCE
            view.update(status="healthy", observed_at=resource["observed_at"],
                        stale_after="2026-10-20T00:15:00Z", source_revision=resource["source_revision"])
            if "verified_environment" in resource:
                view["verified_environment"] = resource["verified_environment"]
            return FakeResponse(202, {"id": "operation-" + binding_id, "state": "succeeded"}, self.revision)
        if parts.path.endswith("/bindings"):
            body = options.get("data")
            selection = json.loads(body) if isinstance(body, str) else body
            if not isinstance(selection, dict) or set(selection) - BINDING_FIELDS or "kind" not in selection:
                return FakeResponse(422, {"code": "validation_error"}, self.revision)
            reference = selection.get("connection_ref")
            if (not isinstance(reference, dict) or set(reference) != {"kind", "id"}
                    or reference["kind"] not in ("github_installation", "credential")
                    or not all(isinstance(selection.get(field), str) and selection[field]
                               for field in ("provider", "namespace", "native_id"))):
                return FakeResponse(422, {"code": "validation_error"}, self.revision)
            if selection["kind"] == "environment" and not isinstance(selection.get("environment"), dict):
                return FakeResponse(422, {"code": "validation_error"}, self.revision)
            self.counter += 1
            binding_id = "binding-" + str(self.counter)
            self.revision += 1
            self.bindings[binding_id] = {"id": binding_id, "project_id": FIXTURE["project_id"], "generation": 1,
                                         "revision": self.revision, "selection": selection, "status": "pending"}
            return FakeResponse(201, self.binding_items()[-1], self.revision)
        return FakeResponse(404, {"code": "not_found"}, self.revision)

    def delete(self, url, **options):
        headers = options.get("headers") or {}
        self._record("DELETE", url, headers)
        if not self._authorized(headers):
            return FakeResponse(401, {"code": "unauthorized"}, self.revision)
        refusal = self._preconditions(headers)
        if refusal is not None:
            return FakeResponse(refusal, {"code": "precondition"}, self.revision)
        binding_id = urlsplit(url).path.split("/")[-1]
        view = self.bindings.get(binding_id)
        if view is None:
            return FakeResponse(404, {"code": "not_found"}, self.revision)
        self.revision += 1
        stone = {"id": binding_id, "project_id": view["project_id"], "generation": view["generation"],
                 "revision": self.revision, "status": self.tombstone}
        if self.keep_tombstone:
            self.bindings[binding_id] = dict(stone)
        else:
            self.bindings.pop(binding_id)
        return FakeResponse(200, stone, self.revision)


def contract_ports(gateway, directory, *, cli_calls=None, environ=None, page=None):
    """Wire the real adapter to the contract double, with a served-CLI stand-in."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    provider = SimpleNamespace(base_url="https://product.example.test", request=gateway)

    def runner(arguments):
        if cli_calls is not None:
            cli_calls.append(list(arguments))
        if arguments[0] == "sources":
            return {"items": gateway.source_items("github_installation", "inst-1"), "next_cursor": None,
                    "next_prerequisites": []}
        if arguments[0] == "source-bindings":
            return {"items": gateway.binding_items(), "next_cursor": None}
        raise AssertionError("The installed client was asked for an unpublished command")

    return build_source_ports(provider, dict(FIXTURE), directory, page=page,
                              environ=dict(CONTRACT_ENVIRON if environ is None else environ), cli=runner,
                              fetch=lambda url, token: json.loads(json.dumps(REPOSITORY_PAYLOAD)),
                              identity=lambda: json.loads(json.dumps(CALLER_IDENTITY)))


class FakeExpectResponse:
    def __init__(self, response):
        self.value = response

    def __enter__(self):
        return self

    def __exit__(self, *details):
        return False


class FakePage:
    """Serve the UI's own bindings response, in its real envelope."""

    def __init__(self, gateway):
        self.gateway = gateway
        self.sections = []

    def expect_response(self, predicate):
        body = {"items": self.gateway.binding_items(), "next_cursor": None}
        url = "https://product.example.test/api/v1/products/prod-1/projects/proj-1/bindings?limit=50"
        hit = SimpleNamespace(url=url, request=SimpleNamespace(method="GET"), json=lambda: body)
        assert predicate(hit), "The browser capture must match the served bindings response"
        assert not predicate(SimpleNamespace(url=url, request=SimpleNamespace(method="POST"), json=lambda: body))
        return FakeExpectResponse(hit)


@pytest.fixture
def stub_browser(monkeypatch):
    helpers = ModuleType("tests.e2e.chat.helpers")
    helpers.inject_tokens_and_navigate = lambda page, owner, path=None: None
    navigation = ModuleType("tests.e2e.new_ui.product_navigation")
    navigation.open_product_section = lambda page, section=None: page.sections.append(section)
    monkeypatch.setitem(sys.modules, "tests.e2e.chat.helpers", helpers)
    monkeypatch.setitem(sys.modules, "tests.e2e.new_ui.product_navigation", navigation)
    return navigation


def source_queries(gateway):
    return [{key: value[0] for key, value in parse_qs(urlsplit(call["url"]).query).items()}
            for call in gateway.calls if call["url"].split("?")[0].endswith("/sources")]


def test_discovery_uses_connection_kinds_and_only_run_authorized_resources(tmp_path):
    gateway = FakeGateway()
    selections = contract_ports(gateway, tmp_path / "captures")["discover"]()
    queries = source_queries(gateway)
    assert queries[0] == {"limit": "50"}, "The first sources read must enumerate the authorized connections"
    assert {query.get("connection_kind") for query in queries[1:]} == {"github_installation", "credential"}
    assert all(query.get("connection_kind") not in ("repository", "environment") for query in queries)
    assert all(query.get("connection_id") for query in queries[1:])
    assert selections["repository"] == {
        "kind": "repository", "provider": "github",
        "connection_ref": {"kind": "github_installation", "id": "inst-1"},
        "namespace": "27", "native_id": "35",
    }, "Only the repository identity this run is authorized to select may be bound"
    assert selections["environment"]["connection_ref"] == {"kind": "credential", "id": "cred-1"}
    assert selections["environment"]["native_id"] == "us-east-1"
    assert selections["environment"]["namespace"] == "000000000000"
    assert selections["environment"]["environment"] == {
        "name": ENVIRONMENT_RESOURCE["name"], "environment_class": "development", "production": False,
    }


def test_discovery_refuses_when_no_authorized_resource_is_returned(tmp_path):
    cases = {
        "repository_selection": {**CONTRACT_ENVIRON, "PRODUCT_V06_REPOSITORY_NATIVE_ID": "4242"},
        "environment_selection": {**CONTRACT_ENVIRON, "PRODUCT_V06_ENVIRONMENT_REGION": "ap-south-1"},
        "authorized_identities": {"GITHUB_TOKEN": "ghs-example-token"},
    }
    for name, environ in cases.items():
        ports = contract_ports(FakeGateway(), tmp_path / name, environ=environ)
        with pytest.raises(SourcePrerequisite) as refusal:
            ports["discover"]()
        assert str(refusal.value) == PREREQUISITES[name]


def test_mutations_send_the_flat_binding_input_and_both_mandatory_headers(tmp_path):
    gateway = FakeGateway()
    ports = contract_ports(gateway, tmp_path / "captures")
    selections = ports["discover"]()
    created = ports["create_binding"](selections["repository"])
    binding = ports["verify_binding"](created)
    ports["unlink_binding"](binding["id"])
    mutations = [call for call in gateway.calls if call["method"] in ("POST", "DELETE")]
    assert len(mutations) == 3
    for call in mutations:
        assert re.fullmatch(r"[A-Za-z0-9_-]{16,128}", call["headers"]["Idempotency-Key"])
        assert call["headers"]["If-Profile-Revision"].isdecimal(), "Every mutation carries the profile revision"
    assert len({call["headers"]["Idempotency-Key"] for call in mutations}) == 3
    assert [call["headers"]["If-Profile-Revision"] for call in mutations] == ["7", "8", "8"], (
        "The current profile revision must be refreshed from the Product before each mutation"
    )
    posted = mutations[0]["body"]
    assert "selection" not in posted and posted["kind"] == "repository"
    assert posted["connection_ref"] == {"kind": "github_installation", "id": "inst-1"}
    assert binding["kind"] == "repository" and binding["source_revision"] == "repo-rev"
    assert [entry["profile_revision"] for entry in ports["revisions"]()] == ["7", "8", "8", "8", "8", "9"], (
        "Each response's Profile-Revision header is retained as evidence"
    )


class DriftingGateway(FakeGateway):
    """A Product whose profile revision moves on right after it is read."""

    def get(self, url, **options):
        response = super().get(url, **options)
        if urlsplit(url).path.endswith("/projects/" + FIXTURE["project_id"]):
            self.revision += 1
        return response


def test_mutations_refuse_a_stale_or_unheadered_precondition(tmp_path):
    drifting = DriftingGateway()
    ports = contract_ports(drifting, tmp_path / "drift")
    with pytest.raises(ValueError, match="HTTP 412"):
        ports["create_binding"](ports["discover"]()["repository"])
    assert drifting.bindings == {}, "A stale If-Profile-Revision must not create a binding"
    gateway = FakeGateway()
    stable = contract_ports(gateway, tmp_path / "captures")
    created = stable["create_binding"](stable["discover"]()["repository"])
    url = "https://product.example.test/api/v1/products/prod-1/projects/proj-1/bindings/" + created["id"]
    assert gateway.delete(url, headers={"Authorization": "Bearer live-access-token"}).status == 428
    assert gateway.bindings[created["id"]]["status"] == "pending", "An unheadered DELETE must not unlink"


def test_deployed_run_reads_items_and_retires_owned_bindings_as_tombstones(tmp_path, stub_browser):
    gateway = FakeGateway()
    captures, cli_calls = tmp_path / "captures", []
    ports = contract_ports(gateway, captures, cli_calls=cli_calls, page=FakePage(gateway))
    written = run_source_reuse(ports, run_id="example-run", tmp_path=tmp_path,
                               report_path=tmp_path / "reports" / "sources.json")
    report = json.loads(Path(written).read_text())
    assert report["owned_binding_ids"] == ["binding-1", "binding-2"]
    assert set(report["observations"]) == {"api", "installed_cli", "browser", "independent_provider"}
    provider_observation = report["observations"]["independent_provider"]
    assert provider_observation["before"]["repository"]["id"] == 35
    assert provider_observation["before"]["environment"]["account_id"] == "000000000000"
    assert provider_observation["calls"] == ["GET https://api.github.com/repositories/35",
                                             "sts:GetCallerIdentity"] * 2
    assert provider_observation["after"] == provider_observation["before"]
    assert report["cleanup"]["tombstoned_binding_ids"] == ["binding-1", "binding-2"]
    assert report["cleanup"]["remaining_owned_binding_ids"] == []
    assert all(state == {"receipt": "unlinked", "readback": "unlinked"}
               for state in report["cleanup"]["owned_binding_status"].values())
    assert [view["status"] for view in gateway.binding_items()] == ["unlinked", "unlinked"], (
        "The deployed readback keeps the tombstone the DELETE returned"
    )
    assert cli_calls == [
        ["sources", "--product", "prod-1", "--project", "proj-1",
         "--connection-kind", "github_installation", "--connection-id", "inst-1"],
        ["source-bindings", "--product", "prod-1", "--project", "proj-1"],
    ], "The served client must be executed with its published commands"
    assert stub_browser.open_product_section is not None
    assert sorted(path.name for path in captures.glob("*.json")) == [
        "example-run-v06-api.json", "example-run-v06-browser.json",
        "example-run-v06-independent-provider.json", "example-run-v06-installed-cli.json",
    ]
    assert json.loads((captures / "example-run-v06-independent-provider.json").read_text())["before"]
    assert json.loads((captures / "example-run-v06-browser.json").read_text())["items"]
    assert json.loads((captures / "example-run-v06-installed-cli.json").read_text())["sources"]["items"]


def test_cleanup_refuses_an_absent_or_still_linked_owned_binding(tmp_path, stub_browser):
    for name, gateway in (("absent", FakeGateway(keep_tombstone=False)),
                          ("linked", FakeGateway(tombstone="healthy"))):
        ports = contract_ports(gateway, tmp_path / name, cli_calls=[], page=FakePage(gateway))
        with pytest.raises(ValueError):
            run_source_reuse(ports, run_id="example-run", tmp_path=tmp_path / name,
                             report_path=tmp_path / name / "refused.json")
        assert not (tmp_path / name / "refused.json").exists()
        assert [call["method"] for call in gateway.calls].count("DELETE") == 2, (
            "The run still attempts cleanup of both bindings it created"
        )


def test_installed_cli_and_browser_surfaces_require_their_served_capability(tmp_path):
    provider = SimpleNamespace(base_url="https://product.example.test", request=FakeGateway())
    ports = build_source_ports(provider, dict(FIXTURE), tmp_path, environ=dict(CONTRACT_ENVIRON),
                               fetch=lambda url, token: REPOSITORY_PAYLOAD, identity=lambda: CALLER_IDENTITY)
    with pytest.raises(SourcePrerequisite) as refusal:
        ports["read_surface"]("installed_cli", {})
    assert str(refusal.value) == PREREQUISITES["installed_cli"]
    with pytest.raises(SourcePrerequisite) as refusal:
        ports["read_surface"]("browser", {})
    assert str(refusal.value) == PREREQUISITES["browser_sources"]
    published = build_source_ports(provider, dict(FIXTURE), tmp_path,
                                   environ={**CONTRACT_ENVIRON, "PRODUCT_RESUME_CLI_PATH": "/opt/adp/adp",
                                            "PRODUCT_RESUME_CLI_HASHES": json.dumps({"adp": "a" * 64})},
                                   fetch=lambda url, token: REPOSITORY_PAYLOAD, identity=lambda: CALLER_IDENTITY)
    with pytest.raises(SourcePrerequisite) as refusal:
        published["read_surface"]("installed_cli", {})
    assert str(refusal.value) == PREREQUISITES["installed_cli"], (
        "A client inventory missing adp-product.py is not a digest-verified installation"
    )


def test_independent_provider_readback_refuses_an_unauthorized_identity(tmp_path):
    gateway = FakeGateway()
    assert contract_ports(gateway, tmp_path / "captures")["read_provider"]() == {
        "repository": {"id": 35, "owner": {"id": 27}, "updated_at": "repo-rev", "name": "adp",
                       "default_branch": "main", "visibility": "private"},
        "environment": {"account_id": "000000000000", "region": "us-east-1", "role_ref": ROLE_REF,
                        "caller_arn": CALLER_IDENTITY["Arn"], "caller_user_id": CALLER_IDENTITY["UserId"]},
    }
    provider = SimpleNamespace(base_url="https://product.example.test", request=gateway)
    for identity, environ in ((lambda: {**CALLER_IDENTITY, "Account": "999999999999"}, CONTRACT_ENVIRON),
                              (lambda: {**CALLER_IDENTITY, "Arn": "arn:aws:iam::000000000000:user/other"},
                               CONTRACT_ENVIRON),
                              (lambda: CALLER_IDENTITY, {**CONTRACT_ENVIRON, "GITHUB_TOKEN": ""})):
        ports = build_source_ports(provider, dict(FIXTURE), tmp_path, environ=dict(environ),
                                   fetch=lambda url, token: REPOSITORY_PAYLOAD, identity=identity)
        with pytest.raises(SourcePrerequisite) as refusal:
            ports["read_provider"]()
        assert str(refusal.value) == PREREQUISITES["read_provider"]


# --- Orchestration, independent of the deployed transport ---

REPOSITORY_VIEW = {"kind": "repository", "namespace": "27", "native_id": "35", "source_revision": "repo-rev"}
ENVIRONMENT_VIEW = {
    "kind": "environment", "namespace": "000000000000", "native_id": "us-east-1",
    "source_revision": "secret-rev", "observed_at": "2026-10-20T00:00:00Z",
    "verified_environment": {"provider": "aws", "account_id": "000000000000", "regions": ["us-east-1"],
                             "role_ref": "role/example", "source_revision": "secret-rev",
                             "verified_at": "2026-10-20T00:00:00Z"},
}
PROVIDER_STATE = {
    "repository": {"id": 35, "owner": {"id": 27}, "updated_at": "repo-rev"},
    "environment": {"account_id": "000000000000", "region": "us-east-1", "role_ref": "role/example"},
}
USED_PORTS = ("discover", "create_binding", "verify_binding", "list_bindings", "unlink_binding",
              "read_surface", "read_provider", "provider_calls", "artifact")


class FakeDeployment:
    """A stand-in for the deployed ports, used only by these offline checks."""

    def __init__(self, *, environment=True, installed_cli=True, mutate_provider=False, leak_cleanup=False,
                 absent_cleanup=False, simulate_surface=False):
        self.environment = environment
        self.installed_cli = installed_cli
        self.mutate_provider = mutate_provider
        self.leak_cleanup = leak_cleanup
        self.absent_cleanup = absent_cleanup
        self.simulate_surface = simulate_surface
        self.bindings = {}
        self.unlinked = []
        self.calls = []
        self.reads = 0
        self.counter = 0

    def discover(self):
        if not self.environment:
            raise SourcePrerequisite(PREREQUISITES["environment_selection"])
        return {
            "repository": {"kind": "repository", "provider": "github",
                           "connection_ref": {"kind": "github_installation", "id": "inst-1"},
                           "namespace": "27", "native_id": "35"},
            "environment": {"kind": "environment", "provider": "aws",
                            "connection_ref": {"kind": "credential", "id": "cred-1"},
                            "namespace": "000000000000", "native_id": "us-east-1",
                            "environment": {"name": "dev", "environment_class": "development",
                                            "production": False}},
        }

    def create_binding(self, selection):
        self.counter += 1
        identifier = "binding-" + str(self.counter)
        self.bindings[identifier] = {"id": identifier, "selection": selection, "status": "pending"}
        return {"id": identifier}

    def verify_binding(self, binding):
        view = REPOSITORY_VIEW if binding["id"] == "binding-1" else ENVIRONMENT_VIEW
        self.bindings[binding["id"]] = {"id": binding["id"], "status": "healthy", **view}
        return dict(self.bindings[binding["id"]])

    def list_bindings(self):
        return [dict(view) for view in self.bindings.values()]

    def unlink_binding(self, binding_id):
        self.unlinked.append(binding_id)
        stone = {"id": binding_id, "status": "healthy" if self.leak_cleanup else "unlinked"}
        if self.absent_cleanup:
            self.bindings.pop(binding_id, None)
        else:
            self.bindings[binding_id] = dict(stone)
        return stone

    def read_surface(self, surface, bindings):
        if surface == "installed_cli" and not self.installed_cli:
            raise SourcePrerequisite(PREREQUISITES["installed_cli"])
        if self.simulate_surface and surface == "browser":
            return {"artifact_ref": surface + ".json"}
        return {"repository": dict(REPOSITORY_VIEW), "environment": dict(ENVIRONMENT_VIEW),
                "artifact_ref": surface + ".json"}

    def read_provider(self):
        self.reads += 1
        self.calls.extend(["GET https://api.github.com/repositories/35", "sts:GetCallerIdentity"])
        state = json.loads(json.dumps(PROVIDER_STATE))
        if self.mutate_provider and self.reads > 1:
            state["repository"]["updated_at"] = "changed"
        return state

    def artifact(self, name, payload):
        if not payload:
            raise ValueError("A durable artifact must record the data the surface returned")
        return name + "-artifact.json"

    def ports(self):
        return {
            "discover": self.discover,
            "create_binding": self.create_binding,
            "verify_binding": self.verify_binding,
            "list_bindings": self.list_bindings,
            "unlink_binding": self.unlink_binding,
            "read_surface": self.read_surface,
            "read_provider": self.read_provider,
            "provider_calls": lambda: list(self.calls),
            "artifact": self.artifact,
        }


def test_source_reuse_producer_observes_every_surface_and_retires_owned_bindings(tmp_path):
    deployment = FakeDeployment()
    written = run_source_reuse(deployment.ports(), run_id="example-run", tmp_path=tmp_path,
                               report_path=tmp_path / "reports" / "sources.json")
    report = json.loads(Path(written).read_text())
    assert report["owned_binding_ids"] == ["binding-1", "binding-2"]
    assert set(report["observations"]) == {"api", "installed_cli", "browser", "independent_provider"}
    assert report["observations"]["independent_provider"]["before"] == PROVIDER_STATE
    assert "sts:GetCallerIdentity" in report["observations"]["independent_provider"]["calls"]
    assert report["cleanup"]["remaining_owned_binding_ids"] == []
    assert report["cleanup"]["independent_provider_unchanged"] is True
    assert deployment.unlinked == ["binding-1", "binding-2"]
    assert [view["status"] for view in deployment.list_bindings()] == ["unlinked", "unlinked"]
    assert Path(written).stat().st_mode & 0o077 == 0


def test_source_reuse_producer_refuses_leaked_mutated_or_simulated_observations(tmp_path):
    for deployment in (FakeDeployment(leak_cleanup=True), FakeDeployment(absent_cleanup=True),
                       FakeDeployment(mutate_provider=True), FakeDeployment(simulate_surface=True)):
        with pytest.raises(ValueError):
            run_source_reuse(deployment.ports(), run_id="example-run", tmp_path=tmp_path,
                             report_path=tmp_path / "refused.json")
        assert deployment.unlinked, "A refused observation still retires what the run created"
    assert not (tmp_path / "refused.json").exists()


def test_source_reuse_producer_names_the_missing_endpoint_or_fixture(tmp_path):
    missing_environment = FakeDeployment(environment=False)
    with pytest.raises(SourcePrerequisite, match="authorized AWS environment resource"):
        run_source_reuse(missing_environment.ports(), run_id="example-run", tmp_path=tmp_path,
                         report_path=tmp_path / "missing.json")
    assert missing_environment.unlinked == []
    missing_cli = FakeDeployment(installed_cli=False)
    with pytest.raises(SourcePrerequisite, match="digest-verified installed Product client"):
        run_source_reuse(missing_cli.ports(), run_id="example-run", tmp_path=tmp_path,
                         report_path=tmp_path / "missing.json")
    assert missing_cli.unlinked == ["binding-1", "binding-2"], "Cleanup owns the run even when it is refused"
    for name in USED_PORTS:
        ports = {key: value for key, value in FakeDeployment().ports().items() if key != name}
        with pytest.raises(SourcePrerequisite) as refusal:
            run_source_reuse(ports, run_id="example-run", tmp_path=tmp_path, report_path=tmp_path / "missing.json")
        assert str(refusal.value) == PREREQUISITES[name], "Every refusal must name the missing capability"
    assert not (tmp_path / "missing.json").exists()


def test_surface_views_refuse_a_surface_that_read_nothing_back():
    views = {"repository": dict(REPOSITORY_VIEW), "environment": dict(ENVIRONMENT_VIEW)}
    assert surface_views(views, "api") is views
    with pytest.raises(ValueError, match="did not read back the environment source"):
        surface_views({"repository": dict(REPOSITORY_VIEW)}, "installed_cli")
    with pytest.raises(ValueError, match="no source readback"):
        surface_views(None, "browser")


def test_protected_report_is_private_and_refuses_links(tmp_path):
    target = tmp_path / "real.json"
    written = protected_report(target, {"run_id": "example-run"})
    assert json.loads(Path(written).read_text())["run_id"] == "example-run"
    assert target.stat().st_mode & 0o077 == 0
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        protected_report(link, {"run_id": "example-run"})


def test_binding_views_are_flattened_for_the_reviewed_surface_check():
    flattened = flatten_binding({"id": "binding-1", "status": "healthy", "source_revision": "repo-rev",
                                 "selection": {"kind": "repository", "namespace": "27", "native_id": "35"}})
    assert flattened["kind"] == "repository" and flattened["native_id"] == "35"
    assert flattened["source_revision"] == "repo-rev" and "selection" not in flattened
    with pytest.raises(ValueError, match="not an object"):
        flatten_binding(["binding-1"])


# --- The deployed SCN-02 status contract ---

STATUS_SOURCE = {"connection_ref": {"kind": "credential", "id": "cred-1"}, "provider": "aws", "label": "dev",
                 "capabilities": ["environment"]}


class StatusGateway(FakeGateway):
    """Answer the deployed status surfaces, including the stored ``+00:00`` observation clock.

    The deployed route stores ``datetime.now(UTC).isoformat()`` in the binding observation, so the readback
    publishes an offset clock while the reviewed SCN-02 check only accepts the equivalent ``Z`` form: the
    producer has to pin the representation without moving the instant. Coverage, health, binding status and
    the project progress checks follow ``binding_view`` and ``source_overlay``: healthy inside the pinned
    window, degraded once ``stale_after`` passes, restricted while the connection authorization is withdrawn.
    """

    def __init__(self, *, window=6, unauthorize=False, naive_clock=False, **options):
        super().__init__(**options)
        self.window = window
        self.unauthorize = unauthorize
        self.naive_clock = naive_clock
        self.observed = None
        self.stale = None
        self.restricted = None
        self.sessions = 0
        self.phases = []

    def restrict(self, phase):
        self.restricted = phase
        self.phases.append(phase)

    def release(self):
        self.restricted = None

    def expired(self):
        return self.stale is not None and datetime.now(UTC) >= self.stale

    def progress(self, identifier):
        check = "missing" if self.restricted else ("stale" if self.expired() else "complete")
        return {"id": identifier, "progress": {"checks": [{"id": "basic", "status": "complete"},
                                                          {"id": "environment", "status": check}]}}

    def status_source(self):
        source = json.loads(json.dumps(STATUS_SOURCE))
        if self.restricted:
            return {**source, "coverage": "unavailable", "health": "unverified"}
        return {**source, "coverage": "partial", "health": "healthy",
                "verified_at": ENVIRONMENT_RESOURCE["observed_at"],
                "resources": [json.loads(json.dumps(ENVIRONMENT_RESOURCE))]}

    def binding_items(self):
        items = []
        for view in super().binding_items():
            identity = {key: view[key] for key in ("id", "project_id", "generation", "revision")}
            if view.get("status") == "unlinked":
                items.append({**identity, "status": "unlinked"})
            elif self.restricted:
                items.append({**identity, "status": "restricted"})
            else:
                if view.get("status") == "healthy" and self.expired():
                    view["status"] = "degraded"
                items.append(view)
        return items

    def get(self, url, **options):
        parts = urlsplit(url)
        query = {key: value[0] for key, value in parse_qs(parts.query).items()}
        headers = options.get("headers") or {}
        if parts.path.endswith("/sources") and query.get("limit") == "1":
            self._record("GET", url, headers)
            if self.unauthorize and self.restricted:
                return FakeResponse(404, {"code": "not_found"}, self.revision)
            return FakeResponse(200, {"items": [self.status_source()], "next_cursor": None,
                                      "coverage": "partial"}, self.revision)
        if "/revisions/" in parts.path or "/onboarding-sessions/" in parts.path:
            self._record("GET", url, headers)
            return FakeResponse(200, self.progress(parts.path.rsplit("/", 1)[1]), self.revision)
        response = super().get(url, **options)
        if response.status == 200 and parts.path.endswith("/projects/" + FIXTURE["project_id"]):
            return FakeResponse(200, {**response.json(), **self.progress(FIXTURE["project_id"])}, self.revision)
        return response

    def post(self, url, **options):
        parts = urlsplit(url)
        if parts.path.endswith("/onboarding-sessions"):
            headers = options.get("headers") or {}
            self._record("POST", url, headers, options.get("data"))
            refusal = self._preconditions(headers)
            if refusal is not None:
                return FakeResponse(refusal, {"code": "precondition"}, self.revision)
            self.sessions += 1
            return FakeResponse(201, {"id": "session-" + str(self.sessions)}, self.revision)
        response = super().post(url, **options)
        if parts.path.endswith("/verify") and response.status == 202:
            self.observed = datetime.now(UTC) - timedelta(seconds=1)
            self.stale = self.observed + timedelta(seconds=self.window)
            offset = "" if self.naive_clock else "+00:00"
            self.bindings[parts.path.split("/")[-2]].update(
                observed_at=self.observed.isoformat().replace("+00:00", offset),
                stale_after=self.stale.isoformat().replace("+00:00", offset),
            )
        return response


def status_fixture(gateway, **overrides):
    fixture = dict(FIXTURE)
    fixture["withdraw_source_authorization"] = gateway.restrict
    fixture["restore_source_authorization"] = gateway.release
    fixture.update(overrides)
    return fixture


def status_ports(gateway, directory, *, fixture=None, cli_calls=None, environ=None, page=None):
    """Wire the real status adapter to the contract double and its served-CLI stand-in."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    provider = SimpleNamespace(base_url="https://product.example.test", request=gateway)

    def runner(arguments):
        if cli_calls is not None:
            cli_calls.append(list(arguments))
        if arguments[0] == "sources":
            return {"items": [gateway.status_source()], "next_cursor": None}
        if arguments[0] == "source-bindings":
            return {"items": gateway.binding_items(), "next_cursor": None}
        raise AssertionError("The installed client was asked for an unpublished command")

    environment = dict(CONTRACT_ENVIRON if environ is None else environ)
    environment.setdefault("PRODUCT_V06_STATUS_WAIT_SECONDS", "60")
    return build_status_ports(provider, status_fixture(gateway) if fixture is None else fixture, directory,
                              page=FakePage(gateway) if page is None else page, environ=environment, cli=runner,
                              fetch=lambda url, token: json.loads(json.dumps(REPOSITORY_PAYLOAD)),
                              identity=lambda: json.loads(json.dumps(CALLER_IDENTITY)))


def test_status_producer_observes_every_deployed_phase_and_pins_the_stored_clock(tmp_path, stub_browser):
    gateway = StatusGateway()
    calls = []
    written = run_source_status(status_ports(gateway, tmp_path / "captures", cli_calls=calls),
                                run_id="example-run", tmp_path=tmp_path,
                                report_path=tmp_path / "status-report.json")
    report = json.loads(Path(written).read_text())
    assert set(report["attempts"]) == set(STATUS_PHASES)
    assert report["observed_at"] == gateway.observed.isoformat().replace("+00:00", "Z")
    assert report["stale_after"] == gateway.stale.isoformat().replace("+00:00", "Z")
    assert report["attempts"]["at_expiry"]["clock"] == report["stale_after"], (
        "The expiry attempt is pinned to the boundary the deployed binding published"
    )
    assert report["attempts"]["before_expiry"]["surfaces"]["binding"]["status"] == "healthy"
    assert report["attempts"]["partial"]["surfaces"]["source"]["coverage"] == "partial"
    assert report["attempts"]["at_expiry"]["surfaces"]["binding"]["status"] == "degraded", (
        "The producer has to wait for the deployed clock instead of asserting staleness"
    )
    for phase in ("unavailable", "revoked"):
        surfaces = report["attempts"][phase]["surfaces"]
        assert surfaces["binding"]["status"] == "restricted"
        assert surfaces["source"]["coverage"] == "unavailable" and "resources" not in surfaces["source"]
        assert ROLE_REF not in json.dumps(surfaces) and "000000000000" not in json.dumps(surfaces)
    assert gateway.phases == ["unavailable", "revoked"] and gateway.restricted is None
    assert report["provider_identity"] == {"account_id": "000000000000", "role_ref": ROLE_REF}
    assert report["cleanup"]["tombstoned_binding_ids"] == report["owned_binding_ids"]
    assert report["cleanup"]["remaining_owned_binding_ids"] == []
    assert [call[0] for call in calls] == ["sources", "source-bindings"] * len(STATUS_PHASES), (
        "The served client is executed for every status phase"
    )
    assert gateway.sessions == 1, "One run-owned onboarding session is created and reused by every phase"
    assert Path(written).stat().st_mode & 0o077 == 0
    for attempt in report["attempts"].values():
        assert set(attempt["artifacts"]) == set(STATUS_ARTIFACTS)
        assert len(set(attempt["artifacts"].values())) == len(STATUS_ARTIFACTS)
        assert all(Path(ref).stat().st_mode & 0o077 == 0 for ref in attempt["artifacts"].values())


def test_status_producer_refuses_a_capability_or_clock_it_was_not_given(tmp_path, stub_browser):
    bare = StatusGateway()
    with pytest.raises(SourcePrerequisite) as refusal:
        run_source_status(status_ports(bare, tmp_path / "bare", fixture=dict(FIXTURE)), run_id="example-run",
                          tmp_path=tmp_path, report_path=tmp_path / "bare.json")
    assert str(refusal.value) == PREREQUISITES["status_restriction"]
    assert [view["status"] for view in bare.binding_items()] == ["unlinked"], "Cleanup owns a refused run"
    cases = {
        "await_expiry": (StatusGateway(), {**CONTRACT_ENVIRON, "PRODUCT_V06_STATUS_WAIT_SECONDS": "1"}),
        "status_restriction": (StatusGateway(unauthorize=True), None),
        "status_clock": (StatusGateway(naive_clock=True), None),
    }
    for name, (gateway, environ) in cases.items():
        with pytest.raises(SourcePrerequisite) as refusal:
            run_source_status(status_ports(gateway, tmp_path / name, environ=environ), run_id="example-run",
                              tmp_path=tmp_path, report_path=tmp_path / (name + ".json"))
        assert str(refusal.value) == PREREQUISITES[name], "Every refusal names the missing capability"
        assert [view["status"] for view in gateway.binding_items()] == ["unlinked"]
        assert not (tmp_path / (name + ".json")).exists()


def test_pinned_clock_keeps_the_deployed_instant_and_refuses_an_unanchored_stamp():
    assert pinned_clock("2026-10-20T00:00:00.500000+00:00", "observed_at") == "2026-10-20T00:00:00.500000Z"
    assert pinned_clock("2026-10-20T00:00:00Z", "stale_after") == "2026-10-20T00:00:00Z"
    stored = {"observed_at": "2026-10-20T00:00:00+00:00", "status": "healthy",
              "resources": [{"verified_at": "2026-10-20T00:00:00+00:00", "native_id": "us-east-1"}]}
    assert pinned_timestamps(stored) == {
        "observed_at": "2026-10-20T00:00:00Z", "status": "healthy",
        "resources": [{"verified_at": "2026-10-20T00:00:00Z", "native_id": "us-east-1"}],
    }
    for value in ("2026-10-20T00:00:00", "2026-10-20T00:00:00+02:00", "", None, 1761523200):
        with pytest.raises(SourcePrerequisite, match=re.escape("pinned UTC observation clock")):
            pinned_clock(value, "observed_at")


def test_status_attempt_refuses_a_phase_that_shares_or_skips_a_surface_artifact():
    base = {"source": {"coverage": "partial"}, "binding": {"id": "binding-1", "status": "healthy"},
            "profile": {"progress": {}}, "revision": {"progress": {}}, "session": {"progress": {}},
            "native_id": "us-east-1", "artifacts": {name: name + ".json" for name in STATUS_ARTIFACTS}}

    def attempt(view, phase="before_expiry"):
        ports = {"status_surfaces": lambda name, binding_id: json.loads(json.dumps(view))}
        return status_attempt(ports, phase, "binding-1", run_id="example-run", source_revision="a" * 40)

    observed = attempt(base)
    assert observed["status"] == "passed" and observed["clock"].endswith("Z")
    assert observed["surfaces"]["native_id"] == "us-east-1"
    assert observed["artifacts"] == base["artifacts"]
    for change in ({"artifacts": dict.fromkeys(STATUS_ARTIFACTS, "shared.json")},
                   {"artifacts": {"api": "api.json"}}, {"artifacts": {**base["artifacts"], "browser": ""}},
                   {"binding": {"id": "someone-else"}}, {"native_id": ""}, {"source": None}):
        with pytest.raises(ValueError):
            attempt({**base, **change})
    with pytest.raises(SourcePrerequisite) as refusal:
        status_attempt({}, "before_expiry", "binding-1", run_id="example-run", source_revision="a" * 40)
    assert str(refusal.value) == PREREQUISITES["status_surfaces"]


# --- SCN-03: the deployed paged resource_cursor, verify replay and owned-only unlink ---

REPOSITORY_PAGES = {None: (REPOSITORY_RESOURCE, "2"), "2": (OTHER_REPOSITORY, None)}
OTHER_PAYLOAD = {"id": 99, "owner": {"id": 27}, "updated_at": "other-rev", "name": "other",
                 "default_branch": "main", "visibility": "private"}
PROFILE_VALUES = {"product": {"name": "Example product", "purpose": "Serve the regression story"},
                  "project": {"name": "Example project", "environment_class": "development"}}


class RecoveryGateway(FakeGateway):
    """Answer one bounded resource page at a time and keep one receipt per verification."""

    def __init__(self, *, pages=None, receipts=True, values=None, **options):
        super().__init__(**options)
        self.pages = dict(REPOSITORY_PAGES if pages is None else pages)
        self.receipts = receipts
        self.values = PROFILE_VALUES if values is None else values

    def paged_sources(self, query):
        if query.get("connection_kind") != "github_installation" or not query.get("connection_id"):
            return FakeResponse(422, {"code": "validation_error"}, self.revision)
        cursor = query.get("resource_cursor")
        if cursor not in self.pages:
            return FakeResponse(400, {"code": "invalid_cursor"}, self.revision)
        resource, next_cursor = self.pages[cursor]
        item = {key: value for key, value in CONNECTIONS[0].items() if key != "resources"}
        item["resources"] = [json.loads(json.dumps(resource))]
        item["coverage"] = "partial"
        item["resource_cursor"] = next_cursor
        return FakeResponse(200, {"items": [item], "next_cursor": None, "coverage": "partial"}, self.revision)

    def get(self, url, **options):
        parts = urlsplit(url)
        query = {key: value[0] for key, value in parse_qs(parts.query).items()}
        headers = options.get("headers") or {}
        if parts.path.endswith("/sources") and query.get("limit") == "1":
            self._record("GET", url, headers)
            if not self._authorized(headers):
                return FakeResponse(401, {"code": "unauthorized"}, self.revision)
            return self.paged_sources(query)
        response = super().get(url, **options)
        if parts.path.endswith("/projects/" + FIXTURE["project_id"]) and response.status == 200:
            return FakeResponse(200, {**response.json(), **self.values}, self.revision)
        return response

    def post(self, url, **options):
        response = super().post(url, **options)
        if urlsplit(url).path.endswith("/verify") and response.status == 202 and not self.receipts:
            return FakeResponse(202, {"state": "succeeded"}, self.revision)
        return response


def recovery_ports(gateway, directory, *, cli_calls=None, environ=None, page=None):
    """Wire the real recovery adapter to the contract double, with a served-CLI stand-in."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    provider = SimpleNamespace(base_url="https://product.example.test", request=gateway)
    payloads = {"35": REPOSITORY_PAYLOAD, "99": OTHER_PAYLOAD}

    def runner(arguments):
        if cli_calls is not None:
            cli_calls.append(list(arguments))
        if arguments[0] == "sources":
            assert "--limit" in arguments and arguments[arguments.index("--limit") + 1] == "1"
            cursor = (arguments[arguments.index("--resource-cursor") + 1]
                      if "--resource-cursor" in arguments else None)
            resource, next_cursor = gateway.pages[cursor]
            item = {key: value for key, value in CONNECTIONS[0].items() if key != "resources"}
            return {"items": [{**item, "resources": [resource], "resource_cursor": next_cursor}],
                    "next_cursor": None, "next_prerequisites": []}
        if arguments[0] == "source-bindings":
            return {"items": gateway.binding_items(), "next_cursor": None}
        raise AssertionError("The installed client was asked for an unpublished command")

    def readback(url, token):
        native_id = url.rsplit("/", 1)[-1]
        if native_id not in payloads:
            raise AssertionError("The independent provider was asked for an unselected repository")
        return json.loads(json.dumps(payloads[native_id]))

    return build_recovery_ports(provider, dict(FIXTURE), directory, page=page,
                                environ=dict(CONTRACT_ENVIRON if environ is None else environ), cli=runner,
                                fetch=readback)


def test_recovery_producer_resumes_the_interrupted_page_and_unlinks_only_what_it_owns(tmp_path, stub_browser):
    gateway = RecoveryGateway()
    calls = []
    ports = recovery_ports(gateway, tmp_path / "captures", cli_calls=calls, page=FakePage(gateway))
    written = run_source_recovery(ports, run_id="example-run", tmp_path=tmp_path,
                                  report_path=tmp_path / "recovery-report.json")
    report = json.loads(Path(written).read_text())
    assert set(report["attempts"]) == set(RECOVERY_PHASES)
    first, resumed = report["attempts"]["interrupted"], report["attempts"]["resumed"]
    assert first["requested_cursor"] is None and first["resource_cursor"] == "2"
    assert resumed["requested_cursor"] == "2" and resumed["resource_cursor"] is None, (
        "The resumed page continues on the cursor the deployed read published and then exhausts it"
    )
    assert [first["resource_ids"], resumed["resource_ids"]] == [["35"], ["99"]]
    assert [first["page_count"], resumed["page_count"]] == [1, 2]
    paged = [query for query in source_queries(gateway) if query.get("limit") == "1"]
    assert [query.get("resource_cursor") for query in paged] == [None, "2"]
    assert all(query["connection_kind"] == "github_installation" and query["connection_id"] == "inst-1"
               for query in paged), "Paged discovery stays on the authorized connection pair"
    reverified = report["attempts"]["reverified"]
    assert reverified["binding_ids"] == sorted(report["owned_binding_ids"] + [report["preserved_binding_id"]])
    assert reverified["selected_native_id"] == "35" == reverified["binding_native_id"]
    assert len(set(reverified["verify_receipt_ids"])) == 1 and reverified["verify_attempts"] == 2
    verifies = [call for call in gateway.calls if call["method"] == "POST" and call["url"].endswith("/verify")]
    assert len(verifies) == 2 and verifies[0]["headers"] == verifies[1]["headers"], (
        "The replayed verification sends one identical idempotency key and profile revision"
    )
    unlinked = report["attempts"]["unlinked"]
    assert unlinked["unlinked_binding_ids"] == report["owned_binding_ids"]
    assert unlinked["remaining_binding_ids"] == [report["preserved_binding_id"]]
    assert unlinked["confirmed_values_after"] == unlinked["confirmed_values_before"]
    assert unlinked["confirmed_values_before"]["product_purpose"] == PROFILE_VALUES["product"]["purpose"]
    assert report["cleanup"]["tombstoned_binding_ids"] == report["owned_binding_ids"]
    assert report["cleanup"]["remaining_owned_binding_ids"] == [] and report["cleanup"]["cleanup_attempts"] == 1
    assert report["preserved_cleanup"]["readback"] == "unlinked", "The run also retires the peer it created"
    deleted = [call["url"].rsplit("/", 1)[-1] for call in gateway.calls if call["method"] == "DELETE"]
    assert sorted(deleted) == sorted(report["owned_binding_ids"] + [report["preserved_binding_id"]]), (
        "An already unlinked owned binding is read back as a tombstone, not deleted twice"
    )
    provider = report["independent_provider"]
    assert set(provider["before"]) == {"35", "99"} and provider["after"] == provider["before"]
    assert all(call.startswith("GET ") for call in provider["calls"]), "Recovery never mutates a provider"
    assert [call[0] for call in calls] == ["sources", "source-bindings"] * len(RECOVERY_PHASES)
    assert [("--resource-cursor" in call) for call in calls if call[0] == "sources"] == [False, True, False, False]
    assert Path(written).stat().st_mode & 0o077 == 0
    for attempt in report["attempts"].values():
        assert attempt["status"] == "passed" and set(attempt["artifacts"]) == {"api", "installed_cli", "browser",
                                                                              "independent_provider"}
        assert len(set(attempt["artifacts"].values())) == 4
        assert all(Path(ref).stat().st_mode & 0o077 == 0 for ref in attempt["artifacts"].values())


def test_recovery_producer_refuses_a_page_receipt_value_or_provider_it_was_not_given(tmp_path, stub_browser):
    cases = {
        "recovery_pages": (RecoveryGateway(pages={None: (REPOSITORY_RESOURCE, None)}), None),
        "verify_receipt": (RecoveryGateway(receipts=False), None),
        "recovery_values": (RecoveryGateway(values={}), None),
        "repository_provider": (RecoveryGateway(),
                                {key: value for key, value in CONTRACT_ENVIRON.items() if key != "GITHUB_TOKEN"}),
    }
    for name, (gateway, environ) in cases.items():
        ports = recovery_ports(gateway, tmp_path / name, environ=environ, page=FakePage(gateway))
        with pytest.raises(SourcePrerequisite) as refusal:
            run_source_recovery(ports, run_id="example-run", tmp_path=tmp_path,
                                report_path=tmp_path / (name + ".json"))
        assert str(refusal.value) == PREREQUISITES[name], "Every refusal names the missing capability"
        assert not (tmp_path / (name + ".json")).exists()
        assert all(view["status"] == "unlinked" for view in gateway.binding_items()), (
            "A refused recovery observation still retires every binding the run created"
        )


def test_recovery_refuses_a_project_that_already_holds_a_binding_it_does_not_own(tmp_path, stub_browser):
    gateway = RecoveryGateway()
    gateway.bindings["binding-foreign"] = {"id": "binding-foreign", "project_id": FIXTURE["project_id"],
                                           "generation": 1, "revision": gateway.revision, "status": "healthy"}
    ports = recovery_ports(gateway, tmp_path / "isolation", page=FakePage(gateway))
    with pytest.raises(SourcePrerequisite) as refusal:
        run_source_recovery(ports, run_id="example-run", tmp_path=tmp_path,
                            report_path=tmp_path / "isolation.json")
    assert str(refusal.value) == PREREQUISITES["recovery_isolation"]
    assert gateway.bindings["binding-foreign"]["status"] == "healthy", "A binding this run does not own is untouched"
    assert [call["method"] for call in gateway.calls].count("DELETE") == 0
    tombstoned = RecoveryGateway()
    tombstoned.bindings["binding-old"] = {"id": "binding-old", "project_id": FIXTURE["project_id"],
                                          "generation": 1, "revision": tombstoned.revision, "status": "unlinked"}
    assert recovery_ports(tombstoned, tmp_path / "tombstone")["require_isolation"]() is True, (
        "An earlier run's tombstone is not a live binding"
    )


def test_recovery_page_and_attempt_refuse_an_unusable_page_or_a_shared_artifact():
    payload = {"items": [{"resources": [REPOSITORY_RESOURCE], "resource_cursor": "2"}]}
    assert recovery_page(payload, None, 1) == {
        "requested_cursor": None, "resource_cursor": "2", "resource_ids": ["35"], "page_count": 1,
        "source": payload["items"][0],
    }
    assert recovery_page({"items": [{"resources": [OTHER_REPOSITORY]}]}, "2", 2)["resource_cursor"] is None
    for unusable in ({"items": []}, {"items": [{"resources": []}]}, {},
                     {"items": [{"resources": [REPOSITORY_RESOURCE, OTHER_REPOSITORY]}]}):
        with pytest.raises(SourcePrerequisite) as refusal:
            recovery_page(unusable, None, 1)
        assert str(refusal.value) == PREREQUISITES["recovery_pages"]
    for broken in ({"resources": [{**REPOSITORY_RESOURCE, "native_id": ""}]},
                   {"resources": [REPOSITORY_RESOURCE], "resource_cursor": ""}):
        with pytest.raises(ValueError):
            recovery_page({"items": [broken]}, None, 1)

    surfaces = {name: name + ".json" for name in ("api", "installed_cli", "browser", "independent_provider")}

    def attempt(artifacts, detail=None):
        ports = {"recovery_surfaces": lambda phase, observed, evidence=None: {"artifacts": artifacts}}
        return recovery_attempt(ports, "interrupted", run_id="example-run", source_revision="a" * 40,
                                detail={"page_count": 1} if detail is None else detail)

    observed = attempt(surfaces)
    assert observed["status"] == "passed" and observed["page_count"] == 1
    assert observed["artifacts"] == surfaces and observed["observed_at"].endswith("Z")
    for change in (dict.fromkeys(surfaces, "shared.json"), {"api": "api.json"}, {**surfaces, "browser": ""}):
        with pytest.raises(ValueError):
            attempt(change)
    with pytest.raises(ValueError):
        attempt(surfaces, detail={})
    with pytest.raises(SourcePrerequisite) as refusal:
        recovery_attempt({}, "interrupted", run_id="example-run", source_revision="a" * 40,
                         detail={"page_count": 1})
    assert str(refusal.value) == PREREQUISITES["recovery_surfaces"]
