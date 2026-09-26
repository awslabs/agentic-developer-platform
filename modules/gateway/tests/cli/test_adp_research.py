"""Research contract against real domain schemas and route allowlist."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[4]
CLI = ROOT / "modules/gateway/cli"
sys.path.insert(0, str(CLI))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


research = load("research_cli", CLI / "adp-superplane-research.py")
schemas = load("research_schema", ROOT / "modules/domain-apps/superplane/src/superplane-api/app/schemas/research.py")
ID = "5ca3b5a4-7364-4f2c-95e2-711cbe3524d6"


@pytest.fixture(autouse=True)
def session(monkeypatch):
    monkeypatch.setattr(research.common, "access_token", lambda: "test-human")
    monkeypatch.setattr(research.common, "ensure_can_mutate", lambda *a, **kw: {})


def test_canonical_domain_helper_matches_served_copy():
    assert (CLI / "adp-superplane-research.py").read_bytes() == (ROOT / "modules/domain-apps/superplane/cli/adp-superplane-research.py").read_bytes()


def test_actual_schema_and_route():
    api = Mock()
    api.request.return_value = schemas.ResearchFindingsList(items=[], total=0, page=1, page_size=20).model_dump(mode="json")
    result = research.execute(research.parser().parse_args(["findings", "list"]), api)
    method, path, _ = api.request.call_args.args
    assert [method, path.split("?")[0].removeprefix("/superplane/v1")] in json.loads(
        (ROOT / "modules/gateway/src/domain_proxy/superplane_routes.json").read_text()
    )
    assert result["detail"]["complete"] and not result["detail"]["snapshot"]


@pytest.mark.parametrize(
    "words", [["scan", "--request-file", "unused", "--request-id", ID], ["proposal", "generate", "--request-file", "unused", "--request-id", ID]]
)
def test_no_unsupported_paid_dispatch(words):
    api = Mock()
    assert research.execute(research.parser().parse_args(words), api)["status"] == "unavailable"
    api.request.assert_not_called()


def test_old_server_never_receives_mutation():
    api = Mock()
    api.request.return_value = {}
    result = research.execute(research.parser().parse_args(["proposal", "approve", ID, "--expect-revision", "a" * 64, "--yes"]), api)
    assert result["status"] == "unavailable"
    assert [c.args[0] for c in api.request.call_args_list] == ["GET"]


def test_exact_approval_schema():
    api = Mock()
    api.request.side_effect = [{"proposal_contract": "revision-idempotency-v1"}, {"id": ID, "revision": "b" * 64, "status": "approved"}]
    research.execute(research.parser().parse_args(["proposal", "approve", ID, "--expect-revision", "a" * 64, "--yes"]), api)
    body = schemas.ProposalApproveRequest.model_validate(api.request.call_args.args[2])
    assert body.expected_revision == "a" * 64 and body.approved_by is None


def test_duplicate_record_rejected():
    api = Mock()
    api.request.side_effect = [
        {"items": [{"id": ID}], "page": 1, "page_size": 1, "total": 2},
        {"items": [{"id": ID}], "page": 2, "page_size": 1, "total": 2},
    ]
    with pytest.raises(research.common.CliError, match="changed during pagination"):
        research.execute(research.parser().parse_args(["findings", "list", "--page-size", "1", "--max-pages", "2"]), api)


def test_parser_json(capsys):
    assert research.main(["proposal", "approve", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_no_rounding_idempotency_collision():
    with pytest.raises(ValueError, match="two decimal"):
        schemas.ProposalCreateRequest(
            request_id=ID, title="title", objective="objective long", hypothesis="hypothesis long", estimated_cost_usd=0.001
        )


@pytest.mark.parametrize("ack", [[], {"id": "foreign", "revision": "b" * 64, "status": "approved"}])
def test_malformed_mutation_ack_is_unknown(ack):
    api = Mock()
    api.request.side_effect = [{"proposal_contract": "revision-idempotency-v1"}, ack]
    with pytest.raises(research.common.CliError) as error:
        research.execute(research.parser().parse_args(["proposal", "approve", ID, "--expect-revision", "a" * 64, "--yes"]), api)
    assert error.value.code == "unknown_mutation_outcome"
    assert [c.args[0] for c in api.request.call_args_list] == ["GET", "PATCH"]


def test_time_range_requires_both_bounds_without_read():
    api = Mock()
    with pytest.raises(research.common.CliError, match="both timezone-aware"):
        research.execute(research.parser().parse_args(["findings", "list", "--start", "2026-09-25T00:00:00Z"]), api)
    api.request.assert_not_called()
