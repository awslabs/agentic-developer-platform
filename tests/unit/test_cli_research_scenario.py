"""Nightly research scenario cannot quietly introduce paid operations."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock
import pytest


@pytest.fixture
def scenario(monkeypatch):
    remote = Path(__file__).parents[1] / "e2e/cli_uplift/remote"
    monkeypatch.syspath_prepend(str(remote))
    for name in ["common", "story_reads", "story_research"]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(
        "story_research", remote / "story_research.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_only_read_commands(scenario):
    cli = Mock()
    cli.json.side_effect = [
        {"status": "ok", "detail": {"items": [], "complete": True}},
        {"status": "ok", "detail": {"items": [], "complete": True}},
        {"status": "ok", "detail": {"sources": []}},
        {"status": "ok", "detail": {"total_findings": 0}},
    ]
    evidence = {}
    scenario.research(cli, evidence)
    commands = [c.args[0] for c in cli.json.call_args_list]
    assert all(
        not set(c) & {"scan", "generate", "create", "approve", "reject"}
        for c in commands
    )
    assert len(commands) == 4
    assert "acceptance remains open" in evidence["qualification"]


def test_malformed_page_does_not_pass(scenario):
    cli = Mock()
    cli.json.return_value = {"status": "ok", "detail": {"items": []}}
    with pytest.raises(Exception, match="completeness"):
        scenario.research(cli, {})


def test_read_fixture_is_separate_from_mutation_recovery():
    from tests.e2e.cli_uplift import cases, preflight

    configured = {"research_readback": True}
    available = preflight.evaluate_fixtures(configured)
    assert cases.SUPERPLANE_RESEARCH in available
    assert cases.SUPERPLANE_DOMAIN not in available
    assert cases.SUPERPLANE_RESEARCH not in preflight.evaluate_fixtures({})
    assert [row.id for row in cases.suite_cases("research")] == ["E25"]
    assert cases.SUPERPLANE_RESEARCH in cases.BY_ID["E25"].requires
