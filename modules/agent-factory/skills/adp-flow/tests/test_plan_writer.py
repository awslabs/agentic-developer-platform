"""Contract tests for the flow plan writer."""

import importlib.util
import json
from pathlib import Path

import pytest


WRITER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "plan_writer.py"
SPEC = importlib.util.spec_from_file_location("adp_flow_plan_writer", WRITER_PATH)
WRITER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRITER)


@pytest.fixture
def published_plan(tmp_path):
    intent = {
        "epic": "epic", "title": "Example", "description": "Build a checked feature",
        "story_repositories": {"story-one": "example/code"},
        "waves": [{"ref": "w1", "title": "First", "description": "Build the feature",
                   "stories": ["story-one"], "evaluation": "evaluation-one"}],
    }
    ledger = {"issues": {
        "epic": {"repository": "example/home", "number": 100, "title": "Example"},
        "story-one": {"repository": "example/code", "number": 101, "title": "Build the feature"},
        "evaluation-one": {"repository": "example/home", "number": 102, "title": "Evaluate the feature",
                           "url": "https://github.com/example/home/issues/102"},
    }}
    facts = {
        "home_repository": "example/home", "product_id": "example-product", "org_id": "example-org",
        "spec_revision": "revision-1", "emitted_at": "2026-11-03T00:00:00Z",
        "project_budget": {"available_usd": None},
        "repositories": {"example/home": {"provider_repository_id": 1, "installation_id": 2},
                         "example/code": {"provider_repository_id": 3, "installation_id": 4}},
    }
    (tmp_path / "intent.md").write_text("# Confirmed\n\n```json\n" + json.dumps(intent) + "\n```\n")
    (tmp_path / "story-mapping.json").write_text(json.dumps(ledger))
    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(facts))
    return tmp_path, facts_path, facts


@pytest.mark.parametrize("field", ["installation_id", "provider_repository_id"])
def test_missing_story_binding_fails_without_writes(published_plan, field):
    directory, facts_path, facts = published_plan
    del facts["repositories"]["example/code"][field]
    facts_path.write_text(json.dumps(facts))
    with pytest.raises(ValueError, match=f"story story-one is missing valid {field}"):
        WRITER.write_plan(directory, facts_path)
    assert not (directory / "proposal.authored.json").exists()
    assert not (directory / "policy-intent.json").exists()
    assert not (directory / "README.md").exists()


@pytest.mark.parametrize("revision", [5, "", None, "has space"])
def test_spec_revision_must_be_a_string_and_is_refused_without_writes(published_plan, revision):
    """The gateway's proposal schema takes a non-empty string; a Dev Box run that
    copied the Product's integer revision was refused with HTTP 422 (2026-11-03)."""
    directory, facts_path, facts = published_plan
    facts["spec_revision"] = revision
    facts_path.write_text(json.dumps(facts))
    with pytest.raises(ValueError, match="spec_revision must be a non-empty string"):
        WRITER.write_plan(directory, facts_path)
    assert not (directory / "proposal.authored.json").exists()
    assert not (directory / "policy-intent.json").exists()


def test_story_repositories_follow_ledger_and_include_home(published_plan):
    directory, facts_path, facts = published_plan
    intent = WRITER.load_intent(directory / "intent.md")
    intent["waves"][0]["stories"].append("story-home")
    intent["story_repositories"]["story-home"] = "example/home"
    (directory / "intent.md").write_text("# Confirmed\n\n```json\n" + json.dumps(intent) + "\n```\n")
    ledger_path = directory / "story-mapping.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["issues"]["story-home"] = {"repository": "example/home", "number": 103, "title": "Home feature"}
    ledger_path.write_text(json.dumps(ledger))
    document = WRITER.write_plan(directory, facts_path)
    story_nodes = {node["issue_ref"]: node for node in document["nodes"] if node["kind"] == "story"}
    assert {node["repository_identity"] for node in story_nodes.values()} == {"example/home", "example/code"}
    assert story_nodes["101"]["provider_repository_id"] == facts["repositories"]["example/code"]["provider_repository_id"]
    assert story_nodes["103"]["repository_connection"]["installation_id"] == facts["repositories"]["example/home"]["installation_id"]
    assert document["proposed_execution_policy"]["repository_ids"] == ["example/code", "example/home"]


def test_ledger_repository_mismatch_is_refused(published_plan):
    directory, facts_path, _ = published_plan
    ledger_path = directory / "story-mapping.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["issues"]["story-one"]["repository"] = "example/home"
    ledger_path.write_text(json.dumps(ledger))
    with pytest.raises(ValueError, match="story story-one repository mismatch"):
        WRITER.write_plan(directory, facts_path)
    assert not (directory / "proposal.authored.json").exists()
    assert not (directory / "policy-intent.json").exists()
    assert not (directory / "README.md").exists()


def test_capped_project_rejects_limit_above_headroom_without_files(published_plan):
    directory, facts_path, facts = published_plan
    facts["project_budget"]["available_usd"] = 120
    facts_path.write_text(json.dumps(facts))
    with pytest.raises(ValueError, match=r"limits.max_spend_usd 600 exceeds Project available_usd 120"):
        WRITER.write_plan(directory, facts_path)
    assert not any((directory / name).exists() for name in ("proposal.authored.json", "policy-intent.json", "README.md"))


def test_uncapped_project_is_metered_and_capped_project_within_headroom_succeeds(published_plan):
    directory, facts_path, facts = published_plan
    WRITER.write_plan(directory, facts_path)
    assert "Project uncapped, metered" in (directory / "README.md").read_text()
    facts["project_budget"]["available_usd"] = 600
    facts_path.write_text(json.dumps(facts))
    WRITER.write_plan(directory, facts_path)
    assert "Project available: $600; proposed flow limit: $600" in (directory / "README.md").read_text()


@pytest.mark.parametrize("budget", [{}, {"available_usd": "NaN"}, {"available_usd": -1}])
def test_missing_or_invalid_budget_fails_closed(published_plan, budget):
    directory, facts_path, facts = published_plan
    facts["project_budget"] = budget
    facts_path.write_text(json.dumps(facts))
    with pytest.raises(ValueError, match="Project"):
        WRITER.write_plan(directory, facts_path)
    assert not (directory / "proposal.authored.json").exists()


def test_recorded_interview_fixture_is_deterministic_and_has_bound_evaluations(tmp_path):
    import shutil

    source = Path(__file__).resolve().parent / "fixtures/recorded-interview"
    for name in ("intent.md", "story-mapping.json", "facts.json"):
        shutil.copyfile(source / name, tmp_path / name)
    first = WRITER.write_plan(tmp_path, tmp_path / "facts.json")
    output = (tmp_path / "proposal.authored.json").read_bytes()
    second = WRITER.write_plan(tmp_path, tmp_path / "facts.json")
    assert first == second
    assert (tmp_path / "proposal.authored.json").read_bytes() == output
    assert len(first["wave_metadata"]) == 3
    assert len([node for node in first["nodes"] if node["kind"] == "eval"]) == 3
    assert all(node["issue_ref"] for node in first["nodes"] if node["kind"] == "eval")
    assert first["proposed_execution_policy"]["repository_ids"] == ["example/component", "example/home"]


def test_recorded_interview_refuses_unpublished_evaluation(tmp_path):
    import shutil

    source = Path(__file__).resolve().parent / "fixtures/recorded-interview"
    for name in ("intent.md", "story-mapping.json", "facts.json"):
        shutil.copyfile(source / name, tmp_path / name)
    ledger_path = tmp_path / "story-mapping.json"
    ledger = json.loads(ledger_path.read_text())
    del ledger["issues"]["eval-three"]["number"]
    ledger_path.write_text(json.dumps(ledger))
    with pytest.raises(ValueError, match="wave w3 evaluation must be published"):
        WRITER.write_plan(tmp_path, tmp_path / "facts.json")
    assert not (tmp_path / "proposal.authored.json").exists()


@pytest.mark.parametrize("evaluation", [None, "", ["eval-one", "eval-two"]])
def test_wave_without_exactly_one_evaluation_is_refused(published_plan, evaluation):
    directory, facts_path, _ = published_plan
    intent = WRITER.load_intent(directory / "intent.md")
    intent["waves"][0]["evaluation"] = evaluation
    (directory / "intent.md").write_text("# Confirmed\n\n```json\n" + json.dumps(intent) + "\n```\n")
    with pytest.raises(ValueError, match="wave w1 must have exactly one distinct evaluation"):
        WRITER.write_plan(directory, facts_path)
    assert not (directory / "proposal.authored.json").exists()


@pytest.mark.parametrize("expiry", ["2026-11-04T00:00:00Z", "2026-11-04T01:00:00+01:00"])
def test_confirmed_expiry_is_preserved(published_plan, expiry):
    directory, facts_path, _ = published_plan
    intent = WRITER.load_intent(directory / "intent.md")
    intent["policy"] = {"expires_at": expiry}
    (directory / "intent.md").write_text("```json\n" + json.dumps(intent) + "\n```\n")
    proposal = WRITER.write_plan(directory, facts_path)
    assert proposal["proposed_execution_policy"]["expires_at"] == expiry
    assert json.loads((directory / "policy-intent.json").read_text())["proposed_execution_policy"]["expires_at"] == expiry


def test_omitted_expiry_defaults_to_fourteen_days(published_plan):
    directory, facts_path, _ = published_plan
    proposal = WRITER.write_plan(directory, facts_path)
    assert proposal["proposed_execution_policy"]["expires_at"] == "2026-11-17T00:00:00Z"


@pytest.mark.parametrize("expiry", [None, "", "not-a-date", 123, "2026-11-04", "2026-11-04T00:00:00",
                                   "2026-11-03T00:00:00Z", "2026-11-02T00:00:00Z"])
def test_invalid_confirmed_expiry_is_refused_without_writes(published_plan, expiry):
    directory, facts_path, _ = published_plan
    intent = WRITER.load_intent(directory / "intent.md")
    intent["policy"] = {"expires_at": expiry}
    (directory / "intent.md").write_text("```json\n" + json.dumps(intent) + "\n```\n")
    with pytest.raises(ValueError, match="expires_at"):
        WRITER.write_plan(directory, facts_path)
    assert not any((directory / name).exists() for name in ("proposal.authored.json", "policy-intent.json", "README.md"))


@pytest.mark.parametrize("actions,effects", [(["deploy"], []), (["operate"], []),
                                            (["develop"], ["external_spend"]), (["develop"], ["irreversible"])])
@pytest.mark.parametrize("gate", [None, {}, {"title": " "}])
def test_operational_wave_requires_a_named_readiness_gate(published_plan, actions, effects, gate):
    directory, facts_path, _ = published_plan
    intent = WRITER.load_intent(directory / "intent.md")
    intent["policy"] = {"allowed_actions": actions}
    intent["waves"][0].update(actions=actions, effects=effects, readiness_gate=gate)
    (directory / "intent.md").write_text("```json\n" + json.dumps(intent) + "\n```\n")
    with pytest.raises(ValueError, match="wave w1.*readiness_gate"):
        WRITER.write_plan(directory, facts_path)
    assert not any((directory / name).exists() for name in ("proposal.authored.json", "policy-intent.json", "README.md"))


@pytest.mark.parametrize("action", ["deploy", "operate"])
def test_operational_authority_requires_a_declared_operational_wave(published_plan, action):
    directory, facts_path, _ = published_plan
    intent = WRITER.load_intent(directory / "intent.md")
    intent["policy"] = {"allowed_actions": ["develop", action]}
    intent["waves"][0]["readiness_gate"] = {"title": "Unrelated checkpoint"}
    (directory / "intent.md").write_text("```json\n" + json.dumps(intent) + "\n```\n")
    with pytest.raises(ValueError, match=f"{action}.*declared wave"):
        WRITER.write_plan(directory, facts_path)
    assert not (directory / "proposal.authored.json").exists()


@pytest.mark.parametrize("actions,effects", [(["deploy"], []), (["operate"], []),
                                            (["develop"], ["external_spend"]), (["develop"], ["irreversible"])])
def test_readiness_gate_precedes_all_affected_stories(published_plan, actions, effects):
    directory, facts_path, _ = published_plan
    intent = WRITER.load_intent(directory / "intent.md")
    intent["policy"] = {"allowed_actions": actions}
    intent["waves"][0].update(actions=actions, effects=effects, readiness_gate={"title": "Owner checks readiness"})
    (directory / "intent.md").write_text("```json\n" + json.dumps(intent) + "\n```\n")
    document = WRITER.write_plan(directory, facts_path)
    gate = next(node for node in document["nodes"] if node["kind"] == "gate")
    assert gate["title"] == "Owner checks readiness"
    assert "executor" not in gate
    for story in (node for node in document["nodes"] if node["kind"] == "story"):
        assert {"from_address": gate["address"], "to_address": story["address"]} in document["edges"]
    assert "Owner checks readiness" in (directory / "README.md").read_text()


@pytest.mark.parametrize("fields", [{"actions": ["deploy"]}, {"effects": ["typo"]}, {"effects": "external_spend"}])
def test_undeclared_actions_or_invalid_effects_are_refused(published_plan, fields):
    directory, facts_path, _ = published_plan
    intent = WRITER.load_intent(directory / "intent.md")
    intent["waves"][0].update(fields, readiness_gate={"title": "Owner checks readiness"})
    (directory / "intent.md").write_text("```json\n" + json.dumps(intent) + "\n```\n")
    with pytest.raises(ValueError, match="wave w1"):
        WRITER.write_plan(directory, facts_path)
    assert not (directory / "proposal.authored.json").exists()
