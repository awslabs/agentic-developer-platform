"""Offline qualification tests; these do not contact AWS."""

import gzip
import hashlib
from pathlib import Path

import botocore
import botocore.loaders


ROOT = Path(__file__).resolve().parents[4]
QUALIFICATION = ROOT / "docs/l5-services/well-architected/qualification.md"
MODEL_HASH = "bcba9531f89a168c5fa4f725bea65f8fab67d021f5aa201d6c89b6e9e814fceb"


def test_ac_01():
    text = QUALIFICATION.read_text()
    assert "revision 1" in text
    assert MODEL_HASH in text
    assert "1.43.120" in text
    assert "AccessDenied" in text and "denied/unknown eligibility" in text
    assert "SCN-01 remains" in text and "manual/not-run" in text

    model = botocore.loaders.create_loader().load_service_model("wellarchitected", "service-2")
    operations = model["operations"]
    shapes = model["shapes"]
    start = shapes[operations["StartAgentRecommendationGeneration"]["input"]["shape"]]
    create = shapes[operations["CreateAgentProfile"]["input"]["shape"]]
    profile = shapes[operations["GetAgentProfile"]["output"]["shape"]]
    assert set(start["members"]) == {"profileArn", "types", "name", "additionalContext", "scope"}
    assert "clientToken" in create["members"]
    assert "StopAgentRecommendationGeneration" not in operations
    assert shapes[start["members"]["additionalContext"]["shape"]]["document"] is True
    assert {"eligibleForArchitectureGeneration", "eligibleForScheduledGeneration", "fieldErrors"} <= set(profile["members"])
    assert set(shapes["Pillar"]["enum"]) == {
        "COST_OPTIMIZATION", "SECURITY", "RESILIENCE", "PERFORMANCE", "OPERATIONAL_EXCELLENCE"
    }

    if botocore.__version__ == "1.43.120":
        path = Path(botocore.__file__).parent / "data/wellarchitected/2020-03-31/service-2.json.gz"
        assert hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest() == MODEL_HASH


def test_ac_02():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock

    from l5_services.adapters.well_architected.generation import (
        PreparedGeneration, cancel_generation, reconcile_generation, start_generation,
    )

    now = datetime(2026, 10, 27, tzinfo=UTC)
    prepared = PreparedGeneration(
        tenant_id="tenant-example", project_id="project-example",
        profile_arn="arn:aws:wellarchitected:example-region-1:000000000000:agent-profile/example",
        name="wa-generation-1", creator="approved-operator", model_hash=MODEL_HASH,
        request_hash="prepared-request-hash", created_after=now,
        created_before=now + timedelta(minutes=1),
        scope={"pillars": ["SECURITY"]}, additional_context={"source": "pinned-example"},
    )
    detail = {"id": "generation-1", "profileArn": prepared.profile_arn,
              "name": prepared.name, "createdBy": prepared.creator, "createdAt": now,
              "scope": prepared.scope, "additionalContext": prepared.additional_context}
    client = Mock()
    client.meta = SimpleNamespace(config=SimpleNamespace(retries={"total_max_attempts": 1}))
    client.start_agent_recommendation_generation.return_value = detail
    accepted = start_generation(client, prepared)
    assert accepted.classification == "accepted"
    assert accepted.native_job_ref["generation_id"] == "generation-1"
    assert client.start_agent_recommendation_generation.call_args.kwargs == {
        "profileArn": prepared.profile_arn, "types": ["ARCHITECTURE"],
        "name": prepared.name, "additionalContext": prepared.additional_context,
        "scope": prepared.scope,
    }
    assert "clientToken" not in client.start_agent_recommendation_generation.call_args.kwargs

    client.list_agent_recommendation_generations.side_effect = [
        {"items": [], "nextToken": "page-2"},
        {"items": [detail], "nextToken": None},
    ]
    client.get_agent_recommendation_generation.return_value = detail
    assert reconcile_generation(client, prepared).native_job_ref == accepted.native_job_ref
    assert client.list_agent_recommendation_generations.call_count == 2
    assert client.get_agent_recommendation_generation.call_count == 1
    assert client.start_agent_recommendation_generation.call_count == 1

    client.reset_mock(side_effect=True)
    client.list_agent_recommendation_generations.return_value = {"items": []}
    assert reconcile_generation(client, prepared).classification == "outcome_unknown"
    client.list_agent_recommendation_generations.return_value = {"items": [detail, detail]}
    assert reconcile_generation(client, prepared).classification == "outcome_unknown"
    assert client.get_agent_recommendation_generation.call_count == 0
    assert client.start_agent_recommendation_generation.call_count == 0

    client.list_agent_recommendation_generations.return_value = {"items": [detail]}
    client.get_agent_recommendation_generation.return_value = detail | {"scope": {"pillars": ["RESILIENCE"]}}
    assert reconcile_generation(client, prepared).classification == "outcome_unknown"
    client.list_agent_recommendation_generations.return_value = {"items": [], "nextToken": "page-2"}
    assert reconcile_generation(client, prepared, max_pages=1).classification == "outcome_unknown"
    assert client.start_agent_recommendation_generation.call_count == 0
    assert cancel_generation().classification == "cancel_unsupported"
    client.stop_agent_recommendation_generation.assert_not_called()


def test_ac_02_lost_start_and_local_expiry():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock

    import pytest

    from l5_services.adapters.well_architected.generation import (
        PreparedGeneration, cancel_generation, reconcile_generation, start_generation,
    )

    now = datetime(2026, 10, 27, tzinfo=UTC)
    prepared = PreparedGeneration(
        tenant_id="tenant-example", project_id="project-example", profile_arn="profile-example",
        name="generated-name", creator="operator-example", model_hash=MODEL_HASH,
        request_hash="prepared-request-hash", created_after=now,
        created_before=now + timedelta(seconds=30),
        scope={"pillars": ["SECURITY"]}, additional_context={"source": "immutable"},
    )
    client = Mock()
    client.meta = SimpleNamespace(config=SimpleNamespace(retries={"total_max_attempts": 1}))
    client.start_agent_recommendation_generation.side_effect = TimeoutError("lost reply")
    with pytest.raises(TimeoutError):
        start_generation(client, prepared)
    assert client.start_agent_recommendation_generation.call_count == 1

    native = {"id": "generation-example", "profileArn": prepared.profile_arn,
              "name": prepared.name, "createdBy": prepared.creator, "createdAt": now,
              "scope": prepared.scope, "additionalContext": prepared.additional_context}
    client.list_agent_recommendation_generations.side_effect = [
        {"items": []}, {"items": [native]},
    ]
    assert reconcile_generation(client, prepared).classification == "outcome_unknown"
    client.get_agent_recommendation_generation.return_value = native
    assert reconcile_generation(client, prepared).native_job_ref["generation_id"] == "generation-example"
    assert client.start_agent_recommendation_generation.call_count == 1
    assert client.list_agent_recommendation_generations.call_count == 2
    assert client.get_agent_recommendation_generation.call_count == 1
    assert cancel_generation().observation is None
    client.stop_agent_recommendation_generation.assert_not_called()

    client.meta.config.retries = {"total_max_attempts": 3}
    with pytest.raises(ValueError, match="disabled SDK retries"):
        start_generation(client, prepared)
    assert client.start_agent_recommendation_generation.call_count == 1


def test_ac_02_reject_incomplete_readback():
    from datetime import UTC, datetime, timedelta
    from unittest.mock import Mock

    from botocore.exceptions import ClientError

    from l5_services.adapters.well_architected.generation import PreparedGeneration, reconcile_generation

    now = datetime(2026, 10, 27, tzinfo=UTC)
    prepared = PreparedGeneration(
        tenant_id="tenant-example", project_id="project-example", profile_arn="profile-example",
        name="generated-name", creator="operator-example", model_hash=MODEL_HASH,
        request_hash="prepared-request-hash", created_after=now,
        created_before=now + timedelta(seconds=30),
        scope={"pillars": ["SECURITY"]}, additional_context={"source": "immutable"},
    )
    item = {"id": "generation-example", "profileArn": prepared.profile_arn,
            "name": prepared.name, "createdBy": prepared.creator, "createdAt": now}
    client = Mock()
    client.list_agent_recommendation_generations.return_value = {"items": [item]}
    client.get_agent_recommendation_generation.return_value = item
    assert reconcile_generation(client, prepared).classification == "outcome_unknown"
    client.get_agent_recommendation_generation.return_value = item | {
        "scope": prepared.scope, "additionalContext": prepared.additional_context,
        "createdAt": now + timedelta(hours=1),
    }
    assert reconcile_generation(client, prepared).classification == "outcome_unknown"
    client.list_agent_recommendation_generations.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "ListAgentRecommendationGenerations"
    )
    assert reconcile_generation(client, prepared).classification == "outcome_unknown"
    client.start_agent_recommendation_generation.assert_not_called()


def test_ac_03():
    import json

    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / "docs/l5-services/contracts/v1/schemas.json").read_text())

    def valid(name, value):
        return Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"}).is_valid(value)

    source = {"tenant_id": "tenant-example", "project_id": "project-example",
              "source_connection_id": "connection-example", "record_type": "repository_commit",
              "record_id": "repository-example", "record_revision": "a" * 40}
    document = source | {"record_type": "document", "record_id": "architecture-example",
                         "record_revision": "document-v1"}
    run = {"service_id": "well_architected", "capability": "architecture_review",
           "configuration_revision": 1, "binding_id": "binding-example", "sources": [source, document]}
    settings = {"profile_ref": source | {"record_type": "profile"},
                "source_refs": [source, document], "capabilities": ["architecture_review"],
                "enabled": False, "visibility": "product_team", "exclusions": [], "overrides": [],
                "triggers": [], "service_id": "well_architected",
                "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY", "RESILIENCE"]}}
    assert valid("RunInput", run)
    assert valid("ConfigurationInput", settings)
    assert valid("Service", {"service_id": "well_architected", "availability": "unavailable",
                             "capabilities": ["architecture_review"], "contract_version": "1.0"})
    assert valid("Options", {"service_id": "well_architected", "connections": [],
                             "sources": [document, source], "regions": [],
                             "capabilities": ["architecture_review"], "unavailable_reasons": []})
    assert valid("RunInput", {key: value for key, value in run.items() if key != "service_id"}
                 | {"capability": "threat_model", "sources": [document]})
    assert valid("RunInput", {"service_id": "devops", "configuration_revision": 1,
                              "binding_id": "binding-example", "capability": "investigation",
                              "devops_input": {"kind": "investigation", "resource_refs": [source],
                                               "telemetry_refs": [], "question": "What changed?"}})
    assert not valid("RunInput", run | {"sources": [document, source | {"record_revision": "HEAD"}]})
    assert not valid("RunInput", run | {"sources": [source | {"record_type": "resource"}]})
    assert not valid("RunInput", run | {"capability": "release_verification"})
    assert not valid("RunInput", run | {"clientToken": "override"})
    assert not valid("ConfigurationInput", settings | {"well_architected": {
        "review_mode": "resource_inspection", "pillars": ["SECURITY"]}})
    assert not valid("ConfigurationInput", settings | {"well_architected": {
        "review_mode": "source_only", "pillars": ["SUSTAINABILITY"]}})


def test_ac_03_shared_l5_validation():
    from fastapi import HTTPException
    import pytest

    from l5_services.configuration_population import configuration_options, populate_settings
    from l5_services.configuration_readiness import assess_readiness
    from l5_services.run_input import validate_run_input

    def reference(kind, identifier, revision):
        return {"tenant_id": "tenant-example", "project_id": "project-example",
                "source_connection_id": "connection-example", "record_type": kind,
                "record_id": identifier, "record_revision": revision}

    document = reference("document", "architecture-example", "doc-v1")
    repository = reference("repository_commit", "repository-example", "a" * 40)
    profile_ref = reference("product", "product-example", "1")
    profile = {"tenant_id": "tenant-example", "project_id": "project-example",
               "product_id": "product-example", "revision": 1,
               "configuration": {"basic": {"documents": [document], "repositories": [repository]}}}
    available = {"coverage": "complete", "next_cursor": None, "items": [
        {"resources": [document, repository]},
    ]}
    choices = {"service_id": "well_architected", "capabilities": ["architecture_review"],
               "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]}}
    settings, inherited = populate_settings(profile, profile_ref, available, choices, "well_architected")
    assert settings["source_refs"] == [repository, document]
    assert "source_refs" in inherited
    assert settings["well_architected"] == choices["well_architected"]
    assert assess_readiness(settings, "configuration-example", [], delegation_active=True,
                            service_id="well_architected")[:2] == (
                                "disabled", ["catalog_mapping_unresolved", "provider_unqualified"])
    assert configuration_options(profile, available, "well_architected")["capabilities"] == []
    assert assess_readiness(settings | {"enabled": True}, "configuration-example", [],
                            delegation_active=True, service_id="well_architected")[0] == "needs_input"

    run = {"service_id": "well_architected", "capability": "architecture_review",
           "binding_id": "binding-example", "configuration_revision": 1,
           "sources": [repository, document]}
    validate_run_input(run)
    for invalid in (
        run | {"sources": [reference("resource", "unrequested", "1")]},
        run | {"sources": [document | {"record_revision": "HEAD"}]},
        run | {"sources": [document | {"untrusted": "value"}]},
        run | {"capability": "code_review"}, run | {"clientToken": "untrusted"},
        run | {"service_id": {}}, run | {"service_id": []},
        run | {"parent_task_ref": None},
        run | {"executionRoleArn": "untrusted"}, run | {"pr_base": "main"},
    ):
        with pytest.raises(HTTPException) as error:
            validate_run_input(invalid)
        assert error.value.status_code == 422
    for invalid in (
        choices | {"well_architected": {"review_mode": "resource_inspection", "pillars": ["SECURITY"]}},
        choices | {"well_architected": {"review_mode": "source_only", "pillars": ["SUSTAINABILITY"]}},
        choices | {"well_architected": {"review_mode": "source_only", "pillars": ["SECURITY", "SECURITY"]}},
        choices | {"well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"],
                                          "execution_role_ref": reference("role", "unlisted", "1")}},
        choices | {"well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"],
                                          "execution_role_ref": None}},
        choices | {"authority": "untrusted"},
        choices | {"well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"],
                                          "goal_refs": [document | {"tenant_id": "foreign"}]}},
    ):
        with pytest.raises(ValueError):
            populate_settings(profile, profile_ref, available, invalid, "well_architected")
    with pytest.raises(ValueError):
        populate_settings(profile, profile_ref, available, choices | {"devops": {}}, "well_architected")


def test_ac_03_all_service_variants_remain_disjoint():
    import json

    import pytest
    from fastapi import HTTPException
    from jsonschema import Draft202012Validator

    from l5_services.run_input import validate_run_input

    contract = ROOT / "docs/l5-services/contracts/v1"
    schema = json.loads((contract / "schemas.json").read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/RunInput"})
    examples = json.loads((contract / "examples.json").read_text())
    valid_runs = [example["value"] for example in examples
                  if example["schema"] == "RunInput" and example["valid"]]
    service_ids = ("security", "devops", "finops", "well_architected")
    assert {run.get("service_id", "security") for run in valid_runs} == set(service_ids)
    repository = next(run["sources"][0] for run in valid_runs if run["capability"] == "code_review")
    valid_runs.append({
        "service_id": "devops", "capability": "release_readiness",
        "configuration_revision": 1, "binding_id": "binding-example",
        "devops_input": {"kind": "release_readiness", "repository_ref": repository,
                         "base_revision": "b" * 40, "head_revision": repository["record_revision"]},
    })
    for run in valid_runs:
        original_service = run.get("service_id", "security")
        assert validator.is_valid(run)
        validate_run_input(run)
        for service_id in (*service_ids, {}, []):
            if service_id == original_service:
                continue
            invalid = run | {"service_id": service_id}
            assert not validator.is_valid(invalid)
            with pytest.raises(HTTPException) as error:
                validate_run_input(invalid)
            assert error.value.status_code == 422


def test_ac_03_operation_parity_and_catalog_block():
    import csv
    import json

    contract = ROOT / "docs/l5-services/contracts/v1"
    with (contract / "operations.tsv").open(newline="") as source:
        matrix = list(csv.DictReader(source, delimiter="\t"))
    paths = json.loads((contract / "openapi.json").read_text())["paths"]
    http_operations = {specification["operationId"]: specification
                       for path in paths.values() for specification in path.values()
                       if isinstance(specification, dict) and "operationId" in specification}
    tools = json.loads((contract / "mcp-tools.json").read_text())
    tool_operations = {tool["x-http-operation"]: tool for tool in tools}
    names = {entry["operation_id"] for entry in matrix}
    assert len(names) == len(matrix) == 34
    assert names == set(http_operations)
    assert names - {"launchNativeBrowser"} == set(tool_operations)
    for entry in matrix:
        operation = http_operations[entry["operation_id"]]
        if entry["input"] != "-":
            assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"] \
                == f'./schemas.json#/$defs/{entry["input"]}'
        if entry["output"] != "binary":
            response = operation["responses"]["202" if entry["effect"] in {"mutate", "binding"} else "200"]
            assert response["content"]["application/json"]["schema"]["$ref"] \
                == f'./schemas.json#/$defs/{entry["output"]}'
        if entry["MCP"] != "-":
            assert tool_operations[entry["operation_id"]]["name"] == entry["MCP"]
    for operation_id in ("saveConfiguration", "submitRun", "getDevopsPrevention",
                         "configureDevopsPrevention"):
        assert operation_id in http_operations and operation_id in tool_operations

    text = QUALIFICATION.read_text()
    assert "no authoritative Module or Feature ID mapping was verified" in text
    assert "unmapped" in text and "before dependent dispatch" in text
    assert "PranavSharma1000" in text and "#8683" in text


def test_scn_01_manual_procedure_link_is_not_native_evidence():
    backlog = (ROOT / "docs/l5-services/well-architected/backlog.md").read_text()
    row = next(line for line in backlog.splitlines() if line.startswith("| SCN-01 | AC-01 |"))
    assert "Controls: C1" in row and "| manual |" in row
    assert "qualification.md#manual-scn-01-evidence-and-disposition" in row
    assert "manual/not-run: private fixtures required" in row
    qualification = QUALIFICATION.read_text()
    assert "## Model and documentation baseline" in qualification
    assert "## Manual SCN-01 evidence and disposition" in qualification
    assert "No AWS calls, resource creation" in qualification
    assert "eligible/ineligible fixtures" in qualification
    assert "SCN-01 remains" in qualification and "manual/not-run" in qualification


def test_scn_02():
    import json
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock

    import pytest

    from l5_services.adapters.well_architected.generation import (
        PreparedGeneration, cancel_generation, reconcile_generation, start_generation,
    )

    fixture = json.loads((Path(__file__).parent / "fixtures/scn_02.json").read_text())
    assert fixture["scenario"] == "SCN-02"
    assert fixture["fixture_kind"] == "scripted_provider_not_live"
    assert fixture["jitter_seconds"] == 0
    now = datetime.fromisoformat(fixture["clock"].replace("Z", "+00:00"))
    assert now.tzinfo == UTC
    prepared = PreparedGeneration(
        tenant_id="tenant-example", project_id="project-example", profile_arn="profile-example",
        name="pinned-generation-name", creator="operator-example", model_hash=MODEL_HASH,
        request_hash="synthetic-request-hash", created_after=now,
        created_before=now + timedelta(seconds=30), scope={"pillars": ["SECURITY"]},
        additional_context={"synthetic_example": "not_a_qualified_wire_format"},
    )
    for case in fixture["cases"]:
        client = Mock()
        client.meta = SimpleNamespace(config=SimpleNamespace(retries={"total_max_attempts": 1}))
        client.start_agent_recommendation_generation.side_effect = TimeoutError("lost response")
        with pytest.raises(TimeoutError):
            start_generation(client, prepared)
        assert client.start_agent_recommendation_generation.call_count == 1, case["id"]
        assert set(client.start_agent_recommendation_generation.call_args.kwargs) == {
            "profileArn", "types", "name", "scope", "additionalContext",
        }, case["id"]
        assert client.start_agent_recommendation_generation.call_args.kwargs["types"] == ["ARCHITECTURE"]
        identity = {"profileArn": prepared.profile_arn, "name": prepared.name,
                    "createdBy": prepared.creator, "createdAt": now}
        detail = {**identity, "id": "generation-example", "scope": prepared.scope,
                  "additionalContext": prepared.additional_context}
        if case["rounds"][-1].get("detail") == "mismatched_scope":
            detail["scope"] = {"pillars": ["RESILIENCE"]}
        client.get_agent_recommendation_generation.return_value = detail
        for round_case in case["rounds"]:
            client.list_agent_recommendation_generations.side_effect = [
                {"items": [identity | {"id": generation_id} for generation_id in page["ids"]],
                 "nextToken": page.get("nextToken")}
                for page in round_case["pages"]
            ]
            outcome = reconcile_generation(client, prepared, max_pages=round_case.get("max_pages", 20))
            assert outcome.classification == round_case["expected"], case["id"]
            if outcome.classification == "accepted":
                assert outcome.native_job_ref == {
                    "tenant_id": prepared.tenant_id, "project_id": prepared.project_id,
                    "profile_arn": prepared.profile_arn, "generation_id": "generation-example",
                }
            else:
                assert outcome.native_job_ref is None
        assert client.list_agent_recommendation_generations.call_count == case["expected_list_calls"], case["id"]
        assert client.get_agent_recommendation_generation.call_count == case["expected_get_calls"], case["id"]
        assert client.start_agent_recommendation_generation.call_count == 1, case["id"]
        assert cancel_generation().classification == "cancel_unsupported"
        client.stop_agent_recommendation_generation.assert_not_called()

    row = next(line for line in (ROOT / "docs/l5-services/well-architected/backlog.md").read_text().splitlines()
               if line.startswith("| SCN-02 | AC-02 |"))
    assert "test_wa01.py::test_scn_02" in row and "fixtures/scn_02.json" in row
    assert "installed-worker integration pending WA04/WA07" in row


def test_scn_03():
    from copy import deepcopy
    from datetime import datetime
    import json

    from fastapi import HTTPException
    from jsonschema import Draft202012Validator
    import pytest

    from l5_services.configuration_population import populate_settings
    from l5_services.configuration_readiness import assess_readiness
    from l5_services.run_input import validate_run_input

    fixture = json.loads((Path(__file__).parent / "fixtures/scn_03.json").read_text())
    assert fixture["scenario"] == "SCN-03"
    assert fixture["fixture_kind"] == "offline_contract_not_live_client"
    assert datetime.fromisoformat(fixture["fixed_clock"].replace("Z", "+00:00")).tzinfo is not None
    schema = json.loads((ROOT / "docs/l5-services/contracts/v1/schemas.json").read_text())
    tools = {tool["x-http-operation"]: tool for tool in json.loads(
        (ROOT / "docs/l5-services/contracts/v1/mcp-tools.json").read_text())}
    validator = {name: Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"})
                 for name in ("RunInput", "ConfigurationInput")}

    def source(product, kind, revision):
        return {"tenant_id": product["tenant_id"], "project_id": product["project_id"],
                "source_connection_id": "connection-example", "record_type": kind,
                "record_id": kind + "-example", "record_revision": revision}

    assert len(fixture["products"]) == 2
    assert len({(item["tenant_id"], item["project_id"]) for item in fixture["products"]}) == 2
    for product in fixture["products"]:
        repository = source(product, "repository_commit", product["repository_revision"])
        document = source(product, "document", product["document_revision"])
        references = [repository, document]
        run = {"service_id": "well_architected", "capability": "architecture_review",
               "binding_id": "binding-example", "configuration_revision": 1, "sources": references}
        profile_ref = source(product, "product", "1") | {"record_id": product["product_id"]}
        profile = {"tenant_id": product["tenant_id"], "project_id": product["project_id"],
                   "product_id": product["product_id"], "revision": 1,
                   "configuration": {"basic": {"repositories": [repository], "documents": [document]}}}
        available = {"coverage": "complete", "next_cursor": None, "items": [{"resources": references}]}
        choices = {"service_id": "well_architected", "capabilities": ["architecture_review"],
                   "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY", "RESILIENCE"]}}
        settings, _ = populate_settings(profile, profile_ref, available, choices, "well_architected")
        assert validator["ConfigurationInput"].is_valid(settings)
        assert validator["RunInput"].is_valid(run)
        validate_run_input(run)
        assert assess_readiness(settings, "configuration-example", [], delegation_active=True,
                                service_id="well_architected")[0] == "disabled"
        for operation_id, body in (("saveConfiguration", settings), ("submitRun", run)):
            request = {"product_id": product["product_id"], "project_id": product["project_id"],
                       "service_id": "well_architected", "input": body,
                       "idempotency_key": "fictional-request-1234"}
            assert Draft202012Validator(tools[operation_id]["inputSchema"]).is_valid(request)

        for case in fixture["negative_cases"]:
            mutated = deepcopy(settings if case["target"] == "configuration" else run)
            target = mutated
            path = case["field"].split(".")
            for key in path[:-1]:
                target = target[int(key)] if isinstance(target, list) else target[key]
            target[path[-1]] = case["value"]
            assert not validator["ConfigurationInput" if case["target"] == "configuration"
                                 else "RunInput"].is_valid(mutated), case["id"]
            if case["target"] == "run":
                with pytest.raises(HTTPException) as error:
                    validate_run_input(mutated)
                assert error.value.status_code == 422, case["id"]
            else:
                choice = deepcopy(choices)
                target_choice = choice
                for key in path[:-1]:
                    target_choice = target_choice[int(key)] if isinstance(target_choice, list) else target_choice[key]
                target_choice[path[-1]] = case["value"]
                with pytest.raises(ValueError, match=(
                    "unsupported configuration choice" if case["id"] == "authority_override"
                    else "invalid source-only review settings"
                )):
                    populate_settings(profile, profile_ref, available, choice, "well_architected")

        foreign = fixture["products"][0 if product == fixture["products"][1] else 1]
        foreign_document = source(foreign, "document", foreign["document_revision"])
        with pytest.raises(ValueError):
            populate_settings(profile, profile_ref, available, choices | {
                "well_architected": choices["well_architected"] | {"goal_refs": [foreign_document]},
            }, "well_architected")

    row = next(line for line in (ROOT / "docs/l5-services/well-architected/backlog.md").read_text().splitlines()
               if line.startswith("| SCN-03 | AC-03 |"))
    assert "test_wa01.py::test_scn_03" in row and "fixtures/scn_03.json" in row
    assert "installed clients pending WA06/WA07" in row
