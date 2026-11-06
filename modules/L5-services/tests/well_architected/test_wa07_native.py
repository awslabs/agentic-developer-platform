"""Synthetic SCN-02 readback tests are not live native acceptance."""

import base64
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from l5_services.installed_release_probe import ReleaseProofError
from l5_services.wa_native_evidence import verify_native_reviews, verify_portable_native_reviews
from test_wa07_portability import cases as configuration_cases
from wa_source_fixture import read_archive, source_fixture


ACCOUNT = "000000000000"
REGION = "us-east-1"


def cases():
    projects = []
    for index in (1, 2):
        project = f"external-project-{index}"
        profile = f"arn:aws:wellarchitected:{REGION}:{ACCOUNT}:agent-profile/profile-{index}"
        generation = f"generation-{index}"
        arn = f"arn:aws:wellarchitected:{REGION}:{ACCOUNT}:agent-recommendation/00000000-0000-0000-0000-{index:012d}"
        native = {"tenant_id": "tenant-example", "project_id": project,
                  "profile_arn": profile, "generation_id": generation,
                  "name": f"adp-wa-{index}"}
        architecture = {"tenant_id": "tenant-example", "project_id": project,
                        "record_type": "document",
                        "record_id": f"architecture-{index}", "record_revision": "pinned"}
        goal = {"tenant_id": "tenant-example", "project_id": project,
                "record_id": f"goal-{index}", "record_revision": "pinned"}
        source_manifest, citation = source_fixture(architecture, architecture, f"run-{index}", text=f"IaC {index}")
        archive = source_manifest["archive"]
        files = source_manifest["architecture"]["files"]
        uri = f"s3://fictional-wa-bucket/" + archive["artifact_id"]
        projects.append({
            "run_id": f"run-{index}", "result_revision": 1,
            "run_input": {"sources": [architecture]},
            "source_manifest": source_manifest,
            "project_kind": "non_adp", "architecture": architecture, "goal": goal,
            "native_job_ref": native,
            "source_receipt": {"architecture": architecture, "native_job_ref": native,
                               "provider_coverage": "provider_coverage_unverified",
                               "sources": [architecture], "files": files, "archive": archive,
                               "provider_source_uri": uri},
            "result": {"scope": {"tenant_id": "tenant-example", "project_id": project},
                       "native_job_ref": native, "run_id": f"run-{index}", "revision": 1,
                       "manifest_ref": archive, "coverage": files,
                       "limitations": ["provider_source_coverage_unverified"],
                       "kind": "well_architected_result",
                       "review_mode": "source_only", "goals": [goal["record_id"]],
                       "recommendations": [{"native_id": arn, "generation_id": generation,
                                            "title": f"Review {index}",
                                            "comparison": {"status": "supported", "citations": [citation]}}]},
        })
    return projects


def native_reader(projects, calls):
    def read(service, operation, *arguments):
        assert service == "wellarchitected"
        calls.append(operation)
        if operation == "get-agent-recommendation-generation":
            native = next(case["native_job_ref"] for case in projects
                          if case["native_job_ref"]["generation_id"] == arguments[-1])
            source = next(case["source_receipt"] for case in projects
                          if case["native_job_ref"] == native)
            return {"id": native["generation_id"], "profileArn": native["profile_arn"],
                    "name": native["name"], "status": "COMPLETED", "additionalContext": {
                        "s3Uri": source["provider_source_uri"],
                        "s3VersionId": source["archive"]["version"],
                        "lensArns": ["arn:aws:wellarchitected::aws:lens/wellarchitected"]}}
        item = next((case, recommendation) for case in projects
                    for recommendation in case["result"]["recommendations"]
                    if recommendation["native_id"] == arguments[-1])
        case, recommendation = item
        return {"recommendationArn": recommendation["native_id"],
                "profileArn": case["native_job_ref"]["profile_arn"],
                "generationId": case["native_job_ref"]["generation_id"],
                "title": recommendation["title"], "sources": [{"name": "pinned IaC"}]}
    return read


def fixture():
    return SimpleNamespace(account_id=ACCOUNT,
                           queue_arn=f"arn:aws:sqs:{REGION}:{ACCOUNT}:adp-dev-l5-work")


def portable_cases():
    configured = configuration_cases()
    for config, native in zip(configured, cases()):
        native["architecture"] = config["architecture"]
        native["run_input"] = config["run_input"]
        native["goal"] = config["goal"]
        native["source_receipt"]["architecture"] = config["architecture"]
        native["source_receipt"]["sources"] = [config["architecture"]]
        native["source_receipt"]["files"][0]["source_ref"] = config["architecture"]
        native["source_receipt"]["files"][0]["revision"] = config["architecture"]["record_revision"]
        native["result"]["goals"] = [config["goal"]["record_id"]]
        native["result"]["recommendations"][0]["comparison"]["citations"][0]["source_ref"] = config["architecture"]
        manifest, citation = source_fixture(config["architecture"], config["architecture"], native["run_id"])
        native["source_manifest"] = manifest
        native["source_receipt"].update(archive=manifest["archive"], files=manifest["architecture"]["files"])
        native["result"].update(manifest_ref=manifest["archive"], coverage=manifest["architecture"]["files"])
        native["result"]["recommendations"][0]["comparison"]["citations"] = [citation]
        config.update(native)
    return configured


def pinned_fixture(configured):
    installed = fixture()
    release = configured[0]["release_digests"]
    for field, digest in (("wheel_sha256", "wheel"), ("schema_sha256", "schema"),
                          ("gateway_image_digest", "gateway"), ("frontend_asset_sha256", "frontend"),
                          ("cli_manifest_sha256", "cli")):
        setattr(installed, field, release[digest])
    installed.worker_code_sha256 = base64.b64encode(bytes.fromhex(release["worker"])).decode()
    return installed


def test_native_case_checks_two_independent_generation_and_recommendation_reads():
    projects, calls = cases(), []
    assert len(verify_native_reviews(projects, fixture(), native_reader(projects, calls), read_archive=read_archive)) == 2
    assert calls == ["get-agent-recommendation-generation", "get-agent-recommendation"] * 2


def test_portable_native_case_matches_pinned_installed_artifacts_and_configured_scopes():
    projects = portable_cases()
    calls = []
    assert len(verify_portable_native_reviews(projects, pinned_fixture(projects),
                                              native_reader(projects, calls), read_archive=read_archive)) == 2
    assert calls == ["get-agent-recommendation-generation", "get-agent-recommendation"] * 2


@pytest.mark.parametrize("change,error", [
    (lambda pair: pair[0].update(project_kind="adp"), "wa_native_configuration_invalid"),
    (lambda pair: pair[1].update({key: deepcopy(pair[0][key]) for key in
                                 ("run_input", "settings", "architecture", "goal")}),
     "wa_native_configuration_invalid"),
    (lambda pair: pair[1]["release_digests"].update(worker="f" * 64), "wa_native_configuration_invalid"),
    (lambda pair: pair[1]["changes_since_release"].append("deployment"), "wa_native_configuration_invalid"),
    (lambda pair: [case["release_digests"].update(gateway="f" * 64) for case in pair], "wa_native_release_changed"),
    (lambda pair: pair[1]["result"]["scope"].update(project_id="external-project-1"),
     "wa_native_configuration_scope_changed"),
])
def test_portable_native_case_refuses_incompatible_evidence_before_provider_reads(change, error):
    projects = portable_cases()
    installed = pinned_fixture(projects)
    change(projects)
    calls = []
    with pytest.raises(ReleaseProofError, match=error):
        verify_portable_native_reviews(projects, installed, native_reader(projects, calls), read_archive=read_archive)
    assert calls == []


def test_live_selector_routes_private_cases_through_portability_guard(tmp_path, monkeypatch):
    import test_wa07

    projects = portable_cases()
    installed = pinned_fixture(projects)
    projects[1]["release_digests"]["worker"] = "f" * 64
    manifest = tmp_path / "release.json"
    manifest.write_text("{}")
    readback = tmp_path / "readback.json"
    readback.write_text("{}")
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(projects))
    monkeypatch.setenv("L5_WA07_RELEASE_MANIFEST", str(manifest))
    monkeypatch.setenv("L5_WA07_RELEASE_READBACK", str(readback))
    monkeypatch.setenv("L5_WA07_PORTABLE_CASES", str(cases_path))
    monkeypatch.setenv("L5_WA07_AWS_CONNECTION", "fictional-local")
    monkeypatch.setattr(test_wa07.InstalledFixture, "from_manifest", lambda path: installed)
    monkeypatch.setattr(test_wa07, "_live_worker_readback", lambda *args: None)
    monkeypatch.setattr(test_wa07, "verify_wa_release", lambda *args: None)
    calls = []
    monkeypatch.setattr(test_wa07, "_aws_read", lambda *args: calls.append(args))
    with pytest.raises(ReleaseProofError, match="wa_native_configuration_invalid"):
        test_wa07.test_ac_02()
    assert calls == []


@pytest.mark.parametrize("change,error", [
    (lambda projects: projects[0].update(project_kind="adp"), "non_adp"),
    (lambda projects: projects[0]["native_job_ref"].update(profile_arn="foreign"), "identity_changed"),
    (lambda projects: projects[1].update(deepcopy(projects[0])), "generation_reused"),
    (lambda projects: projects[1]["result"].update(goals=[]), "goals_changed"),
    (lambda projects: projects[0]["source_receipt"].update(provider_coverage="unknown"), "source_unverified"),
    (lambda projects: projects[0]["result"].update(recommendations=[]), "recommendations_missing"),
    (lambda projects: projects[0]["result"]["recommendations"][0]["comparison"].update(citations=[]), "comparison_unverified"),
    (lambda projects: projects[0]["result"]["recommendations"][0]["comparison"]["citations"][0].update(
        source_ref=projects[1]["architecture"]), "citation_unverified"),
])
def test_native_case_refuses_foreign_unattributed_or_unverified_results(change, error):
    projects = cases()
    change(projects)
    with pytest.raises(ReleaseProofError, match=f"wa_native_{error}"):
        verify_native_reviews(projects, fixture(), native_reader(projects, []), read_archive=read_archive)


def test_live_selector_without_fixtures_skips_not_passes(monkeypatch):
    from test_wa07 import test_ac_02

    for variable in ("L5_WA07_RELEASE_MANIFEST", "L5_WA07_RELEASE_READBACK", "L5_WA07_PORTABLE_CASES"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(pytest.skip.Exception, match="deployed-target"):
        test_ac_02()
    monkeypatch.setenv("L5_WA07_PORTABLE_CASES", "/private/nonexistent")
    with pytest.raises(pytest.fail.Exception, match="release manifest"):
        test_ac_02()
