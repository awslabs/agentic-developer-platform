"""Run-owned installed CLI and independent Insights readback scenarios."""

import base64
import copy
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[3]
CLI = REPO / "modules/gateway/cli"
sys.path.insert(0, str(REPO / "modules"))
results = importlib.import_module("insights.results")

spec = importlib.util.spec_from_file_location(
    "i23_registry_fixture", Path(__file__).with_name("test_i22_registry.py")
)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


def fixed_snapshot():
    work = registry.native("work", "example-work")
    cost = registry.native("cost", "example-cost", amount="10.00", currency="USD")
    product = registry.reference("product", "example-product", source="adp.profile")
    portfolio = registry.reference("portfolio", "example-commerce")
    initiative = registry.reference("initiative", "example-initiative")
    contribution = registry.reference("contribution", "example-contribution")
    links = [
        registry.link(
            "work_product", "example-work-link", work["subject_ref"], product
        ),
        registry.link(
            "work_product", "example-cost-link", cost["subject_ref"], product
        ),
        registry.link("portfolio_product", "example-membership", portfolio, product),
    ]
    fixture = registry.ScenarioFixture(links)
    admitted_links = fixture.admitted_links()
    source = {
        "subject_ref": contribution,
        "source_ref": registry.reference(
            "catalog_change", "example-contribution-event"
        ),
        "source_revisions": [contribution, product, initiative, work["subject_ref"]],
        "source_event_id": "example-contribution-event",
        "source_event_time": registry.EVENT_TIME,
        "effective_at": "2026-10-18T00:00:00Z",
        "recorded_at": registry.EVENT_TIME,
        "mapping_version": "approved-1",
        "record_type": "context_record",
        "source_snapshot": {
            "product_ref": product,
            "initiative_ref": initiative,
            "valid_from": "2026-10-18T00:00:00Z",
            "work_links": [
                {"work_ref": work["subject_ref"], "valid_from": "2026-10-18T00:00:00Z"}
            ],
        },
    }
    fixture.source.snapshots[
        (contribution["record_id"], contribution["record_revision"])
    ] = source
    fixture.registry.ingest_catalog(contribution, "example-publisher")
    admitted_contributions = [
        copy.deepcopy(row)
        for row in fixture.sink.rows.values()
        if row["record_type"] == "context_record"
    ]
    assert len(fixture.sink.rows) == len(links) + 1
    assert len(admitted_contributions) == 1
    assert fixture.sink.quarantined == []
    return (
        {
            "status": "complete",
            "dataset_version": "example-dataset-1",
            "mapping_versions": ["approved-1"],
            "facts": [work, cost],
            "relationships": admitted_links,
            "contributions": admitted_contributions,
            "allocations": [],
        },
        work,
        initiative,
        contribution,
    )


class LocalGateway(BaseHTTPRequestHandler):
    app = None
    requests = []
    reply = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path.startswith("/api/cli/"):
            name = path.path.removeprefix("/api/cli/")
            candidate = CLI / name
            if not candidate.is_file() or candidate.name != name:
                self.send_error(404)
                return
            data = candidate.read_bytes()
            status = 200
            headers = [
                (
                    "Content-Type",
                    "application/json" if name.endswith(".json") else "text/plain",
                )
            ]
        elif path.path.startswith("/api/engineering-insights/v1/projects/"):
            self.__class__.requests.append((path, self.headers.get("Authorization")))
            if self.__class__.reply is not None:
                status, payload = self.__class__.reply
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            status_line = []
            headers = []

            def start_response(value, extra):
                status_line.append(value)
                headers.extend(extra)

            data = b"".join(
                self.__class__.app(
                    {
                        "PATH_INFO": path.path.removeprefix("/api"),
                        "QUERY_STRING": path.query,
                        "REQUEST_METHOD": "GET",
                        "HTTP_AUTHORIZATION": self.headers.get("Authorization"),
                    },
                    start_response,
                )
            )
            status = int(status_line[0].split(" ", 1)[0])
        else:
            self.send_error(404)
            return
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def session(home, gateway_url):
    directory = home / ".bedrock-gateway"
    directory.mkdir(exist_ok=True)
    (directory / "config.json").write_text(json.dumps({"gateway_url": gateway_url}))

    def encoded(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    token = ".".join(
        (
            encoded({"alg": "none"}),
            encoded(
                {
                    "username": "example-viewer",
                    "sub": "example-viewer",
                    "org_id": registry.TENANT,
                }
            ),
            "sig",
        )
    )
    (directory / "tokens.json").write_text(
        json.dumps(
            {
                "id_token": "example-id",
                "access_token": token,
                "refresh_token": "example-refresh",
                "expires_at": int(time.time()) + 3600,
            }
        )
    )
    return token


LATER = "2026-10-21T12:00:00Z"


def qualified_shared_policy(snapshot):
    references = {
        key: dict(zip(results.projection.REFERENCE_FIELDS, key))
        for key in results.snapshot_references(snapshot)
    }
    identities = list(references.values())
    return {
        "schema_version": "results-policy/1.0", "tenant_id": registry.TENANT,
        "project_id": registry.PROJECT, "dataset_version": snapshot["dataset_version"],
        "snapshot_sha256": results.snapshot_digest(snapshot), "policy_epoch": 1,
        "envelopes": [{
            "envelope_version": 1, "identity": identities[0], "realm_id": "example-shared-realm",
            "realm_revision": 1, "descriptor_digest": "a" * 64, "audience": "shared",
            "required_policies": [
                {"ref": name, "revision": 1, "visibility": "shared",
                 "audience_expression": "current:project-readers"}
                for name in ("source-policy", "evidence-policy")
            ],
            "dependency_identities": identities,
            "dependency_versions": [{"identity": identity, "revision": 1, "metadata_digest": "b" * 64}
                                    for identity in identities],
        }],
    }


@contextmanager
def installed_gateway(snapshot, state=None):
    if state is None:
        state = {"snapshot": snapshot, "granted": True}

    def granted(tenant, project, viewer):
        return state["granted"] and (tenant, project, viewer) == (
            registry.TENANT,
            registry.PROJECT,
            "example-viewer",
        )

    initial_policy = qualified_shared_policy(snapshot)

    def published_policy(tenant, project):
        if "policy" in state:
            return state["policy"]
        current = state["snapshot"]
        if not isinstance(current, dict) or current.get("status") != "complete" or not current.get("facts"):
            return initial_policy
        return qualified_shared_policy(current)

    def read_snapshot(tenant, project):
        if state.get("on_snapshot_load"):
            state["on_snapshot_load"]()
        return state["snapshot"]

    def can_read_policy(tenant, project, viewer, policy):
        if not (granted(tenant, project, viewer)
                and state.get("source_granted", True) and state.get("evidence_granted", True)):
            return False
        try:
            authority = state.get("policy_authority")
            return authority(policy) if authority else True
        except OSError:
            raise results.ReadError(409, "projection_incomplete") from None

    reader = results.ResultsReader(
        read_snapshot, granted, "example-signing-secret",
        read_policy=published_policy,
        can_read_policy=can_read_policy,
    )
    with tempfile.TemporaryDirectory(prefix="adp-i23-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        binary = root / "bin"
        installer = root / "install.sh"
        installer.write_bytes((CLI / "install.sh").read_bytes())
        LocalGateway.requests = []
        LocalGateway.reply = None
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalGateway)
        gateway_url = f"http://127.0.0.1:{server.server_port}/api"
        environment = {
            **os.environ,
            "HOME": str(home),
            # The installer deliberately honors inherited deployment stores.
            # Keep both legacy and named stores inside this run-owned fixture.
            "ADP_HOME": str(home / ".adp"),
            "BG_CONFIG_DIR": str(home / ".bedrock-gateway"),
            "ADP_LEGACY_CONFIG_DIR": str(home / ".bedrock-gateway"),
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        for name in (
            "ADP_TENANT_ID",
            "ADP_TENANT_SUB",
            "ADP_TENANT_FLAG",
            "ADP_TENANT",
            "ADP_DEPLOYMENT",
            "ADP_DEPLOYMENT_ID",
            "ADP_DEPLOYMENT_NAME",
            "ADP_DEPLOYMENT_SOURCE",
            "ADP_DEPLOYMENT_URL",
        ):
            environment.pop(name, None)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            installed = subprocess.run(
                [
                    "sh",
                    str(installer),
                    "--gateway-url",
                    gateway_url,
                    "--prefix",
                    str(binary),
                    "--no-path-edit",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
            )
            assert installed.returncode == 0, installed.stderr
            assert (binary / "adp-insights.py").read_bytes() == (
                CLI / "adp-insights.py"
            ).read_bytes()
            token = session(home, gateway_url)
            LocalGateway.app = results.ResultsApp(
                reader,
                lambda environ: (registry.TENANT, "example-viewer")
                if environ.get("HTTP_AUTHORIZATION") == f"Bearer {token}"
                else None,
            )
            yield binary, environment, token, gateway_url
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
            LocalGateway.app = None
            LocalGateway.reply = None
            assert not worker.is_alive()


def read_result(binary, environment, token, gateway_url, as_of, portfolios=()):
    filters = [
        "--project",
        registry.PROJECT,
        "--product",
        "example-product",
        "--as-of",
        as_of,
    ]
    query = [
        ("view", "business_context"),
        ("product_id", "example-product"),
        ("as_of", as_of),
    ]
    for portfolio in portfolios:
        filters.extend(["--portfolio", portfolio])
        query.append(("portfolio_id", portfolio))
    observed = subprocess.run(
        [str(binary / "adp"), "insights", "context", "show", *filters, "--json"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert observed.returncode == 0, (observed.stdout, observed.stderr)
    cli = json.loads(observed.stdout)
    independent = Request(
        f"{gateway_url}/engineering-insights/v1/projects/{registry.PROJECT}/results?{urlencode(query)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urlopen(independent, timeout=5) as response:
        assert response.status == 200
        readback = json.load(response)
    assert cli["status"] == "ok"
    assert cli["detail"] == readback
    return readback


def insights_cli_context_read():
    snapshot, work, initiative, contribution = fixed_snapshot()
    with installed_gateway(snapshot) as (binary, environment, token, gateway_url):
        readback = read_result(
            binary, environment, token, gateway_url, registry.EVENT_TIME
        )
        assert readback["dataset_version"] == snapshot["dataset_version"]
        assert readback["mapping_versions"] == snapshot["mapping_versions"]
        assert readback["coverage"] == "complete"
        assert readback["as_of"] == registry.EVENT_TIME
        assert readback["work_count"] == 1
        assert readback["cost_by_currency"] == {"USD": "10.00"}
        context = next(
            item
            for item in readback["contexts"]
            if item["subject_ref"] == work["subject_ref"]
        )
        assert context["portfolio_ids"] == ["example-commerce"]
        assert context["initiative_refs"] == [initiative]
        assert context["contribution_refs"] == [contribution]
        assert context["effective_at"] == registry.EVENT_TIME
        assert all(item[1] == f"Bearer {token}" for item in LocalGateway.requests)
        assert len(LocalGateway.requests) == 2
        report = {
            "scenario": "SCN-01",
            "requirements_revision": "issue-8294:SCN-01",
            "source_revision": registry.SOURCE_REVISION,
            "cli_exit": 0,
            "independent_observation": {
                "status": 200,
                "qualified_work_ref": work["subject_ref"],
                "dataset_version": readback["dataset_version"],
                "mapping_versions": readback["mapping_versions"],
            },
        }
    report["cleanup"] = "complete"
    print(json.dumps(report, sort_keys=True))


def shared_snapshot():
    snapshot, work, _, _ = fixed_snapshot()
    product = registry.reference("product", "example-product", source="adp.profile")
    shared = registry.reference("portfolio", "example-shared")
    commerce = registry.reference("portfolio", "example-commerce")
    newer = registry.native(
        "work", "example-new-work", effective_at=LATER, recorded_at=LATER
    )
    new_links = [
        registry.link(
            "portfolio_product", "example-shared-membership", shared, product
        ),
        registry.link(
            "portfolio_product",
            "example-membership",
            commerce,
            product,
            recorded=LATER,
            revision="2",
            mapping="approved-2",
            end="2026-10-20T00:00:00Z",
        ),
        registry.link(
            "work_product",
            "example-new-work-link",
            newer["subject_ref"],
            product,
            recorded=LATER,
            start="2026-10-20T00:00:00Z",
            mapping="approved-2",
        ),
    ]
    fixture = registry.ScenarioFixture([*snapshot["relationships"], *new_links])
    snapshot["relationships"] = fixture.admitted_links()
    snapshot["facts"] += [
        copy.deepcopy(work),
        copy.deepcopy(snapshot["facts"][1]),
        newer,
    ]
    snapshot["mapping_versions"] = ["approved-1", "approved-2"]
    assert len(fixture.sink.rows) == len(snapshot["relationships"])
    assert fixture.sink.quarantined == []
    return snapshot, work, newer


def insights_cli_deduplicated_view():
    snapshot, original, newer = shared_snapshot()
    portfolios = ("example-commerce", "example-shared")
    with installed_gateway(snapshot) as (binary, environment, token, gateway_url):
        observations = []
        for as_of, expected_work_count, expected_mappings in (
            (registry.EVENT_TIME, 1, ["approved-1"]),
            (LATER, 2, ["approved-1", "approved-2"]),
        ):
            readback = read_result(
                binary, environment, token, gateway_url, as_of, portfolios
            )
            assert readback["coverage"] == "complete"
            assert readback["dataset_version"] == snapshot["dataset_version"]
            assert readback["mapping_versions"] == expected_mappings
            assert readback["work_count"] == expected_work_count
            assert readback["cost_by_currency"] == {"USD": "10.00"}
            assert readback["unallocated_by_currency"] == {"USD": "10.00"}
            assert readback["as_of"] == as_of
            refs = [row["subject_ref"] for row in readback["contexts"]]
            assert len(refs) == len({tuple(ref.items()) for ref in refs})
            assert sum(ref["record_id"] == "example-cost" for ref in refs) == 1
            assert sum(ref == original["subject_ref"] for ref in refs) == 1
            historical = next(
                row
                for row in readback["contexts"]
                if row["subject_ref"] == original["subject_ref"]
            )
            assert historical["portfolio_ids"] == list(portfolios)
            assert {
                link["mapping_version"] for link in historical["relationship_versions"]
            } == {"approved-1"}
            if as_of == LATER:
                future = next(
                    row
                    for row in readback["contexts"]
                    if row["subject_ref"] == newer["subject_ref"]
                )
                assert future["portfolio_ids"] == ["example-shared"]
                assert {
                    link["mapping_version"] for link in future["relationship_versions"]
                } == {"approved-2", "approved-1"}
            observations.append(
                {
                    "as_of": as_of,
                    "work_count": readback["work_count"],
                    "cost_by_currency": readback["cost_by_currency"],
                    "mapping_versions": readback["mapping_versions"],
                }
            )
        assert len(LocalGateway.requests) == 4
        assert all(item[1] == f"Bearer {token}" for item in LocalGateway.requests)
        report = {
            "scenario": "SCN-02",
            "requirements_revision": "issue-8294:SCN-02",
            "source_revision": registry.SOURCE_REVISION,
            "independent_observations": observations,
            "cli_exits": [0, 0],
        }
    report["cleanup"] = "complete"
    print(json.dumps(report, sort_keys=True))


def error_command(binary, environment, token, project_id=registry.PROJECT):
    observed = subprocess.run(
        [
            str(binary / "adp"),
            "insights",
            "context",
            "show",
            "--project",
            project_id,
            "--product",
            "example-product",
            "--as-of",
            registry.EVENT_TIME,
            "--json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    body = json.loads(observed.stdout)
    assert observed.returncode != 0, (observed.stdout, observed.stderr)
    assert body["detail"] == {}
    serialized = json.dumps(body)
    for hidden in (
        "native.source",
        "example-work",
        "example-cost",
        "example-initiative",
        "example-contribution",
        "example-hidden-source",
        "10.00",
        "work_count",
        "dataset_version",
        "mapping_versions",
        token,
    ):
        assert hidden not in serialized
    return observed.returncode, body


def error_readback(gateway_url, token, project_id=registry.PROJECT):
    query = urlencode(
        [
            ("view", "business_context"),
            ("product_id", "example-product"),
            ("as_of", registry.EVENT_TIME),
        ]
    )
    request = Request(
        f"{gateway_url}/engineering-insights/v1/projects/{project_id}/results?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(request, timeout=5):
            raise AssertionError("A denied read succeeded")
    except HTTPError as error:
        with error:
            return error.code, json.load(error)


def insights_cli_denial_coverage():
    snapshot, _, _, _ = fixed_snapshot()
    state = {"snapshot": snapshot, "granted": True}
    with installed_gateway(snapshot, state=state) as (
        binary,
        environment,
        token,
        gateway_url,
    ):
        assert (
            read_result(binary, environment, token, gateway_url, registry.EVENT_TIME)[
                "coverage"
            ]
            == "complete"
        )
        observations = []
        state["granted"] = False
        for label, project_id in (
            ("foreign", "foreign-project"),
            ("revoked", registry.PROJECT),
        ):
            exit_code, cli = error_command(binary, environment, token, project_id)
            http_status, api = error_readback(gateway_url, token, project_id)
            assert exit_code == 5
            assert cli["status"] == "failed"
            assert cli["error"]["code"] == "not_found"
            assert (http_status, api) == (
                404,
                {"error": {"code": "not_found", "message": "Result unavailable"}},
            )
            observations.append(
                {
                    "case": label,
                    "cli_exit": exit_code,
                    "cli_code": cli["error"]["code"],
                    "api_status": http_status,
                }
            )

        state["granted"] = True
        for label, reply in (
            ("older_gateway", (404, {"detail": "Not Found"})),
            (
                "unsupported",
                (
                    501,
                    {
                        "error": {
                            "code": "unsupported_capability",
                            "message": "example-hidden-source",
                        }
                    },
                ),
            ),
        ):
            LocalGateway.reply = reply
            exit_code, cli = error_command(binary, environment, token)
            http_status, api = error_readback(gateway_url, token)
            assert exit_code == 5
            assert cli["status"] == "failed"
            assert cli["error"]["code"] == "unsupported_operation"
            assert (http_status, api) == reply
            assert "example-hidden-source" not in json.dumps(cli)
            observations.append(
                {
                    "case": label,
                    "cli_exit": exit_code,
                    "cli_code": cli["error"]["code"],
                    "api_status": http_status,
                }
            )

        LocalGateway.reply = None
        for label, published, expected in (
            ("pending", None, "projection_pending"),
            ("incomplete", {"status": "incomplete"}, "projection_incomplete"),
            (
                "missing_facts",
                {
                    "status": "complete",
                    "dataset_version": "example-dataset-1",
                    "mapping_versions": ["approved-1"],
                    "relationships": [],
                },
                "projection_incomplete",
            ),
        ):
            state["snapshot"] = published
            exit_code, cli = error_command(binary, environment, token)
            http_status, api = error_readback(gateway_url, token)
            assert exit_code == 4
            assert cli["status"] == (
                "pending" if expected == "projection_pending" else "unavailable"
            )
            assert cli["error"]["code"] == expected
            assert (http_status, api) == (
                409,
                {"error": {"code": expected, "message": "Result unavailable"}},
            )
            observations.append(
                {
                    "case": label,
                    "cli_exit": exit_code,
                    "cli_code": expected,
                    "api_status": http_status,
                }
            )

        state["snapshot"] = snapshot
        assert (
            read_result(binary, environment, token, gateway_url, registry.EVENT_TIME)[
                "coverage"
            ]
            == "complete"
        )
        existing = subprocess.run(
            [str(binary / "adp"), "product", "--help"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert existing.returncode == 0, existing.stderr
        assert "adp product" in existing.stdout
        assert len(LocalGateway.requests) == 2 * (len(observations) + 2)
        report = {
            "scenario": "SCN-03",
            "requirements_revision": "issue-8294:SCN-03",
            "source_revision": registry.SOURCE_REVISION,
            "surface_reports": {
                "installed_cli": [
                    {
                        "case": row["case"],
                        "exit": row["cli_exit"],
                        "code": row["cli_code"],
                    }
                    for row in observations
                ],
                "independent_api": [
                    {"case": row["case"], "status": row["api_status"]}
                    for row in observations
                ],
            },
        }
    report["cleanup"] = "complete"
    print(json.dumps(report, sort_keys=True))


class I23Scenarios(unittest.TestCase):
    def test_installed_cli_does_not_use_inherited_operator_stores(self):
        with tempfile.TemporaryDirectory(prefix="adp-foreign-fixture-") as temporary:
            foreign = Path(temporary)
            config = foreign / "config"
            config.mkdir(mode=0o700)
            (config / "config.json").write_text(json.dumps({
                "gateway_url": "https://foreign.example/api",
            }))
            (config / "tokens.json").write_text('{"refresh_token":"fixture-only"}')
            expected = {path.name: path.read_bytes() for path in config.iterdir()}
            with patch.dict(os.environ, {
                "ADP_HOME": str(foreign / "adp"),
                "BG_CONFIG_DIR": str(config),
                "ADP_LEGACY_CONFIG_DIR": str(config),
                "ADP_DEPLOYMENT_ID": "foreign-id",
                "ADP_DEPLOYMENT_NAME": "foreign",
                "ADP_DEPLOYMENT_SOURCE": "inherited",
                "ADP_DEPLOYMENT_URL": "https://foreign.example/api",
            }):
                snapshot = fixed_snapshot()[0]
                with installed_gateway(snapshot) as (binary, environment, token, gateway_url):
                    home = Path(environment["HOME"])
                    self.assertEqual(environment["BG_CONFIG_DIR"], str(home / ".bedrock-gateway"))
                    self.assertEqual(environment["ADP_LEGACY_CONFIG_DIR"], str(home / ".bedrock-gateway"))
                    self.assertEqual(environment["ADP_HOME"], str(home / ".adp"))
                    observed = read_result(binary, environment, token, gateway_url, registry.EVENT_TIME)
                    self.assertEqual(observed["dataset_version"], snapshot["dataset_version"])
            self.assertEqual({path.name: path.read_bytes() for path in config.iterdir()}, expected)
            self.assertFalse((foreign / "adp").exists())

    def test_insights_cli_context_read(self):
        insights_cli_context_read()

    def test_insights_cli_deduplicated_view(self):
        insights_cli_deduplicated_view()

    def test_insights_cli_denial_coverage(self):
        insights_cli_denial_coverage()
