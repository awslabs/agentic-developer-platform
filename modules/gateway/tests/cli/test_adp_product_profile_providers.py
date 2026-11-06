"""Installed Product CLI passes native provider selections to the scoped Gateway API."""

import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = Path(__file__).parents[2] / "cli/adp-product.py"
spec = importlib.util.spec_from_file_location("adp_product_profile_providers", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.mark.parametrize(
    "provider,kind,namespace,native_id",
    [
        ("sonarqube", "quality_project", "/", "team:api"),
        ("jenkins", "ci_job", "team", "team/api"),
        ("argocd", "deployment_application", "team/ns", "api"),
        ("gitlab", "repository", "88", "901"),
        ("pagerduty", "incident_service", "/", "P123"),
        ("linear", "tracker_team", "/", "team-a"),
        ("slack", "communication_channel", "/", "C123"),
    ],
)
def test_cli_preserves_typed_provider_selection(tmp_path, monkeypatch, capsys, provider, kind, namespace, native_id):
    selection = {
        "provider": provider,
        "kind": kind,
        "namespace": namespace,
        "native_id": native_id,
        "connection_ref": {"kind": "credential", "id": "registered-connection"},
    }
    source_file = tmp_path / "source.json"
    source_file.write_text(json.dumps(selection), encoding="utf-8")

    class RecordedClient:
        def request(self, method, path, body=None, **kwargs):
            assert method == "POST" and path == "/v1/products/product/projects/project/bindings"
            assert body == selection
            assert kwargs["extra_headers"] == {"If-Profile-Revision": "3", "Idempotency-Key": "profile_provider_12345"}
            return {"id": "binding", "status": "unverified", "selection": selection}

    monkeypatch.setattr(cli.common, "Api", lambda: RecordedClient())
    monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *args, **kwargs: None)
    assert (
        cli.main(
            [
                "source-bind",
                "--product",
                "product",
                "--project",
                "project",
                "--input",
                str(source_file),
                "--revision",
                "3",
                "--idempotency-key",
                "profile_provider_12345",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["detail"]["selection"] == selection


@pytest.mark.parametrize(
    "provider,kind,namespace,native_id",
    [
        ("sonarqube", "quality_project", "/", "team:api"),
        ("jenkins", "ci_job", "team", "team/api"),
        ("argocd", "deployment_application", "team/ns", "api"),
        ("gitlab", "repository", "77", "901"),
        ("pagerduty", "incident_service", "/", "P123"),
        ("linear", "tracker_team", "/", "team-a"),
        ("slack", "communication_channel", "T123", "C123"),
    ],
)
def test_cli_reads_bounded_typed_pages_without_native_fallback(monkeypatch, capsys, provider, kind, namespace, native_id):
    resource = {"kind": kind, "namespace": namespace, "native_id": native_id, "name": "Same name", "source_revision": "fixture-revision"}
    calls = []

    class RecordedClient:
        def request(self, method, path, body=None, **kwargs):
            calls.append((method, path))
            assert method == "GET" and body is None
            query = parse_qs(urlsplit(path).query)
            assert query["connection_kind"] == ["credential"]
            assert query["connection_id"] == ["registered-connection"]
            assert query["limit"] == ["1"]
            if len(calls) == 1:
                assert "resource_cursor" not in query
                return {
                    "items": [
                        {
                            "provider": provider,
                            "coverage": "partial",
                            "capabilities": [kind],
                            "resources": [resource],
                            "resource_cursor": "opaque-page",
                        }
                    ]
                }
            assert query["resource_cursor"] == ["opaque-page"]
            return {"items": [{"provider": provider, "coverage": "unavailable", "capabilities": [kind], "resources": []}]}

    monkeypatch.setattr(cli.common, "Api", lambda: RecordedClient())
    args = [
        "sources",
        "--product",
        "product",
        "--project",
        "project",
        "--connection-kind",
        "credential",
        "--connection-id",
        "registered-connection",
        "--limit",
        "1",
        "--json",
    ]
    assert cli.main(args) == 0
    first_page = json.loads(capsys.readouterr().out)["detail"]
    first = first_page["items"][0]
    assert first["resources"] == [resource] and first["resource_cursor"] == "opaque-page"
    if provider in ("jenkins", "pagerduty"):
        assert not any(("Pipeline" if provider == "jenkins" else "Support") in item for item in first_page["next_prerequisites"])
    assert "not covered" not in first["next_prerequisite"]
    assert cli.main([*args, "--resource-cursor", first["resource_cursor"]]) == 0
    stopped = json.loads(capsys.readouterr().out)["detail"]["items"][0]
    assert stopped["coverage"] == "unavailable" and not stopped["resources"]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "provider,kind,native_id",
    [
        ("sonarqube", "quality_project", "team:api"),
        ("jenkins", "ci_job", "team/api"),
        ("argocd", "deployment_application", "api"),
        ("gitlab", "repository", "901"),
        ("pagerduty", "incident_service", "P123"),
        ("linear", "tracker_team", "team-a"),
        ("slack", "communication_channel", "C123"),
    ],
)
def test_cli_verifies_discovers_applies_and_reads_saved_reference(monkeypatch, capsys, provider, kind, native_id):
    root = "/v1/products/product/projects/project"
    destination = {
        "sonarqube": ("advanced", "observability"),
        "jenkins": ("advanced", "pipelines"),
        "argocd": ("advanced", "deployments"),
        "gitlab": ("basic", "repositories"),
        "pagerduty": ("advanced", "support"),
        "linear": ("basic", "trackers"),
        "slack": ("basic", "collaboration"),
    }[provider]
    reference = {
        "tenant_id": "tenant",
        "project_id": "project",
        "source_connection_id": "connection",
        "record_type": kind,
        "record_id": native_id,
        "record_revision": "native-revision",
    }
    proof = {"paths": ["/configuration/" + "/".join(destination)], "source_refs": [reference]}
    calls = []

    class RecordedClient:
        def request(self, method, path, body=None, **kwargs):
            calls.append((method, path, body, kwargs))
            if method == "POST":
                assert kwargs["extra_headers"] == {"If-Profile-Revision": "3", "Idempotency-Key": "profile_provider_12345"}
            if path == root + "/bindings/binding/verify":
                assert (method, body) == ("POST", None)
                return {"id": "verified", "state": "succeeded", "kind": "verifyBinding"}
            if path == root + "/operations/verified":
                return {"id": "verified", "state": "succeeded", "kind": "verifyBinding", "coverage": "complete"}
            if path == root + "/bindings?limit=50":
                return {
                    "items": [{"id": "binding", "status": "healthy", "selection": {"provider": provider, "kind": kind, "native_id": native_id}}],
                    "next_cursor": None,
                }
            if path == root + "/onboarding-sessions":
                assert body == {"mode": "direct", "selected_binding_ids": []}
                return {"id": "session", "revision": 3, "state": "open"}
            if path == root + "/onboarding-sessions/session" and method == "GET":
                return {"id": "session", "revision": 3, "state": "open"}
            if path == root + "/onboarding-sessions/session/discoveries":
                assert body == {"binding_ids": ["binding"], "adapter": "devlake"}
                return {
                    "id": "discovery",
                    "kind": "discoverSources",
                    "state": "succeeded",
                    "coverage": "complete",
                    "resources": [{"provider": provider, "kind": kind, "native_id": native_id}],
                }
            if path == root + "/operations/discovery":
                return {
                    "id": "discovery",
                    "product_id": "product",
                    "project_id": "project",
                    "kind": "discoverSources",
                    "state": "succeeded",
                    "coverage": "complete",
                }
            if path == root + "/onboarding-sessions/session/source-enrichment":
                assert body == {"operation_id": "discovery"}
                return {"revision": 3, "applied": [proof], "conflicts": [], "suggestions": [], "coverage": "complete"}
            if path == root:
                return {"revision": 3, "configuration": {destination[0]: {destination[1]: [reference]}}}
            raise AssertionError((method, path))

    monkeypatch.setattr(cli.common, "Api", lambda: RecordedClient())
    monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *args, **kwargs: None)
    base = ["--product", "product", "--project", "project", "--json"]
    write = ["--revision", "3", "--idempotency-key", "profile_provider_12345"]
    for command in (
        ["source-verify", "--binding", "binding", *write],
        ["source-operation", "--operation", "verified"],
        ["source-bindings"],
        ["source-session", *write],
        ["source-session", "--session", "session"],
        ["source-discover", "--session", "session", "--binding", "binding", *write],
        ["source-operation", "--operation", "discovery"],
        ["source-apply", "--session", "session", "--operation", "discovery", *write],
    ):
        assert cli.main([command[0], *base, *command[1:]]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
    assert output["detail"]["applied"] == [proof]
    assert len(calls) == 11


def test_cli_continuation_uses_only_served_devlake_discovery(monkeypatch, capsys):
    calls = []

    class StoppedClient:
        def request(self, method, path, body=None, **kwargs):
            calls.append((method, path, body))
            if method == "GET":
                return {"id": "session", "revision": 3, "state": "open"}
            assert path.endswith("/onboarding-sessions/session/discoveries")
            assert body == {"binding_ids": ["binding"], "adapter": "devlake", "cursor": "opaque-page"}
            raise cli.common.CliError("Go connector unavailable", "connector_unavailable")

    monkeypatch.setattr(cli.common, "Api", lambda: StoppedClient())
    monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *args, **kwargs: None)
    assert (
        cli.main(
            [
                "source-discover",
                "--product",
                "product",
                "--project",
                "project",
                "--session",
                "session",
                "--binding",
                "binding",
                "--cursor",
                "opaque-page",
                "--revision",
                "3",
                "--idempotency-key",
                "profile_provider_12345",
                "--json",
            ]
        )
        != 0
    )
    assert "connector_unavailable" in capsys.readouterr().out
    assert len(calls) == 2


@pytest.mark.parametrize(
    "state,coverage,cursor",
    [
        ("running", "partial", None),
        ("succeeded", "unavailable", None),
        ("succeeded", "partial", "pending-page"),
    ],
)
def test_cli_refuses_incomplete_discovery_before_apply(monkeypatch, capsys, state, coverage, cursor):
    calls = []

    class RecordedClient:
        def request(self, method, path, body=None, **kwargs):
            calls.append((method, path))
            assert method == "GET" and path.endswith("/operations/discovery")
            return {
                "id": "discovery",
                "product_id": "product",
                "project_id": "project",
                "kind": "discoverSources",
                "state": state,
                "coverage": coverage,
                **({"cursor": cursor} if cursor else {}),
            }

    monkeypatch.setattr(cli.common, "Api", lambda: RecordedClient())
    assert (
        cli.main(
            [
                "source-apply",
                "--product",
                "product",
                "--project",
                "project",
                "--session",
                "session",
                "--operation",
                "discovery",
                "--revision",
                "3",
                "--idempotency-key",
                "profile_provider_12345",
                "--json",
            ]
        )
        != 0
    )
    assert "incomplete" in capsys.readouterr().out
    assert len(calls) == 1


def test_cli_rejects_mismatched_saved_reference(monkeypatch, capsys):
    root = "/v1/products/product/projects/project"

    class RecordedClient:
        def request(self, method, path, body=None, **kwargs):
            if path.endswith("/operations/discovery"):
                return {
                    "id": "discovery",
                    "kind": "discoverSources",
                    "product_id": "product",
                    "project_id": "project",
                    "state": "succeeded",
                    "coverage": "complete",
                }
            if path.endswith("/source-enrichment"):
                return {"revision": 4, "applied": [{"paths": ["/configuration/basic/quality_projects"], "source_refs": [{"record_id": "native-id"}]}]}
            assert path == root and method == "GET"
            return {"revision": 4, "configuration": {"basic": {"quality_projects": [{"record_id": "wrong"}]}}}

    monkeypatch.setattr(cli.common, "Api", lambda: RecordedClient())
    monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *args, **kwargs: None)
    assert (
        cli.main(
            [
                "source-apply",
                "--product",
                "product",
                "--project",
                "project",
                "--session",
                "session",
                "--operation",
                "discovery",
                "--revision",
                "3",
                "--idempotency-key",
                "profile_provider_12345",
                "--json",
            ]
        )
        != 0
    )
    assert "does not match" in capsys.readouterr().out
