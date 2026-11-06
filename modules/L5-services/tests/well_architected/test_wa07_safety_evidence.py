"""Synthetic selector checks do not establish installed safety acceptance."""

import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from l5_services.installed_release_probe import ReleaseProofError
from l5_services.wa_safety_evidence import verify_wa_safety


ACCOUNT = "000000000000"
REGION = "us-east-1"
PREFIX = f"arn:aws:wellarchitected:{REGION}:{ACCOUNT}:agent-profile/"
SCOPE = {"tenant_id": "tenant-example", "project_id": "project-example"}


def fixture():
    return SimpleNamespace(account_id=ACCOUNT, role_arn=f"arn:aws:iam::{ACCOUNT}:role/selected-role",
                           queue_arn=f"arn:aws:sqs:{REGION}:{ACCOUNT}:adp-dev-l5-work")


def safety_case():
    adopted = PREFIX + "adopted-example"
    name = "adp-wa-" + sha256(b"tenant-example:run-example:attempt-example").hexdigest()[:32]
    native = {**SCOPE, "profile_arn": adopted, "generation_id": "generation-example", "name": name}
    denials = []
    for kind in ("foreign_tenant", "sibling_project", "foreign_source", "wrong_role"):
        denial = {"kind": kind, "request_scope": SCOPE.copy(), "source": "installed-worker",
                  "evidence_ref": f"private/{kind}", "status": 404,
                  "provider_calls_before": 0, "provider_calls_after": 0,
                  "native_ids_before": [], "native_ids_after": []}
        if kind == "foreign_tenant":
            denial["request_scope"]["tenant_id"] = "tenant-foreign"
        elif kind == "sibling_project":
            denial["request_scope"]["project_id"] = "project-sibling"
        elif kind == "foreign_source":
            denial["source_scope"] = {**SCOPE, "project_id": "project-foreign"}
        else:
            denial["role_arn"] = f"arn:aws:iam::{ACCOUNT}:role/denied-role"
        denials.append(denial)
    return {"source": "authenticated-runtime", "evidence_ref": "private/safety",
            "scope": SCOPE.copy(), "native_job_ref": native, "denials": denials,
            "recovery": {"source": "installed-worker", "evidence_ref": "private/recovery",
                         "run_id": "run-example", "attempt_id": "attempt-example",
                         "faults": ["lost_start_response", "restart", "local_timeout"],
                         "start_call_count": 1, "native_job_ref": native, "remote_state_at_timeout": "unknown"},
            "archive": {"bucket": "example-source-bucket", "key": "private/architecture.zip",
                        "version": "pinned-version", "sha256": "a" * 64},
            "adopted": {"profile_arn": adopted, "before": {"arn": adopted,
                        "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/source-only",
                        "aggregationConfiguration": [], "pillars": ["SECURITY"], "tags": []},
                        "contexts_before": []},
            "owned": {"profile_arn": PREFIX + "owned-example", "ownership": "receipt_owned",
                      "evidence_ref": "private/owned-create"},
            "result": {"source": "authenticated-runtime", "readback_source": "installed-api",
                       "evidence_ref": "private/result-readback", "scope": SCOPE.copy(),
                       "native_job_ref": native, "result_id": "result-example", "revision": 2,
                       "before_sha256": "b" * 64, "after_sha256": "b" * 64}}


def provider_reader(case, calls, *, duplicate=False, source_missing=False, owned_present=False,
                    contexts_changed=False, source_mismatch=False):
    def read(service, operation, *arguments):
        calls.append(operation)
        if operation == "list-agent-recommendation-generations":
            item = {"id": case["native_job_ref"]["generation_id"],
                    "name": case["native_job_ref"]["name"],
                    "profileArn": case["native_job_ref"]["profile_arn"]}
            return {"items": [item, item] if duplicate else [item]}
        if operation == "get-agent-recommendation-generation":
            return {"id": case["native_job_ref"]["generation_id"],
                    "name": case["native_job_ref"]["name"],
                    "profileArn": case["native_job_ref"]["profile_arn"], "status": "COMPLETED",
                    "additionalContext": {"sourceArchive": {
                        "sha256": "f" * 64 if source_mismatch else case["archive"]["sha256"]}}}
        if operation == "head-object":
            return {"VersionId": "different" if source_missing else case["archive"]["version"],
                    "ContentLength": 100,
                    "Metadata": {"sha256": case["archive"]["sha256"]}}
        if operation == "get-agent-profile":
            return case["adopted"]["before"]
        if operation == "list-agent-contexts":
            return {"items": [{"profileArn": case["adopted"]["profile_arn"]}] if contexts_changed
                    else case["adopted"]["contexts_before"]}
        if operation == "list-agent-profiles":
            profiles = [{"arn": case["adopted"]["profile_arn"]}]
            if owned_present:
                profiles.append({"arn": case["owned"]["profile_arn"]})
            return {"items": profiles}
        raise AssertionError("unexpected provider read")
    return read


def test_safety_case_requires_native_source_adopted_and_owned_readbacks():
    case, calls = safety_case(), []
    result = verify_wa_safety(case, fixture(), provider_reader(case, calls), lambda *args: True)
    assert result == {"native_id": "generation-example", "result_id": "result-example"}
    assert calls == ["list-agent-recommendation-generations", "get-agent-recommendation-generation",
                     "head-object", "get-agent-profile", "list-agent-contexts", "list-agent-profiles"]


@pytest.mark.parametrize("change,error", [
    (lambda case: case["denials"][0].update(provider_calls_after=1), "denial_effect_unverified"),
    (lambda case: case["denials"][2].update(source_scope=SCOPE.copy()), "denial_effect_unverified"),
    (lambda case: case["denials"][3].update(role_arn=fixture().role_arn), "denial_effect_unverified"),
    (lambda case: case["recovery"].update(start_call_count=2), "recovery_unverified"),
    (lambda case: case["native_job_ref"].update(name="unattributed"), "native_identity_unverified"),
    (lambda case: case["recovery"].update(faults=["restart"]), "recovery_unverified"),
    (lambda case: case["result"].update(after_sha256="c" * 64), "retention_unverified"),
    (lambda case: case["result"].update(readback_source="mock"), "retention_unverified"),
    (lambda case: case["owned"].update(ownership="adopted"), "ownership_unverified"),
])
def test_safety_case_refuses_unattributed_worker_receipts_before_provider_reads(change, error):
    case, calls = safety_case(), []
    change(case)
    with pytest.raises(ReleaseProofError, match=f"wa_safety_{error}"):
        verify_wa_safety(case, fixture(), provider_reader(case, calls), lambda *args: True)
    assert calls == []


@pytest.mark.parametrize("variant,error", [
    ("duplicate", "duplicate_generation"),
    ("source_missing", "source_missing"),
    ("source_mismatch", "native_identity_unverified"),
    ("owned_present", "owned_or_adopted_unverified"),
    ("not_absent", "owned_present"),
    ("contexts_changed", "adopted_changed"),
])
def test_safety_case_refuses_unverified_native_and_cleanup_readbacks(variant, error):
    case, calls = safety_case(), []
    with pytest.raises(ReleaseProofError, match=f"wa_safety_{error}"):
        verify_wa_safety(case, fixture(), provider_reader(
            case, calls, duplicate=variant == "duplicate", source_missing=variant == "source_missing",
            owned_present=variant == "owned_present", contexts_changed=variant == "contexts_changed",
            source_mismatch=variant == "source_mismatch"),
            lambda *args: variant != "not_absent")
    assert calls


def test_safety_selector_skips_without_private_installed_inputs(monkeypatch):
    from test_wa07 import test_ac_03

    for key in ("L5_WA07_RELEASE_MANIFEST", "L5_WA07_RELEASE_READBACK",
                "L5_WA07_SAFETY_CASE", "L5_WA07_AWS_CONNECTION"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(pytest.skip.Exception, match="deployed-target"):
        test_ac_03()
    monkeypatch.setenv("L5_WA07_SAFETY_CASE", "/private/nonexistent")
    with pytest.raises(pytest.fail.Exception, match="release manifest"):
        test_ac_03()
    monkeypatch.setenv("L5_WA07_RELEASE_MANIFEST", "/private/nonexistent")
    monkeypatch.setenv("L5_WA07_RELEASE_READBACK", "/private/nonexistent")
    with pytest.raises(pytest.fail.Exception, match="selected WA07 AWS connection"):
        test_ac_03()


def test_safety_selector_routes_private_case_through_installed_guard(tmp_path, monkeypatch):
    import test_wa07

    case = safety_case()
    case["denials"][0]["provider_calls_after"] = 1
    paths = [tmp_path / name for name in ("release.json", "readback.json", "safety.json")]
    for path, payload in zip(paths, ({}, {}, case)):
        path.write_text(json.dumps(payload))
    for key, path in zip(("L5_WA07_RELEASE_MANIFEST", "L5_WA07_RELEASE_READBACK",
                          "L5_WA07_SAFETY_CASE"), paths):
        monkeypatch.setenv(key, str(path))
    monkeypatch.setenv("L5_WA07_AWS_CONNECTION", "fictional-local")
    monkeypatch.setattr(test_wa07.InstalledFixture, "from_manifest", lambda path: fixture())
    monkeypatch.setattr(test_wa07, "_live_worker_readback", lambda *args: None)
    monkeypatch.setattr(test_wa07, "verify_wa_release", lambda *args: None)
    calls = []
    monkeypatch.setattr(test_wa07, "_aws_read", lambda *args: calls.append(args))
    monkeypatch.setattr(test_wa07, "_aws_absent", lambda *args: calls.append(args))
    with pytest.raises(ReleaseProofError, match="wa_safety_denial_effect_unverified"):
        test_wa07.test_ac_03()
    assert calls == []


def test_safety_owned_absence_does_not_confuse_denied_read_with_not_found(monkeypatch):
    import test_wa07

    responses = iter(((254, "AccessDeniedException"), (254, "ResourceNotFoundException")))
    calls = []

    def provider_run(arguments, **options):
        calls.append(arguments)
        code, error = next(responses)
        return SimpleNamespace(returncode=code, stderr=error)

    monkeypatch.setattr(test_wa07.subprocess, "run", provider_run)
    arguments = ("wellarchitected", "get-agent-profile", "--profile-arn", PREFIX + "owned-example")
    assert not test_wa07._aws_absent("fictional-local", *arguments)
    assert test_wa07._aws_absent("fictional-local", *arguments)
    assert all(call[:7] == ["adp-cred", "assume", "--service", "aws", "--label",
                             "fictional-local", "--exec"] for call in calls)
