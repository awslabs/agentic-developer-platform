"""Shared multi-repository contract tests with distinct source and harness repos."""

import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from tests.orchestration.test_compile import approval, session  # noqa: F401


def contract():
    path = Path(__file__).resolve().parents[4] / "contracts/multi-repository-evaluation/v1/models.py"
    module_name = "_adp_multi_repository_evaluation_v1_test"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def verified_repositories(monkeypatch):
    if importlib.util.find_spec("src.orchestration.repository_binding") is None:
        yield
        return
    from src.orchestration import repository_binding

    bindings = {
        "fixture/source-a": (101, 901, 17),
        "fixture/source-b": (202, 902, 18),
        "fixture/harness": (303, 903, 19),
    }
    observed = set()

    async def resolve(db, *, tenant_id, name, issue, operation, repository_id=None, installation_id=None, permitted_names=None):
        assert tenant_id == "org-alpha"
        assert name in bindings
        expected_repository, expected_installation, expected_issue = bindings[name]
        assert (repository_id, installation_id, issue) == (expected_repository, expected_installation, expected_issue)
        assert operation == "accept"
        assert permitted_names is None or name in permitted_names
        observed.add(name)
        return repository_binding.VerifiedRepository("github", repository_id, name, installation_id, name, issue, operation)

    monkeypatch.setattr(repository_binding, "resolve_story_repository", resolve)
    yield
    assert {"fixture/source-a", "fixture/source-b"} <= observed


def evaluation(mode="repository"):
    document = {
        "evidence_schema": "multi-repository-evaluation/v1",
        "mode": mode,
        "acceptance_mode": "machine",
        "harness": {
            "repository": "fixture/harness",
            "repository_id": 303,
            "installation_id": 903,
            "source_revision": "a" * 40,
            "definition_revision": "b" * 40,
            "harness_digest": "c" * 64,
            "adapter": "approved-harness",
            "operation": "evaluate",
        },
        "predecessors": [
            {"address": "a", "repository_id": 101, "evidence_kind": "merge", "required_checks": [{"name": "ci", "app_id": 7}]},
            {"address": "b", "repository_id": 202, "evidence_kind": "deployment"},
        ],
        "criteria": [{"criterion_id": "AC-01"}],
    }
    if mode == "deployed":
        document["environment"] = {
            "connection_id": "target-connection",
            "target_contract_id": "target-v1",
            "target_contract_hash": "d" * 64,
            "runtime_contract": "api-v1",
            "deployment_receipt_keys": ["deployment-a", "deployment-b"],
        }
    return document


@pytest.mark.parametrize("mode", ["repository", "deployed"])
def test_explicit_predecessors_and_separate_harness(mode):
    parsed = contract().EvaluationSpecification.model_validate(evaluation(mode))
    assert [source.repository_id for source in parsed.predecessors] == [101, 202]
    assert parsed.harness.repository_id == 303
    if mode == "repository":
        assert parsed.environment is None
    else:
        assert parsed.environment.target_contract_id == "target-v1"
    assert "product_id" not in parsed.model_dump()
    assert 404 not in [source.repository_id for source in parsed.predecessors]


@pytest.mark.parametrize("fault", [None, "wrong-definition", "repository-mode", "transport-input"])
def test_deployed_probe_declaration_keeps_existing_digests_and_refuses_unpinned_scope(fault):
    models = contract()
    original = models.EvaluationSpecification.model_validate(evaluation("deployed"))
    assert "probe" not in original.model_dump(mode="json")
    probe = {
        "path": ".github/workflows/integration-probe.yml",
        "definition_revision": original.harness.definition_revision,
        "account_id": "123456789012",
        "region": "us-east-1",
        "artifact_name": "evaluation-receipt",
        "artifact_path": "evaluation-receipt.json",
        "inputs": {"target_contract_hash": original.environment.target_contract_hash, "runtime_contract": original.environment.runtime_contract},
    }
    if fault == "wrong-definition":
        probe["definition_revision"] = "0" * 40
    elif fault == "transport-input":
        probe["inputs"]["adp_correlation"] = "untrusted"
    document = evaluation("repository" if fault == "repository-mode" else "deployed")
    document["probe"] = probe
    if fault is not None:
        with pytest.raises(ValidationError):
            models.EvaluationSpecification.model_validate(document)
        return
    selected = models.EvaluationSpecification.model_validate(document)
    assert selected.probe.definition_revision == selected.harness.definition_revision
    assert models.specification_digest(selected) != models.specification_digest(original)


@pytest.mark.parametrize("mode", ["repository", "deployed"])
def test_mode_requires_exact_environment_shape(mode):
    document = evaluation(mode)
    if mode == "deployed":
        del document["environment"]
    else:
        document["environment"] = evaluation("deployed")["environment"]
    with pytest.raises(ValidationError):
        contract().EvaluationSpecification.model_validate(document)


@pytest.mark.usefixtures("verified_repositories")
async def test_product_bound_proposal_preserves_explicit_sources_and_harness(session, approval):  # noqa: F811
    from sqlalchemy import select

    from src.orchestration.compile import compile_proposal
    from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow
    from src.orchestration.proposal import RepositoryConnection, validate_proposal
    from tests.orchestration.test_compile import address, valid_proposal
    from tests.orchestration.test_execution_policy_acceptance import a_policy

    proposal = valid_proposal()
    proposal.intent_ref = "epic-c"
    for story, repository_id, installation_id, issue_number in zip(proposal.nodes[:2], [101, 202], [901, 902], [17, 18]):
        story.issue_ref = str(issue_number)
        story.repository_identity = f"fixture/source-{story.address[-1]}"
        story.provider_repository_id = repository_id
        story.repository_connection = RepositoryConnection(provider="github", installation_id=installation_id)
    evaluation_node = next(node for node in proposal.nodes if node.kind == "eval")
    evaluation_node.repository_identity = "fixture/harness"
    evaluation_node.provider_repository_id = 303
    evaluation_node.repository_connection = RepositoryConnection(provider="github", installation_id=903)
    evaluation_node.criteria = ["AC-01"]
    specification = evaluation()
    for source, story in zip(specification["predecessors"], proposal.nodes[:2]):
        source["address"] = story.address
    evaluation_node.evaluation = specification
    proposal.execution_policy = a_policy(repository_ids=["fixture/source-a", "fixture/source-b", "fixture/harness"])

    assert not validate_proposal(proposal)
    accepted = await compile_proposal(session, proposal, approval)
    flow = await session.get(OrchestrationFlow, accepted.flow_id)
    plan = (await session.scalars(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == accepted.flow_id))).one()
    stored = next(node["evaluation"] for node in plan.plan_document["nodes"] if node["kind"] == "eval")
    assert flow.product_id == plan.plan_document["product_id"] == proposal.product_id
    assert {source["repository_id"] for source in stored["predecessors"]} == {101, 202}
    assert stored["harness"]["repository_id"] == 303
    assert proposal.intent_ref == "epic-c" and 404 not in {source["repository_id"] for source in stored["predecessors"]}
    assert set(source["address"] for source in stored["predecessors"]) == {address("story-a"), address("story-b")}

    specification["predecessors"][0]["repository_id"] = 404
    assert "evaluation_source_repository_mismatch" in {violation.rule for violation in validate_proposal(proposal)}
    specification["predecessors"][0]["repository_id"] = 101
    evaluation_node.provider_repository_id = 404
    assert "evaluation_harness_binding_mismatch" in {violation.rule for violation in validate_proposal(proposal)}


@pytest.mark.parametrize("harness_issue", [None, "19"], ids=["missing-issue", "verified-issue"])
async def test_repository_mode_acceptance_requires_a_verified_harness_issue(session, approval, verified_repositories, monkeypatch, harness_issue):  # noqa: F811
    from sqlalchemy import select

    from src.orchestration import evaluation_acceptance, repository_binding
    from src.orchestration.compile import compile_proposal
    from src.orchestration.evaluation_acceptance import (
        EvaluationAcceptanceError,
        EvaluationAcceptanceRequest,
        accept_evaluation,
        accepted_contract,
        preview_evaluation,
    )
    from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationNode
    from src.orchestration.proposal import RepositoryConnection, validate_proposal
    from src.orchestration.repository_evaluation_contract import harness_digest
    from tests.orchestration.test_compile import valid_proposal
    from tests.orchestration.test_execution_policy_acceptance import a_policy

    proposal = valid_proposal()
    for story, repository_id, installation_id, issue_number in zip(proposal.nodes[:2], [101, 202], [901, 902], [17, 18]):
        story.issue_ref = str(issue_number)
        story.repository_identity = f"fixture/source-{story.address[-1]}"
        story.provider_repository_id = repository_id
        story.repository_connection = RepositoryConnection(provider="github", installation_id=installation_id)
    evaluation_node = next(node for node in proposal.nodes if node.kind == "eval")
    evaluation_node.repository_identity = "fixture/harness"
    evaluation_node.provider_repository_id = 303
    evaluation_node.repository_connection = RepositoryConnection(provider="github", installation_id=903)
    evaluation_node.issue_ref = harness_issue
    evidence = evaluation()
    evidence["harness"].update(adapter="engine-repository-evidence-v1", operation="collect", harness_digest=harness_digest())
    for source, story in zip(evidence["predecessors"], proposal.nodes[:2]):
        source["address"] = story.address
    evaluation_node.criteria = ["AC-01"]
    evaluation_node.evaluation = evidence
    proposal.execution_policy = a_policy(repository_ids=["fixture/source-a", "fixture/source-b", "fixture/harness"])
    assert not validate_proposal(proposal)
    accepted = await compile_proposal(session, proposal, approval)
    plan = (await session.scalars(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == accepted.flow_id))).one()
    node = (
        await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == accepted.flow_id, OrchestrationNode.kind == "eval"))
    ).one()
    monkeypatch.setattr(evaluation_acceptance, "evaluation_inputs", AsyncMock(return_value=(None, None)))
    observed = []

    async def resolve(_session, **kwargs):
        observed.append((kwargs["name"], kwargs["issue"], kwargs["installation_id"]))
        return SimpleNamespace(repository_id=kwargs["repository_id"], installation_id=kwargs["installation_id"])

    monkeypatch.setattr(repository_binding, "resolve_story_repository", resolve)
    request = EvaluationAcceptanceRequest(
        node_id=node.id,
        expected_plan_version=plan.version,
        expected_plan_hash=plan.plan_hash,
        specification=evidence,
        authorize_evaluate=True,
        reason="Approve repository-only evidence from harness H.",
    )
    if harness_issue is None:
        with pytest.raises(EvaluationAcceptanceError, match="evaluation_harness_issue_required"):
            await preview_evaluation(session, flow_id=accepted.flow_id, actor=approval, request=request)
        assert observed == []
    else:
        preview = await preview_evaluation(session, flow_id=accepted.flow_id, actor=approval, request=request)
        assert preview["snapshot"] and observed == [
            ("fixture/harness", 19, 903),
            ("fixture/source-a", 17, 901),
            ("fixture/source-b", 18, 902),
        ]
        receipt = await accept_evaluation(
            session,
            flow_id=accepted.flow_id,
            actor=approval,
            request=request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
        )
        assert receipt["decision_id"]
        attached = await accepted_contract(session, node=node, plan=plan)
        assert attached[1].harness.repository == "fixture/harness" and attached[1].mode == "repository"


def golden():
    path = Path(__file__).resolve().parents[4] / "contracts/multi-repository-evaluation/v1/evaluation-receipt.golden.json"
    return json.loads(path.read_text())


def documents():
    model = contract()
    vector = golden()
    specification = model.EvaluationSpecification.model_validate(vector["specification"])
    receipt = model.EvaluationReceipt.model_validate(vector["receipt"])
    return model, specification, receipt


def test_independent_golden_digests_and_exact_receipt():
    model, specification, receipt = documents()
    vector = golden()
    from src.orchestration.repository_evaluation_contract import canonical

    assert hashlib.sha256(canonical(specification.model_dump(mode="json")).encode()).hexdigest() == (
        "159ea212e26a26cf3f7bdfd15f3cb9cbb9a66f1fd7dda96a19707490338b346a"
    )
    independent_specification = deepcopy(vector["specification"])
    independent_specification["harness"].pop("repository")
    manual_specification_digest = hashlib.sha256(json.dumps(independent_specification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manual_inputs = {
        "evidence_schema": "multi-repository-evaluation/v1",
        "specification_digest": manual_specification_digest,
        "sources": vector["sources"],
    }
    manual_input_digest = hashlib.sha256(json.dumps(manual_inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert manual_specification_digest == vector["expected_specification_digest"] == model.specification_digest(specification)
    assert manual_input_digest == vector["expected_input_set_digest"] == model.input_set_digest(specification, receipt.sources)
    model.verify_receipt(specification, receipt, receipt.sources, now=datetime(2026, 10, 20, 12, 1, tzinfo=UTC))
    reversed_specification = deepcopy(vector["specification"])
    reversed_specification["predecessors"].reverse()
    assert model.specification_digest(model.EvaluationSpecification.model_validate(reversed_specification)) == manual_specification_digest
    assert model.input_set_digest(specification, list(reversed(receipt.sources))) == manual_input_digest


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_missing_and_extra_sources_are_rejected(change):
    model, specification, receipt = documents()
    expected_sources = list(receipt.sources)
    if change == "missing":
        expected_sources.pop()
    else:
        extra = receipt.sources[0].model_copy(update={"address": "flow/epic/wave/extra"})
        expected_sources.append(extra)
    with pytest.raises(ValueError, match="evaluation_input_set_mismatch"):
        model.verify_receipt(specification, receipt, expected_sources, now=receipt.observed_at)
    broken = deepcopy(golden()["receipt"])
    if change == "missing":
        broken["sources"].pop()
    else:
        broken["sources"].append({**broken["sources"][0], "address": "flow/epic/wave/extra"})
    with pytest.raises(ValueError, match="evaluation_input_set_mismatch"):
        model.verify_receipt(specification, model.EvaluationReceipt.model_validate(broken), receipt.sources, now=receipt.observed_at)


def test_duplicate_source_and_check_identities_are_rejected():
    model, specification, receipt = documents()
    duplicate = list(receipt.sources) + [receipt.sources[0]]
    with pytest.raises(ValueError, match="evaluation_input_set_mismatch"):
        model.input_set_digest(specification, duplicate)
    broken = deepcopy(golden()["receipt"])
    broken["sources"].append(broken["sources"][0])
    with pytest.raises(ValidationError, match="receipt source addresses must be unique"):
        model.EvaluationReceipt.model_validate(broken)
    broken = deepcopy(golden()["specification"])
    broken["predecessors"].append(broken["predecessors"][0])
    with pytest.raises(ValidationError, match="predecessor addresses must be unique"):
        model.EvaluationSpecification.model_validate(broken)
    broken = deepcopy(golden()["specification"])
    broken["predecessors"][0]["required_checks"].append(broken["predecessors"][0]["required_checks"][0])
    with pytest.raises(ValidationError, match="required check identities must be unique"):
        model.EvaluationSpecification.model_validate(broken)


@pytest.mark.parametrize(
    "field", ["reviewed_head_sha", "merge_sha", "source_binding_revision", "claim_generation", "accepted_plan_hash", "repository_id"]
)
def test_changed_current_source_invalidates_old_evidence(field):
    model, specification, receipt = documents()
    original = receipt.sources[0]
    revision_length = 64 if field.endswith("hash") or field == "source_binding_revision" else 40
    replacement = 404 if field == "repository_id" else 2 if field == "claim_generation" else "f" * revision_length
    changed = original.model_copy(update={field: replacement})
    with pytest.raises(ValueError, match="evaluation_input_digest_mismatch"):
        model.verify_receipt(specification, receipt, [changed, receipt.sources[1]], now=receipt.observed_at)


@pytest.mark.parametrize("field", ["source_revision", "definition_revision", "harness_digest", "repository_id"])
def test_harness_revision_changes_specification_identity(field):
    model, specification, receipt = documents()
    changes = {field: 404 if field == "repository_id" else "f" * (64 if field == "harness_digest" else 40)}
    changed = specification.model_copy(update={"harness": specification.harness.model_copy(update=changes)})
    with pytest.raises(ValueError, match="evaluation_specification_mismatch"):
        model.verify_receipt(changed, receipt, receipt.sources, now=receipt.observed_at)


def test_target_revision_and_required_checks_change_specification_identity():
    model = contract()
    document = evaluation("deployed")
    specification = model.EvaluationSpecification.model_validate(document)
    changed = deepcopy(document)
    changed["environment"]["target_contract_hash"] = "e" * 64
    assert model.specification_digest(specification) != model.specification_digest(model.EvaluationSpecification.model_validate(changed))
    changed = deepcopy(golden()["specification"])
    changed["predecessors"][0]["required_checks"][0]["app_id"] = 8
    assert model.specification_digest(model.EvaluationSpecification.model_validate(changed)) != golden()["expected_specification_digest"]


def test_receipt_requires_exact_successful_checks_and_freshness():
    model, specification, receipt = documents()
    broken = deepcopy(golden()["receipt"])
    broken["sources"][0]["checks"][0]["head_sha"] = "f" * 40
    with pytest.raises(ValueError, match="evaluation_check_revision_mismatch"):
        model.verify_receipt(specification, model.EvaluationReceipt.model_validate(broken), receipt.sources, now=receipt.observed_at)
    broken["sources"][0]["checks"][0]["name"] = "other"
    with pytest.raises(ValueError, match="evaluation_required_checks_mismatch"):
        model.verify_receipt(specification, model.EvaluationReceipt.model_validate(broken), receipt.sources, now=receipt.observed_at)
    with pytest.raises(ValueError, match="evaluation_receipt_not_current"):
        model.verify_receipt(specification, receipt, receipt.sources, now=receipt.expires_at + timedelta(seconds=1))


def test_failed_criterion_attribution_must_name_a_declared_source():
    model, specification, receipt = documents()
    broken = deepcopy(golden()["receipt"])
    broken["verdict"] = "fail"
    broken["criteria"][0].update(outcome="fail", affected_source_address=receipt.sources[1].address)
    attributed = model.EvaluationReceipt.model_validate(broken)
    model.verify_receipt(specification, attributed, receipt.sources, now=receipt.observed_at)
    broken["criteria"][0]["affected_source_address"] = "fixture/unrelated"
    with pytest.raises(ValueError, match="evaluation_affected_source_mismatch"):
        model.verify_receipt(specification, model.EvaluationReceipt.model_validate(broken), receipt.sources, now=receipt.observed_at)
    broken["criteria"][0].update(outcome="pass", affected_source_address=receipt.sources[1].address)
    with pytest.raises(ValueError, match="only a failed criterion"):
        model.EvaluationReceipt.model_validate(broken)


def test_gateway_receipt_selector_checks_current_sources():
    from src.orchestration.evaluation_contract import verify_multi_repository_receipt

    vector = golden()
    received = verify_multi_repository_receipt(
        vector["specification"], vector["receipt"], vector["sources"], now=datetime(2026, 10, 20, 12, 1, tzinfo=UTC)
    )
    assert received.specification_digest == vector["expected_specification_digest"]
    assert received.input_set_digest == vector["expected_input_set_digest"]
    assert received.sources[0].repository_id != received.harness.repository_id


@pytest.mark.parametrize("change", ["missing", "extra", "revision", "check", "duplicate"])
def test_gateway_receipt_selector_rejects_wrong_current_inputs(change):
    from src.orchestration.evaluation_contract import verify_multi_repository_receipt

    vector = golden()
    sources = deepcopy(vector["sources"])
    if change == "missing":
        sources.pop()
    elif change == "extra":
        sources.append({**sources[0], "address": "flow/epic/wave/extra"})
    elif change == "revision":
        sources[0]["merge_sha"] = "f" * 40
    elif change == "check":
        sources[0]["checks"][0]["app_id"] = 8
    else:
        sources.append(deepcopy(sources[0]))
    with pytest.raises(ValueError, match="evaluation_input_(set|digest)_mismatch"):
        verify_multi_repository_receipt(vector["specification"], vector["receipt"], sources, now=datetime(2026, 10, 20, 12, 1, tzinfo=UTC))


@pytest.mark.parametrize("document", ["specification", "receipt"])
def test_gateway_receipt_selector_rejects_legacy_document(document):
    from src.orchestration.evaluation_contract import verify_multi_repository_receipt

    vector = golden()
    vector[document]["evidence_schema"] = "orchestration-evaluation/v1"
    with pytest.raises(ValueError, match="evaluation_(contract|receipt)_version_mismatch"):
        verify_multi_repository_receipt(vector["specification"], vector["receipt"], vector["sources"], now=datetime(2026, 10, 20, 12, 1, tzinfo=UTC))


@pytest.fixture
def canonical_vector():
    vector = golden()
    required = {
        "name": "security",
        "app_id": 8,
        "workflow_path": ".github/workflows/security.yml",
        "workflow_source_revision": "4" * 40,
        "workflow_definition_revision": "5" * 40,
    }
    vector["specification"]["predecessors"][0]["required_checks"].append(required)
    vector["sources"][0]["checks"].append({**required, "head_sha": vector["sources"][0]["reviewed_head_sha"], "conclusion": "success"})
    vector["receipt"]["sources"] = deepcopy(vector["sources"])
    vector["specification"]["mode"] = vector["receipt"]["mode"] = "deployed"
    environment = evaluation("deployed")["environment"]
    vector["specification"]["environment"] = deepcopy(environment)
    vector["receipt"]["environment"] = deepcopy(environment)
    canonical_specification = deepcopy(vector["specification"])
    canonical_specification["harness"].pop("repository")
    specification_hash = hashlib.sha256(json.dumps(canonical_specification, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    canonical_inputs = {
        "evidence_schema": "multi-repository-evaluation/v1",
        "specification_digest": specification_hash,
        "sources": vector["sources"],
    }
    vector["receipt"]["specification_digest"] = specification_hash
    vector["receipt"]["input_set_digest"] = hashlib.sha256(json.dumps(canonical_inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return vector


def verify_vector(vector):
    from src.orchestration.evaluation_contract import verify_multi_repository_receipt

    return verify_multi_repository_receipt(
        vector["specification"], vector["receipt"], vector["sources"], now=datetime(2026, 10, 20, 12, 1, tzinfo=UTC)
    )


@pytest.mark.parametrize("location", ["specification", "receipt", "sources", "all"])
def test_documented_sets_are_order_independent(canonical_vector, location):
    vector = deepcopy(canonical_vector)
    if location in {"specification", "all"}:
        vector["specification"]["predecessors"][0]["required_checks"].reverse()
        vector["specification"]["predecessors"].reverse()
        vector["specification"]["environment"]["deployment_receipt_keys"].reverse()
    if location in {"receipt", "all"}:
        vector["receipt"]["sources"][0]["checks"].reverse()
        vector["receipt"]["sources"].reverse()
        vector["receipt"]["environment"]["deployment_receipt_keys"].reverse()
    if location in {"sources", "all"}:
        vector["sources"][0]["checks"].reverse()
        vector["sources"].reverse()
    before = deepcopy(vector)
    received = verify_vector(vector)
    assert received.input_set_digest == canonical_vector["receipt"]["input_set_digest"]
    assert received.specification_digest == canonical_vector["receipt"]["specification_digest"]
    assert vector == before


@pytest.mark.parametrize("location", ["specification", "receipt"])
def test_harness_display_name_does_not_change_identity(canonical_vector, location):
    canonical_vector[location]["harness"]["repository"] = "renamed/harness"
    verify_vector(canonical_vector)


@pytest.mark.parametrize("location", ["specification", "receipt", "sources"])
@pytest.mark.parametrize(
    "change", ["missing", "extra", "duplicate", "app_id", "workflow_path", "workflow_source_revision", "workflow_definition_revision"]
)
def test_check_canonicalization_preserves_exact_identity_and_pins(canonical_vector, location, change):
    if location == "specification":
        checks = canonical_vector[location]["predecessors"][0]["required_checks"]
    else:
        sources = canonical_vector["sources"] if location == "sources" else canonical_vector["receipt"]["sources"]
        checks = sources[0]["checks"]
    if change == "missing":
        checks.pop()
    elif change == "extra":
        checks.append({**checks[-1], "name": "extra"})
    elif change == "duplicate":
        checks.append(deepcopy(checks[-1]))
    else:
        checks[-1][change] = 9 if change == "app_id" else ".github/workflows/other.yml" if change == "workflow_path" else "f" * 40
    with pytest.raises(ValueError):
        verify_vector(canonical_vector)


@pytest.mark.parametrize("location", ["specification", "receipt"])
@pytest.mark.parametrize(
    "change", ["missing", "extra", "duplicate", "connection_id", "target_contract_id", "target_contract_hash", "runtime_contract"]
)
def test_environment_canonicalization_preserves_exact_identity(canonical_vector, location, change):
    environment = canonical_vector[location]["environment"]
    keys = environment["deployment_receipt_keys"]
    if change == "missing":
        keys.pop()
    elif change == "extra":
        keys.append("extra")
    elif change == "duplicate":
        keys.append(keys[0])
    else:
        environment[change] = "f" * 64 if change == "target_contract_hash" else "changed"
    with pytest.raises(ValueError):
        verify_vector(canonical_vector)


@pytest.mark.parametrize(
    "field", ["repository_id", "installation_id", "source_revision", "definition_revision", "harness_digest", "adapter", "operation"]
)
def test_harness_canonicalization_preserves_receipt_pins(canonical_vector, field):
    canonical_vector["receipt"]["harness"][field] = (
        404 if field.endswith("_id") else "f" * 64 if field == "harness_digest" else "f" * 40 if field.endswith("revision") else "changed"
    )
    with pytest.raises(ValueError, match="evaluation_harness_or_target_mismatch"):
        verify_vector(canonical_vector)


def test_input_digest_rejects_duplicate_check_identities(canonical_vector):
    model = contract()
    specification = model.EvaluationSpecification.model_validate(canonical_vector["specification"])
    for changed_pin in (False, True):
        document = deepcopy(canonical_vector["receipt"])
        checks = document["sources"][0]["checks"]
        duplicate = deepcopy(checks[-1])
        if changed_pin:
            duplicate["workflow_source_revision"] = "f" * 40
        checks.append(duplicate)
        with pytest.raises(ValueError):
            receipt = model.EvaluationReceipt.model_validate(document)
            model.input_set_digest(specification, receipt.sources)
