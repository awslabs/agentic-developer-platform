"""Product HTTP and native AWS boundaries are mocked; no paid Start is made."""

from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import httpx
import pytest

from l5_services.adapters.well_architected.qualification import (
    ProductReader, emit_native_case, read_selected_review)
from test_wa04 import PROFILE, SCOPE
from wa_source_fixture import read_archive, source_fixture


@pytest.fixture
def selected_review():
    scope = SCOPE
    source = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
              "record_type": "document", "record_id": "architecture-example",
              "record_revision": "pinned-example", "source_connection_id": "connection-example"}
    profile = {"arn": PROFILE["record_id"], "aggregationConfiguration": [],
               "executionRoleArn": "arn:aws:iam::000000000000:role/reader-example",
               "pillars": ["SECURITY"], "eligibleForArchitectureGeneration": True,
               "eligibleForScheduledGeneration": False, "fieldErrors": []}
    fingerprint = sha256(json.dumps({"arn": profile["arn"], "aggregation": [],
                         "role": profile["executionRoleArn"], "pillars": profile["pillars"],
                         "contexts": [], "eligible": True, "scheduled": False, "field_errors": []},
                         sort_keys=True).encode()).hexdigest()
    profile_ref = {**PROFILE, "record_revision": fingerprint}
    native_ref = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                  "profile_arn": PROFILE["record_id"], "generation_id": "generation-example",
                  "name": "adp-wa-example"}
    recommendation_arn = "arn:aws:wellarchitected:us-east-1:000000000000:agent-recommendation/00000000-0000-0000-0000-000000000001"
    source_manifest, _ = source_fixture(source, scope, "run-example")
    archive = source_manifest["archive"]
    native_context = {"s3Uri": "s3://fictional-wa-bucket/" + archive["artifact_id"],
                      "s3VersionId": archive["version"],
                      "lensArns": ["arn:aws:wellarchitected::aws:lens/wellarchitected"]}
    records = {
        "configuration": {"scope": scope, "configuration_id": "config-example", "revision": 1,
                          "hash": "a" * 64, "state": "ready", "handoff_state": "linked",
                          "settings": {"enabled": True, "source_refs": [source], "well_architected": {
                              "review_mode": "source_only", "execution_role_ref": {
                                  "record_id": profile["executionRoleArn"]}}}},
        "bindings/binding-example": {"scope": scope, "binding_id": "binding-example",
                                     "generation": 1, "revision": 2, "state": "ready", "configuration_id": "config-example",
                                     "native_resource_ref": profile_ref,
                                     "provider_identity": {"account_id": "000000000000",
                                         "role_arn": "arn:aws:iam::000000000000:role/broker-example",
                                         "region": "us-east-1"}},
        "runs/run-example": {"scope": scope, "run_id": "run-example", "revision": 4, "state": "succeeded",
                             "source_manifest": source_manifest,
                             "native_job_ref": native_ref,
                             "input": {"sources": [source], "binding_id": "binding-example",
                                       "configuration_revision": 1},
                             "admission": {"scope": scope, "binding_generation": 1,
                                           "configuration_hash": "a" * 64, "profile_hash": fingerprint,
                                           "native_profile_ref": profile_ref, "generation_creator": "actor-example",
                                           "generation_scope": {"pillars": ["SECURITY"]}}},
        "results/result-example": {"scope": scope, "result_id": "result-example", "revision": 1,
                                   "run_id": "run-example", "kind": "well_architected_result",
                                   "review_mode": "source_only", "configuration_revision": 1,
                                   "binding_generation": 1, "native_job_ref": native_ref,
                                   "manifest_ref": archive,
                                   "coverage": source_manifest["architecture"]["files"],
                                   "recommendations": [{"native_id": recommendation_arn,
                                                        "generation_id": "generation-example",
                                                        "title": "Check encryption"}]},
    }

    class Native:
        def __init__(self):
            self.calls = []

        def get_agent_profile(self, **request):
            self.calls.append("profile")
            return profile

        def list_agent_contexts(self, **request):
            self.calls.append("contexts")
            return {"items": []}

        def get_agent_recommendation_generation(self, **request):
            self.calls.append("generation")
            return {"id": native_ref["generation_id"], "profileArn": native_ref["profile_arn"],
                    "name": native_ref["name"], "status": "COMPLETED", "createdBy": "actor-example",
                    "scope": {"pillars": ["SECURITY"]}, "additionalContext": native_context}

        def list_agent_recommendations(self, **request):
            self.calls.append("list")
            if request.get("nextToken"):
                return {"items": [{"recommendationArn": recommendation_arn,
                         "profileArn": native_ref["profile_arn"], "generationId": native_ref["generation_id"]}]}
            return {"items": [{"recommendationArn": "other-example",
                    "profileArn": native_ref["profile_arn"], "generationId": "other-generation"}],
                    "nextToken": "second-page"}

        def get_agent_recommendation(self, **request):
            self.calls.append("detail")
            return {"recommendationArn": recommendation_arn,
                    "profileArn": native_ref["profile_arn"], "generationId": native_ref["generation_id"],
                    "title": "Check encryption", "sources": [{"name": "source referenced by provider"}]}

        def start_agent_recommendation_generation(self, **request):
            raise AssertionError("qualification must never Start")

    native = Native()
    delegated = SimpleNamespace(account_id="000000000000", region="us-east-1",
                                read_archive=read_archive,
                                role_arn="arn:aws:iam::000000000000:role/broker-example",
                                client=lambda service: native if service == "wellarchitected" else None)
    selection = {"scope": scope, "binding_id": "binding-example", "run_id": "run-example",
                 "result_id": "result-example", "configuration_revision": 1,
                 "result_revision": 1, "binding_revision": 2, "run_revision": 4, "sources": [source]}
    requests = []

    def respond(request):
        prefix = (f"/api/v1/products/{scope['product_id']}/projects/{scope['project_id']}"
                  "/services/well_architected/")
        assert request.method == "GET" and request.url.path.startswith(prefix)
        path = request.url.path.removeprefix(prefix)
        requests.append(path)
        return httpx.Response(200, json=records[path]) if path in records else httpx.Response(404)

    client = httpx.Client(base_url="https://gateway.example.org", transport=httpx.MockTransport(respond))
    yield ProductReader(client, scope), delegated, selection, records, native, requests
    client.close()


def test_selected_driver_uses_product_api_and_native_reads(selected_review):
    product, delegated, selection, records, native, requests = selected_review
    observation = read_selected_review(product, delegated, selection)
    assert requests == ["configuration", "bindings/binding-example", "runs/run-example",
                        "results/result-example"]
    assert native.calls == ["profile", "contexts", "generation", "list", "list", "detail"]
    assert observation["product"]["result"] == records["results/result-example"]
    assert observation["native"]["recommendations"][
        records["results/result-example"]["recommendations"][0]["native_id"]]["generationId"] == "generation-example"
    assert observation["provider_coverage"] == "provider_coverage_unverified"


@pytest.mark.parametrize("change", ["bucket", "key_prefix"])
def test_driver_rejects_native_location_drift_with_identical_key_suffix_and_version(selected_review, change):
    product, delegated, selection, records, native, _ = selected_review
    original = native.get_agent_recommendation_generation

    def changed_generation(**request):
        response = deepcopy(original(**request))
        context = response["additionalContext"]
        if change == "bucket":
            context["s3Uri"] = context["s3Uri"].replace("fictional-wa-bucket", "foreign-bucket")
        else:
            context["s3Uri"] = context["s3Uri"].replace("/l5/", "/foreign-prefix/l5/")
        archive = records["results/result-example"]["manifest_ref"]
        assert context["s3VersionId"] == archive["version"]
        assert context["s3Uri"].endswith("/" + archive["artifact_id"])
        return response

    native.get_agent_recommendation_generation = changed_generation
    with pytest.raises(ValueError, match="independent generation differs"):
        read_selected_review(product, delegated, selection)


@pytest.mark.parametrize("change", ["bytes", "missing_reader", "missing_manifest", "manifest_version"])
def test_driver_requires_authoritative_manifest_and_hash_verified_bytes(selected_review, change):
    product, delegated, selection, records, _, _ = selected_review
    if change == "bytes":
        original = delegated.read_archive
        delegated.read_archive = lambda uri, reference: original(uri, reference)[:-1] + b"x"
    elif change == "missing_reader":
        delegated.read_archive = None
    elif change == "missing_manifest":
        records["runs/run-example"].pop("source_manifest")
    else:
        manifest = deepcopy(records["runs/run-example"]["source_manifest"])
        manifest["archive"]["version"] = "foreign-version"
        records["runs/run-example"]["source_manifest"] = manifest
    with pytest.raises(ValueError):
        read_selected_review(product, delegated, selection)


@pytest.mark.parametrize("change", ["legacy", "account", "region", "suffix"])
def test_driver_rejects_profile_arn_outside_native_selected_identity(selected_review, change):
    product, delegated, selection, records, native, _ = selected_review
    run = records["runs/run-example"]
    arn = run["native_job_ref"]["profile_arn"]
    arn = {"legacy": arn.replace(":agent-profile/", ":profile/"),
           "account": arn.replace("000000000000", "000000000001"),
           "region": arn.replace("us-east-1", "eu-west-1"), "suffix": arn + "/extra"}[change]
    run["native_job_ref"]["profile_arn"] = arn
    run["admission"]["native_profile_ref"]["record_id"] = arn
    with pytest.raises(ValueError, match="provider identity"):
        read_selected_review(product, delegated, selection)
    assert native.calls == []


def test_qualification_identifiers_match_pinned_sdk_patterns(selected_review):
    import botocore.session
    import re

    product, delegated, selection, _, _, _ = selected_review
    observation = read_selected_review(product, delegated, selection)
    model = botocore.session.get_session().get_service_model("wellarchitected")
    profile_pattern = model.operation_model("GetAgentProfile").input_shape.members["profileArn"].metadata["pattern"]
    recommendation_pattern = model.operation_model("GetAgentRecommendation").input_shape.members["recommendationArn"].metadata["pattern"]
    assert re.fullmatch(profile_pattern, observation["native"]["profile"]["arn"])
    assert all(re.fullmatch(recommendation_pattern, arn) for arn in observation["native"]["recommendations"])


def test_public_source_manifest_schema_retains_pinned_metadata(selected_review):
    from pathlib import Path
    from jsonschema import Draft202012Validator

    _, _, _, records, _, _ = selected_review
    schema = json.loads((Path(__file__).resolve().parents[4]
                         / "docs/l5-services/contracts/v1/schemas.json").read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/WellArchitectedSourceManifest"})
    manifest = deepcopy(records["runs/run-example"]["source_manifest"])
    validator.validate(manifest)
    manifest["allow"] = True
    assert list(validator.iter_errors(manifest))


@pytest.mark.parametrize("mutation", ["foreign_scope", "stale_config", "stale_result", "foreign_role",
                                   "profile_drift", "wrong_generation", "wrong_recommendation",
                                   "missing_coverage", "foreign_source_coverage", "config_sources",
                                   "stale_binding", "stale_run"])
def test_driver_rejects_incompatible_independent_reads(selected_review, mutation):
    product, delegated, selection, records, native, _ = selected_review
    if mutation == "foreign_scope":
        records["runs/run-example"]["scope"] = {**SCOPE, "project_id": "other-example"}
    elif mutation == "stale_config":
        records["configuration"]["revision"] = 2
    elif mutation == "stale_binding":
        records["bindings/binding-example"]["revision"] = 3
    elif mutation == "stale_run":
        records["runs/run-example"]["revision"] = 5
    elif mutation == "stale_result":
        records["results/result-example"]["revision"] = 2
    elif mutation == "foreign_role":
        delegated.role_arn = "arn:aws:iam::000000000000:role/foreign-example"
    elif mutation == "profile_drift":
        native.get_agent_profile = lambda **request: {**records["runs/run-example"]["admission"], "arn": PROFILE["record_id"]}
    elif mutation == "config_sources":
        records["configuration"]["settings"]["source_refs"] = []
    elif mutation == "missing_coverage":
        records["results/result-example"]["coverage"] = []
    elif mutation == "foreign_source_coverage":
        records["results/result-example"]["coverage"][0]["source_ref"] = {
            **selection["sources"][0], "record_revision": "stale-example"}
    elif mutation == "wrong_generation":
        native.get_agent_recommendation_generation = lambda **request: {"id": "other-generation"}
    else:
        native.get_agent_recommendation = lambda **request: {"generationId": "other-generation"}
    with pytest.raises(ValueError):
        read_selected_review(product, delegated, deepcopy(selection))


def source_bound_observation(selected_review):
    product, delegated, selection, records, _, _ = selected_review
    goal = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
            "record_id": "goal-example", "record_revision": "pinned-example"}
    selection["project_kind"] = "non_adp"
    records["configuration"]["settings"]["well_architected"]["goal_refs"] = [goal]
    result = records["results/result-example"]
    result["goals"] = [goal["record_id"]]
    result["limitations"] = ["provider_source_coverage_unverified"]
    _, citation = source_fixture(selection["sources"][0], selection["scope"], selection["run_id"])
    result["recommendations"][0]["comparison"] = {"status": "supported", "citations": [citation]}
    return read_selected_review(product, delegated, selection)


def test_emitted_product_observation_is_accepted_by_native_evaluator(selected_review):
    from l5_services.wa_native_evidence import verify_native_reviews
    from test_wa07_native import cases, fixture, native_reader

    observed = source_bound_observation(selected_review)
    case = emit_native_case(observed)
    assert case["source_receipt"]["sources"] == observed["selection"]["sources"]
    assert case["source_receipt"]["provider_coverage"] == "provider_coverage_unverified"
    assert case["source_receipt"]["provider_source_uri"] == (
        observed["native"]["generation"]["additionalContext"]["s3Uri"])
    selected = [case, cases()[1]]
    calls = []
    assert len(verify_native_reviews(selected, fixture(), native_reader(selected, calls), read_archive=read_archive)) == 2
    assert calls == ["get-agent-recommendation-generation", "get-agent-recommendation"] * 2


@pytest.mark.parametrize("field,value", [("quote", "This sentence is absent from the source"),
                                       ("path", "foreign.txt"), ("revision", "wrong-revision"),
                                       ("line", 2), ("line", True)])
def test_emitter_and_evaluator_verify_exact_pinned_citation(selected_review, field, value):
    from l5_services.installed_release_probe import ReleaseProofError
    from l5_services.wa_native_evidence import verify_native_reviews
    from test_wa07_native import cases, fixture, native_reader

    observed = source_bound_observation(selected_review)
    case = deepcopy(emit_native_case(observed))
    observed["product"]["result"]["recommendations"][0]["comparison"]["citations"][0][field] = value
    with pytest.raises(ValueError, match="pinned source line"):
        emit_native_case(observed)
    case["result"]["recommendations"][0]["comparison"]["citations"][0][field] = value
    selected = [case, cases()[1]]
    with pytest.raises(ReleaseProofError, match="wa_native_citation_unverified"):
        verify_native_reviews(selected, fixture(), native_reader(selected, []), read_archive=read_archive)


@pytest.mark.parametrize("mutation", ["missing_goal", "fake_coverage", "wrong_revision", "foreign_source",
                                   "wrong_version", "fabricated_citation", "missing_file"])
def test_emitter_and_evaluator_reject_falsified_source_observations(selected_review, mutation):
    from l5_services.installed_release_probe import ReleaseProofError
    from l5_services.wa_native_evidence import verify_native_reviews
    from test_wa07_native import cases, fixture, native_reader

    observed = source_bound_observation(selected_review)
    if mutation == "missing_goal":
        observed["product"]["configuration"]["settings"]["well_architected"]["goal_refs"] = []
    elif mutation == "fake_coverage":
        observed["provider_coverage"] = "verified"
    elif mutation == "wrong_revision":
        observed["selection"]["result_revision"] = 2
    elif mutation == "wrong_version":
        observed["native"]["generation"]["additionalContext"]["s3VersionId"] = "other-version"
    if mutation in {"missing_goal", "fake_coverage", "wrong_revision", "wrong_version"}:
        with pytest.raises(ValueError):
            emit_native_case(observed)
        return
    case = emit_native_case(observed)
    if mutation == "foreign_source":
        case["source_receipt"]["files"][0]["source_ref"] = {**case["architecture"],
                                                               "project_id": "other-project"}
    elif mutation == "fabricated_citation":
        case["result"]["recommendations"][0]["comparison"]["citations"][0]["sha256"] = "f" * 64
    else:
        case["source_receipt"]["files"] = []
    selected = [case, cases()[1]]
    with pytest.raises(ReleaseProofError, match="wa_native_(source|citation)_unverified"):
        verify_native_reviews(selected, fixture(), native_reader(selected, []), read_archive=read_archive)


def test_evaluator_rejects_independently_changed_native_source_uri(selected_review):
    from l5_services.installed_release_probe import ReleaseProofError
    from l5_services.wa_native_evidence import verify_native_reviews
    from test_wa07_native import cases, fixture, native_reader

    selected = [emit_native_case(source_bound_observation(selected_review)), cases()[1]]
    read = native_reader(selected, [])

    def changed_read(service, operation, *arguments):
        response = read(service, operation, *arguments)
        if operation == "get-agent-recommendation-generation" and arguments[-1] == "generation-example":
            return {**response, "additionalContext": {**response["additionalContext"],
                    "s3Uri": "s3://fictional-wa-bucket/another-archive.zip"}}
        return response

    with pytest.raises(ReleaseProofError, match="wa_native_generation_unverified"):
        verify_native_reviews(selected, fixture(), changed_read, read_archive=read_archive)


def test_evaluator_rejects_extra_foreign_source_despite_matching_product_claim(selected_review):
    from l5_services.installed_release_probe import ReleaseProofError
    from l5_services.wa_native_evidence import verify_native_reviews
    from test_wa07_native import cases, fixture, native_reader

    selected = [emit_native_case(source_bound_observation(selected_review)), cases()[1]]
    case = selected[0]
    foreign = {**case["architecture"], "project_id": "other-project"}
    case["source_receipt"]["sources"].append(foreign)
    case["run_input"]["sources"].append(foreign)
    case["source_receipt"]["files"].append({**case["source_receipt"]["files"][0],
                                              "source_ref": foreign})
    with pytest.raises(ReleaseProofError, match="wa_native_source_unverified"):
        verify_native_reviews(selected, fixture(), native_reader(selected, []), read_archive=read_archive)
