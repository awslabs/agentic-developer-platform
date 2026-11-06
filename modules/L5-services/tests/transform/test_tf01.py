"""Independent Transform contract and qualification regression fixtures."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from l5_services.adapters.transform.qualification import (
    NativeReadback, ProviderQualification, reconcile_attempt, remote_cancel_verdict,
)
from l5_services.api.router import create_router
from l5_services.configuration_population import finops_configuration_options
from l5_services.run_input import validate_run_input
from l5_services.transform_contract import TransformConfigurationInput, TransformResultDetail
from l5_services.workers.execution import ExecutionService
from l5_services.work_store import AttemptContext


SOURCE = {
    "tenant_id": "example-team", "project_id": "example-project",
    "source_connection_id": "example-connection", "record_type": "repository_commit",
    "record_id": "example/widget", "record_revision": "a" * 40,
}
GOAL = {"kind": "dependency_upgrade", "summary": "Upgrade the library",
        "target": {"kind": "dependency", "name": "sample-library", "version": "2.1.0"}}
RUN = {"service_id": "transform", "configuration_revision": 3,
       "binding_id": "example-binding", "capability": "dependency_upgrade",
       "sources": [SOURCE], "transform_input": {"goal": GOAL}}
SETTINGS = {"connection_ref": SOURCE, "executor_binding_ref": SOURCE,
            "test_policy_ref": SOURCE, "region": "example-region",
            "read_paths": ["src"], "write_paths": ["src/app.py"],
            "excluded_paths": [".git"], "goal_defaults": GOAL}
CONFIGURATION = {"service_id": "transform", "profile_ref": SOURCE,
                 "source_refs": [SOURCE], "capabilities": ["dependency_upgrade"],
                 "enabled": False, "transform": SETTINGS}
RESULT = {"service_id": "transform", "original_patch_ref": SOURCE,
          "baseline_checks": [{"name": "unit", "status": "passed"}],
          "candidate_checks": [{"name": "unit", "status": "passed"}],
          "source_ref": SOURCE, "limitations": [], "provider_state": "completed",
          "cost_status": "unknown"}
CONTRACTS = Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1"
SCOPE = {"tenant_id": "example-team", "product_id": "example-product",
         "project_id": "example-project", "relationship_revision": "1",
         "owner_id": "example-owner", "visibility": "only_me"}


def contract_accepts(name, value):
    definitions = json.loads((CONTRACTS / "schemas.json").read_text())
    return Draft202012Validator({**definitions, "$ref": "#/$defs/" + name}).is_valid(value)


class TransformContractTests(unittest.TestCase):
    def test_contract_compatibility(self):
        validate_run_input(RUN)
        self.assertEqual(TransformConfigurationInput.model_validate(CONFIGURATION).model_dump(
            exclude_none=True), CONFIGURATION)
        self.assertEqual(TransformResultDetail.model_validate(RESULT).model_dump(
            exclude_none=True), RESULT)
        legacy = {"configuration_revision": 1, "binding_id": "example-binding",
                  "capability": "code_review", "sources": [SOURCE]}
        validate_run_input(legacy)
        devops = {"service_id": "devops", "configuration_revision": 1,
                  "binding_id": "example-binding", "capability": "investigation",
                  "devops_input": {"kind": "investigation", "resource_refs": [SOURCE],
                                   "telemetry_refs": [], "question": "What changed?"}}
        validate_run_input(devops)
        envelope = {"service_id": "transform", "result_id": "result-example",
                    "run_id": "run-example", "scope": SCOPE, "revision": 1,
                    "observed_at": "2026-01-01T00:00:00Z", "transform_result": RESULT}
        for name, value in (("RunInput", RUN), ("ConfigurationInput", CONFIGURATION),
                            ("Result", envelope), ("RunInput", legacy), ("RunInput", devops)):
            with self.subTest(name=name):
                self.assertTrue(contract_accepts(name, value))
        self.assertIn("submitRun\tPOST\t", (CONTRACTS / "operations.tsv").read_text())
        self.assertIn("getResult\tGET\t", (CONTRACTS / "operations.tsv").read_text())

        tools = {tool["x-http-operation"]: tool for tool in
                 json.loads((CONTRACTS / "mcp-tools.json").read_text())}
        specification = json.loads((CONTRACTS / "openapi.json").read_text())
        run_path = "/v1/products/{product_id}/projects/{project_id}/services/{service_id}/runs"
        submit = specification["paths"][run_path]["post"]
        self.assertEqual(submit["operationId"], "submitRun")
        self.assertEqual(submit["requestBody"]["content"]["application/json"]["schema"]["$ref"],
                         "./schemas.json#/$defs/RunInput")
        for payload in (RUN, legacy, devops):
            request = {"product_id": "example-product", "project_id": "example-project",
                       "service_id": payload.get("service_id", "security"), "input": payload,
                       "idempotency_key": "pinned-example-run-01"}
            self.assertTrue(Draft202012Validator(tools["submitRun"]["inputSchema"]).is_valid(request))
            self.assertFalse(Draft202012Validator(tools["submitRun"]["inputSchema"]).is_valid(
                {**request, "input": {**payload, "service_id":
                    "devops" if request["service_id"] == "security" else "security"}}))
        result_tool = tools["getResult"]["outputSchema"]
        self.assertTrue(Draft202012Validator(result_tool).is_valid(
            {"http_status": 200, "data": envelope}))
        self.assertFalse(Draft202012Validator(result_tool).is_valid(
            {"http_status": 200, "data": {**envelope, "finding_count": 0}}))

        async def options(product_id, project_id, service_id):
            if service_id == "finops":
                return finops_configuration_options(
                    {"tenant_id": "example-team", "project_id": project_id}, {"items": []})
            return {"connections": [], "sources": [], "regions": [],
                    "capabilities": [], "unavailable_reasons": [], "service_id": service_id}

        backend = Mock(options=AsyncMock(side_effect=options))
        app = FastAPI()
        app.include_router(create_router(lambda: backend))
        client = TestClient(app)
        root = "/v1/products/example-product/projects/example-project/services"
        for service_id in ("security", "devops", "finops", "transform"):
            response = client.get(f"{root}/{service_id}/configuration/options")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(contract_accepts("Options", response.json()))
            self.assertEqual(response.json()["service_id"], service_id)
        self.assertEqual(client.get(f"{root}/unknown/configuration/options").status_code, 404)
        self.assertEqual([invocation.args for invocation in backend.options.await_args_list], [
            ("example-product", "example-project", "security"),
            ("example-product", "example-project", "devops"),
            ("example-product", "example-project", "finops"),
            ("example-product", "example-project", "transform")])

    def test_cross_service_and_extra_fields_are_rejected(self):
        invalid_runs = [
            {**RUN, "service_id": "security"}, {**RUN, "devops_input": {}},
            {**RUN, "sources": [SOURCE, SOURCE]},
            {**RUN, "sources": [{**SOURCE, "record_revision": "HEAD"}]},
            {**RUN, "sources": [{**SOURCE, "record_type": "document"}]},
            {**RUN, "transform_input": {"goal": {**GOAL, "kind": "refactor"}}},
            {**RUN, "transform_input": {"goal": {**GOAL, "target": None}}},
            {**RUN, "host_command": "rm -rf /"},
            {**RUN, "service_id": "unknown"},
            {**RUN, "service_id": []},
            {**RUN, "service_id": {}},
        ]
        for value in invalid_runs:
            with self.subTest(value=value), self.assertRaises(HTTPException) as rejection:
                validate_run_input(value)
            self.assertEqual(rejection.exception.status_code, 422)
        for value in [
            {**CONFIGURATION, "devops": {}},
            {**CONFIGURATION, "transform": {**SETTINGS, "write_paths": ["../secrets"]}},
            {**CONFIGURATION, "transform": {**SETTINGS, "write_paths": ["src/File", "src/file"]}},
            {**CONFIGURATION, "transform": {**SETTINGS, "write_paths": ["/host"]}},
            {**CONFIGURATION, "capabilities": ["code_review"]},
        ]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TransformConfigurationInput.model_validate(value)
        with self.assertRaises(ValidationError):
            TransformResultDetail.model_validate({**RESULT, "findings_artifact": SOURCE})
        with self.assertRaises(ValidationError):
            TransformResultDetail.model_validate({**RESULT, "provider_state": "unknown"})

    def test_contract_schema_rejects_foreign_fields(self):
        for name, value in (
            ("RunInput", {**RUN, "devops_input": {}}),
            ("RunInput", {**RUN, "service_id": "security"}),
            ("RunInput", {**RUN, "sources": [SOURCE, SOURCE]}),
            ("RunInput", {**RUN, "transform_input": {"goal": {**GOAL, "kind": "refactor"}}}),
            ("ConfigurationInput", {**CONFIGURATION, "devops": {}}),
            ("ConfigurationInput", {**CONFIGURATION, "capabilities": ["code_review"]}),
            ("Result", {"service_id": "transform", "result_id": "result-example",
                        "run_id": "run-example", "scope": SCOPE, "revision": 1,
                        "observed_at": "2026-01-01T00:00:00Z", "transform_result": RESULT,
                        "finding_count": 0}),
        ):
            with self.subTest(name=name, value=value):
                self.assertFalse(contract_accepts(name, value))

    def test_executor_boundary(self):
        backend = Mock(options=AsyncMock(), current=AsyncMock())
        app = FastAPI()
        app.include_router(create_router(lambda: backend))
        client = TestClient(app)
        root = "/v1/products/example-product/projects/example-project/services/transform"
        requests = (
            ("post", "/runs", {**RUN, "parent_task_ref": {**SOURCE, "record_type": "task_attempt"}}),
            ("post", "/runs", {**RUN, "sources": [{**SOURCE, "tenant_id": "foreign-team"}]}),
            ("post", "/runs", {**RUN, "sources": [{**SOURCE, "record_revision": "HEAD"}]}),
            ("post", "/runs", {**RUN, "sources": [{**SOURCE, "record_revision": "b" * 40}]}),
            ("post", "/runs", {**RUN, "provider_credentials": "untrusted"}),
            ("post", "/runs", {**RUN, "kubeconfig": "/host/config"}),
            ("post", "/runs", {**RUN, "image": "registry.example/check:latest"}),
            ("post", "/runs", {**RUN, "test_command": "sh -c 'cat /host/secret'"}),
            ("post", "/runs", {**RUN, "write_paths": ["../host"]}),
            ("post", "/runs", RUN),
            ("get", "/results/result-example", None),
        )
        for method, suffix, body in requests:
            with self.subTest(method=method, suffix=suffix, body=body):
                result = getattr(client, method)(root + suffix, json=body) if body is not None else getattr(client, method)(root + suffix)
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json()["detail"]["code"], "capability_unavailable")
        backend.assert_not_called()
        for method in ("submit", "result_store"):
            getattr(backend, method).assert_not_called()
        self.assertEqual(backend.current.await_count, len(requests))

    def test_worker_blocks_transform_before_attempt_or_source_access(self):
        for native_ref, provider_state in ((None, "not_started"), (SOURCE, "running")):
            with self.subTest(native_ref=native_ref):
                run = SimpleNamespace(service_id="transform", revision=1, provider_state=provider_state)
                claim = SimpleNamespace(tenant_id="example-team", run_id="run-example",
                                        run_revision=1, native_job_ref=native_ref, kind="submit")
                session = AsyncMock()
                session.scalar.return_value = None
                session.get.return_value = run
                context = MagicMock()
                context.__aenter__ = AsyncMock(return_value=session)
                context.__aexit__ = AsyncMock(return_value=None)
                sessions = Mock(return_value=context)
                source, adapter, authorize, publish = Mock(), Mock(), Mock(), Mock()
                worker = ExecutionService(sessions, source, adapter, publish, authorize)
                claim_work = AsyncMock(return_value=claim)
                finish_work = AsyncMock(return_value="completed")
                with patch("l5_services.workers.execution.claim_work", claim_work), \
                     patch("l5_services.workers.execution.finish_work", finish_work), \
                     patch("l5_services.work_store.prepare_submit_attempt") as prepare:
                    self.assertTrue(asyncio.run(worker.execute({
                        "schema_version": "1.0", "work_id": "work-example", "generation": 1})))
                self.assertEqual(finish_work.await_args.kwargs["state"], "blocked")
                self.assertEqual(finish_work.await_args.kwargs["provider_state"], provider_state)
                prepare.assert_not_called()
                source.assert_not_called()
                adapter.assert_not_called()
                authorize.assert_not_called()
                publish.assert_not_called()

    def test_provider_qualification(self):
        matrix = json.loads((CONTRACTS.parent.parent / "transform/qualification-v1.json").read_text())
        qualification = ProviderQualification.model_validate(matrix)
        self.assertEqual(qualification.schema_version, "1.0")
        self.assertFalse(qualification.ready_for_submission)
        self.assertEqual(qualification.cancel.status, "unsupported")
        self.assertEqual(qualification.tool_isolation.status, "unsupported")
        for operation in ("submit", "read", "resume"):
            self.assertEqual(getattr(qualification, operation).status, "unqualified")
        self.assertFalse(any(getattr(qualification, operation).receipt_sha256 for operation in
                             ("submit", "read", "resume", "cancel", "tool_isolation")))
        with self.assertRaises(ValidationError):
            ProviderQualification.model_validate({**matrix, "submit": {
                "status": "supported", "reason": "synthetic", "receipt_sha256": None}})
        with self.assertRaises(ValidationError):
            ProviderQualification.model_validate({**matrix, "read": {
                "status": "unqualified", "reason": "synthetic", "receipt_sha256": "a" * 64}})

        fixture = json.loads((Path(__file__).parent / "qualification-scenarios.json").read_text())
        self.assertEqual(fixture["schema_version"], qualification.schema_version)
        self.assertEqual(fixture["fixture_class"], "synthetic_not_provider_evidence")
        self.assertEqual(fixture["cleanup"], "none_created")
        self.assertEqual(fixture["tenant_id"], SOURCE["tenant_id"])
        self.assertEqual(hashlib.sha256(fixture["source_bytes"].encode()).hexdigest(),
                         fixture["source_sha256"])
        self.assertTrue(contract_accepts("Cost", {"kind": "unknown", "currency": "USD"}))
        self.assertFalse(contract_accepts("Cost", {"kind": "unknown", "currency": "USD", "amount": "0"}))

        backend = Mock(submit=AsyncMock(), current=AsyncMock())
        app = FastAPI()
        app.include_router(create_router(lambda: backend))
        response = TestClient(app).post(
            "/v1/products/example-product/projects/example-project/services/transform/runs",
            json=RUN, headers={"Idempotency-Key": "synthetic-scenario-01"})
        self.assertEqual(response.status_code, 503)
        backend.submit.assert_not_awaited()
        backend.current.assert_awaited_once()

        read_only = ProviderQualification.model_validate({**matrix,
            "interface": "synthetic-read-only", "cli_version": "test-only",
            "binary_sha256": "c" * 64, "image_digest": "sha256:" + "d" * 64,
            "definition_revision": "test-only", "read": {
                "status": "supported", "reason": "synthetic fixture only",
                "receipt_sha256": "e" * 64}})
        self.assertFalse(read_only.ready_for_submission)
        attempt = AttemptContext(attempt_id="attempt-example", run_id="run-example",
            work_id="first-work", fence=1, idempotency_token="a" * 64,
            input_hash=fixture["input_hash"], started_at=datetime.fromisoformat(
                fixture["clock_at"].replace("Z", "+00:00")), can_reconcile=True)
        restarted = replace(attempt, work_id="second-work", fence=2)
        self.assertEqual(restarted.attempt_id, attempt.attempt_id)
        self.assertEqual(restarted.idempotency_token, attempt.idempotency_token)
        for case in fixture["cases"]:
            with self.subTest(case=case["id"]):
                value = case["readback"]
                readback = None if value is None else NativeReadback(
                    native_id=value["native_id"], state=value["state"],
                    attempt_token="other" if value.get("wrong_token") else attempt.idempotency_token,
                    input_hash="other" if value.get("wrong_input_hash") else attempt.input_hash)
                selected = read_only if case["qualification"] == "synthetic_read_only" else qualification
                verdict = reconcile_attempt(restarted, selected, readback)
                self.assertEqual(verdict.status, case["expected_status"])
                self.assertFalse(verdict.replacement_submit_allowed)
                self.assertFalse(remote_cancel_verdict(selected, verdict,
                    cancel_receipt_native_id=readback.native_id if readback else None))


    def test_provider_loss_restart_and_cancel_do_not_claim_success(self):
        matrix = ProviderQualification.model_validate(json.loads(
            (CONTRACTS.parent.parent / "transform/qualification-v1.json").read_text()))
        attempt = AttemptContext(attempt_id="attempt-example", run_id="run-example",
                                 work_id="work-example", fence=1,
                                 idempotency_token="a" * 64, input_hash="b" * 64,
                                 started_at=datetime(2026, 1, 1, tzinfo=UTC), can_reconcile=True)
        receipt = NativeReadback(native_id="native-example", attempt_token="a" * 64,
                                 input_hash="b" * 64, state="stopped")
        first = reconcile_attempt(attempt, matrix, None)
        self.assertEqual((first.status, first.provider_state), ("outcome_unknown", "unknown"))
        self.assertFalse(first.replacement_submit_allowed)
        restarted = replace(attempt, work_id="restarted-work", fence=2)
        self.assertEqual(reconcile_attempt(restarted, matrix, receipt).status, "outcome_unknown")
        self.assertFalse(remote_cancel_verdict(matrix, first, cancel_receipt_native_id=receipt.native_id))
        pinned = matrix.model_copy(update={"interface": "synthetic", "cli_version": "test-only",
            "binary_sha256": "c" * 64, "image_digest": "sha256:" + "d" * 64,
            "definition_revision": "test-only", "read": matrix.read.model_copy(update={
                "status": "supported", "receipt_sha256": "e" * 64})})
        observed = reconcile_attempt(restarted, pinned, receipt)
        self.assertEqual((observed.status, observed.native_id, observed.provider_state),
                         ("observed", receipt.native_id, "stopped"))
        self.assertFalse(pinned.ready_for_submission)
        self.assertFalse(observed.replacement_submit_allowed)
        self.assertFalse(remote_cancel_verdict(pinned, observed, cancel_receipt_native_id=receipt.native_id))
        for changed in (receipt.model_copy(update={"attempt_token": "other"}),
                        receipt.model_copy(update={"input_hash": "other"}),
                        receipt.model_copy(update={"native_id": ""}),
                        receipt.model_copy(update={"state": "unknown"})):
            self.assertEqual(reconcile_attempt(attempt, pinned, changed).status, "outcome_unknown")
        self.assertEqual(reconcile_attempt(replace(attempt, can_reconcile=False), pinned, receipt).status,
                         "outcome_unknown")
        qualified_cancel = pinned.model_copy(update={"cancel": pinned.cancel.model_copy(update={
            "status": "supported", "receipt_sha256": "f" * 64})})
        self.assertFalse(remote_cancel_verdict(qualified_cancel, observed))
        self.assertFalse(remote_cancel_verdict(qualified_cancel, observed,
                                               cancel_receipt_native_id="foreign-native"))
        self.assertTrue(remote_cancel_verdict(qualified_cancel, observed,
                                              cancel_receipt_native_id=receipt.native_id))


if __name__ == "__main__":
    unittest.main()
