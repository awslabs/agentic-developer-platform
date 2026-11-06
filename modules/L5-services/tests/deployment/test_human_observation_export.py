"""Installed release collector uses fresh protected exports, never fixture verdicts."""

import copy
import json
from types import SimpleNamespace

import pytest

from test_shared_release import environment, load, Opener  # noqa: F401

collector = load("qualification_human_observations", "qualification_human_observations.py")


@pytest.fixture
def selected(environment):  # noqa: F811
    selector = {
        "challenge_id": "00000000-0000-4000-8000-000000000010", "snapshot_sha256": "d" * 64,
        "selection": {"product_id": "product", "project_id": "project", "service_id": "security", "binding_id": "binding",
                      "configuration_id": "configuration", "configuration_revision": 1, "check_id": "ui-cli-mcp-parity",
                      "subject": "configuration", "result_id": None, "result_revision": None, "result_run_id": None},
    }
    release = collector.release_identity(environment)
    response = {
        "contract": collector.CONTRACT, "release_check_completed": False, "native_qualified": False,
        "export_receipt": {"decision_id": "00000000-0000-4000-8000-000000000012", "sha256": "e" * 64},
        "observation": {
            **selector, "actor_id": "selected-human", "observed_at": "2026-01-01T00:00:00+00:00",
            "provenance": "authenticated-human-observation", "release_check_completed": False, "native_qualified": False,
            "decision_references": [
                {"decision_id": selector["challenge_id"], "sha256": "f" * 64},
                {"decision_id": "00000000-0000-4000-8000-000000000011", "sha256": "0" * 64},
            ],
            "release": {
                **{key: release[key] for key in ("source_revision", "definition_revision", "run_id", "run_attempt", "correlation", "contract_digest")},
                "repo": environment["GITHUB_REPOSITORY"], "provider_repository_id": release["repository_id"],
                "definition_revision": "c" * 40,
                "dispatch_revision": release["definition_revision"], "workflow": ".github/workflows/l5-services-release.yml",
                "provenance": "oidc-authorized-delivery-dispatch-and-provider-run",
            },
            "observed": {
                "snapshot_sha256": selector["snapshot_sha256"], "observation": "displayed", "displayed_configuration_revision": 1,
                "displayed_result_id": None, "displayed_result_revision": None, "displayed_native_hostname": None, "launch_receipt_id": None,
            },
        },
    }
    return SimpleNamespace(selector=selector, response=response, environment=environment)


def test_collector_uses_current_oidc_and_records_partial_response_in_bound_journal(selected):
    c = selected
    opener = Opener(c.response)
    records = []
    journal = SimpleNamespace(binding={"context": collector.release_identity(c.environment)}, append=lambda kind, value: records.append((kind, value)))
    result = collector.collect_into(journal, [c.selector], environment=c.environment, opener=opener)
    assert len(result) == 1 and result[0].facts == c.response
    assert records == [("human-observation-export", {"response_sha256": result[0].sha256, "response": c.response})]
    assert opener.requests[0].full_url.endswith("audience=adp-operations-release-job-v1")
    request = opener.requests[1]
    assert request.full_url.endswith("/api" + collector.PATH) and request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer fictional-oidc-credential"
    assert json.loads(request.data)["selection"] == c.selector["selection"]
    assert result[0].facts["native_qualified"] is result[0].facts["release_check_completed"] is False


@pytest.mark.parametrize("change", ["run", "source", "dispatch", "invalid_definition", "contract", "selection", "pass", "provenance", "receipt", "observed_pass"])
def test_collector_rejects_cross_scope_or_fabricated_evidence(selected, change):
    c = selected
    response = copy.deepcopy(c.response)
    if change in {"run", "source", "dispatch", "invalid_definition", "contract"}:
        key = {"run": "run_id", "source": "source_revision", "dispatch": "dispatch_revision",
               "invalid_definition": "definition_revision", "contract": "contract_digest"}[change]
        response["observation"]["release"][key] = "foreign"
    elif change == "selection":
        response["observation"]["selection"]["configuration_revision"] = 2
    elif change == "pass":
        response["release_check_completed"] = True
    elif change == "provenance":
        response["observation"]["provenance"] = "machine-observation"
    elif change == "receipt":
        response["export_receipt"]["sha256"] = "invalid"
    else:
        response["observation"]["observed"]["pass"] = True
    with pytest.raises(ValueError):
        collector.collect(c.environment, c.selector, opener=Opener(response))


def test_fixture_cannot_supply_collected_facts_or_change_journal_identity(selected):
    c = selected
    with pytest.raises(ValueError, match="fixture verdicts"):
        collector.collect(c.environment, {**c.selector, "response": c.response}, opener=Opener(c.response))
    with pytest.raises(ValueError, match="identities differ"):
        collector.collect_into(SimpleNamespace(binding={"context": {}}), [c.selector], environment=c.environment)
    with pytest.raises(ValueError, match="not collected"):
        _ = collector.CollectedHumanObservation(json.dumps(c.response), object()).facts


@pytest.mark.parametrize("boundary", [None, "missing_run", "other_subject", "other_service"])
def test_collector_submission_requires_its_original_devops_subject_and_run(selected, boundary):
    c = selected
    c.environment["L5_SERVICE"] = "devops"
    c.selector["selection"].update(service_id="devops", subject="run_submission")
    c.response["observation"]["observed"].update(observation="submitted", submitted_run_id="original-ui-run", displayed_configuration_revision=None)
    if boundary == "missing_run":
        c.response["observation"]["observed"]["submitted_run_id"] = None
    elif boundary == "other_subject":
        c.selector["selection"]["subject"] = "configuration"
    elif boundary == "other_service":
        c.selector["selection"]["service_id"] = "security"
    if boundary:
        with pytest.raises(ValueError):
            collector.collect(c.environment, c.selector, opener=Opener(c.response))
    else:
        assert collector.collect(c.environment, c.selector, opener=Opener(c.response)).facts == c.response
