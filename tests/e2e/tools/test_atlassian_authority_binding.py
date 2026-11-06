import copy
import json
import pytest

from tests.e2e.tools import (
    runner,
    evidence_reader,
    atlassian_authority_scenario as authority,
)
from tests.e2e.tools.test_atlassian_authority_scenario import (
    manifest,
    bind,
    BINDING,
    GATEWAY,
    ACCOUNT,
    REGION,
    ROLE,
)


def test_actual_runner_preserves_pending_authority_and_reader_rejects_promotion(
    tmp_path, monkeypatch
):
    doc = manifest()
    for spec in doc["clients"].values():
        spec["probes"] = dict.fromkeys(authority.CASES)
    path = bind(tmp_path, doc)
    campaign = authority.AuthorityCampaign(
        path,
        binding=BINDING,
        gateway_url=GATEWAY,
        account=ACCOUNT,
        region=REGION,
        reader_role=ROLE,
        client_factory=None,
    )
    monkeypatch.setattr(
        campaign,
        "_start",
        lambda *_: pytest.fail("missing consent started a native client"),
    )
    monkeypatch.setattr(
        runner,
        "verify_gateway",
        lambda url: None if url == GATEWAY else pytest.fail("foreign gateway"),
    )
    output = tmp_path / "evidence"
    gateway_image, facade_image = (
        "example/gateway@sha256:" + "d" * 64,
        "example/facade@sha256:" + "e" * 64,
    )
    passed = runner.evaluate(
        "SCN-8454-03",
        output,
        GATEWAY,
        BINDING["source_revision"],
        gateway_image,
        facade_image,
        command=["never-run"],
        client_factory=lambda *_: pytest.fail("unbound generic client started"),
        atlassian_authority_campaign=campaign,
        live=True,
    )
    assert not passed
    report = json.loads((output / "evidence.json").read_text())
    assert report["selected_scenario_ids"] == ["SCN-8454-03"]
    row = report["scenarios"][0]
    assert row["status"] == "blocked" and len(row["observations"]["cases"]) == 16
    assert all(case["status"] == "pending" for case in row["observations"]["cases"])
    candidate = copy.deepcopy(report)
    candidate["scenarios"][0].update(status="pass", cleanup={"status": "confirmed"})
    reached = []
    real = authority.qualify_authority

    def qualify(*args, **kwargs):
        reached.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(authority, "qualify_authority", qualify)
    with pytest.raises(ValueError):
        evidence_reader.validate_atlassian_rows(
            {},
            candidate,
            selected=["SCN-8454-03"],
            run_id="101",
            attempt="1",
            revision=BINDING["definition_revision"],
            target=report["deployed_revisions"],
            authority_manifest=doc,
        )
    assert reached == [True]


def test_protected_binding_derives_current_run_without_exporting_tokens(tmp_path):
    from tests.e2e.tools import atlassian_authority_binding as binding

    release = tmp_path / "release.json"
    release.write_text(json.dumps({"source_revision": BINDING["source_revision"]}))
    release.chmod(0o600)
    doc = manifest()
    del doc["binding"]
    for spec in doc["clients"].values():
        spec["probes"] = dict.fromkeys(authority.CASES)
    environment = {
        "ADP_TOOLS_RELEASE_MANIFEST_FILE": str(release),
        "SOURCE_REVISION": BINDING["source_revision"],
        "GITHUB_SHA": BINDING["definition_revision"],
        "GITHUB_RUN_ID": "303",
        "GITHUB_RUN_ATTEMPT": "1",
        "ADP_TOOLS_ATLASSIAN_AUTHORITY_JSON": json.dumps(doc),
        "ADP_TOOLS_ATLASSIAN_AUTHORITY_SESSIONS_JSON": json.dumps(
            {"claude-code": "private-a", "codex": "private-b"}
        ),
        "RUNNER_TEMP": str(tmp_path),
        "GATEWAY_URL": GATEWAY,
        "EXPECTED_ACCOUNT": ACCOUNT,
        "AWS_REGION": REGION,
        "ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN": ROLE,
    }
    path = binding.materialize(environment)
    raw = path.read_text()
    assert json.loads(raw)["binding"] == binding.release_binding(environment)
    assert "private-a" not in raw and "private-b" not in raw
    assert (path.parent / "aws-config").stat().st_mode & 0o077 == 0
    assert (path.parent / "claude-code/session.token").read_text() == "private-a"


def test_authority_selection_does_not_require_success_only_native_sessions():
    from tests.e2e.tools.atlassian_scenario_selection import selection_flags

    flags = selection_flags("SCN-8454-03")
    assert flags["atlassian_authority"] and flags["atlassian_clients"]
    assert not flags["atlassian_native"]
