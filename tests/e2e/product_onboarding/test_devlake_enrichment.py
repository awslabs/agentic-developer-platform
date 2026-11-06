"""SCN-01 local setup contract; a served two-provider browser run remains pending."""

from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.devlake_profile_setup import assert_profile_setup, run_profile_setup


@pytest.fixture
def setup_case():
    fixture = {"tenant_id": "owned-tenant", "product_id": "owned-product", "project_id": "owned-project",
               "source_revision": "a" * 40, "revision": 1}
    native = {"github": {"namespace": "team", "id": "35", "name": "release"},
              "jira": {"namespace": "fixture-instance", "id": "12", "name": "Planning"}}
    bindings = [
        {"id": "owned-github", "project_id": "owned-project", "provider": "github", "generation": 1,
         "connection_ref": {"kind": "github_installation", "id": "shared-github"}, "status": "healthy",
         "selection": {"kind": "repository", "provider": "github", "namespace": "team", "native_id": "35"}},
        {"id": "owned-jira", "project_id": "owned-project", "provider": "jira", "generation": 1,
         "connection_ref": {"kind": "credential", "id": "shared-jira"}, "status": "healthy",
         "selection": {"kind": "tracker", "provider": "jira", "namespace": "fixture-instance", "native_id": "12"}},
    ]
    browser = {"product_id": "owned-product", "project_id": "owned-project", "session_id": "owned-session",
               "selected": {"github": "owned-github", "jira": "owned-jira"},
               "operations": {"github": "github-operation", "jira": "jira-operation"},
               "credential_entries": 0, "second_setup_prompts": 0, "complete": True,
               "coverage": {"github": "complete", "jira": "complete"}}
    from hashlib import sha256

    basic, provenance = {}, []
    for binding in bindings:
        provider = binding["provider"]
        kind, field = ("repository", "repositories") if provider == "github" else ("tracker", "trackers")
        source = native[provider]
        digest = sha256("\x00".join((kind, source["namespace"], source["id"], source["name"])).encode()).hexdigest()
        reference = {"tenant_id": fixture["tenant_id"], "project_id": fixture["project_id"],
                     "source_connection_id": binding["connection_ref"]["id"], "record_type": kind,
                     "record_id": source["id"], "record_revision": "observation-sha256:" + digest}
        basic[field] = [reference]
        provenance.append({"paths": ["/configuration/basic/" + field], "state": "accepted", "method": "connector",
                           "source_refs": [reference], "binding_id": binding["id"], "binding_generation": 1,
                           "field_map_version": "devlake-profile-v1", "coverage": "complete",
                           "revision_basis": "observation_only", "observed_at": "2026-10-20T00:00:00Z"})
    profile = {**fixture, "revision": 5, "configuration": {"basic": basic}, "provenance": provenance}
    return fixture, browser, native, bindings, profile


def local_ports(setup_case, *, fail_browser=False):
    fixture, browser, native, bindings, profile = deepcopy(setup_case)
    state = {"bindings": [], "profile": {**fixture, "configuration": {}, "provenance": []},
             "connections": ("shared-github", "shared-jira"), "cleaned": []}

    def browser_setup(owned):
        assert owned == fixture
        state["bindings"] = bindings
        if fail_browser:
            raise ConnectionError("browser response lost after saving both bindings")
        state["profile"] = profile
        return browser

    def unlink_owned(ids):
        state["cleaned"].append(ids)
        state["bindings"] = []
        return {"unlinked_binding_ids": ids, "remaining_owned_binding_ids": [],
                "shared_connections_retained": True}

    ports = {"verify_assets": lambda: None, "read_profile": lambda: deepcopy(state["profile"]),
             "read_bindings": lambda: deepcopy(state["bindings"]), "read_native": lambda: deepcopy(native),
             "read_connections": lambda: state["connections"], "browser_setup": browser_setup,
             "unlink_owned": unlink_owned}
    return fixture, ports, state


def test_local_profile_setup_fixture_and_independent_readback(setup_case):
    fixture, ports, state = local_ports(setup_case)
    result = run_profile_setup(fixture, ports)
    assert result["profile_revision"] == 5 and result["session_id"] == "owned-session"
    assert state["cleaned"] == [{"owned-github", "owned-jira"}]
    assert state["connections"] == ("shared-github", "shared-jira")


@pytest.mark.parametrize("failure", ["missing_jira", "wrong_native", "no_provenance", "false_coverage",
                                      "foreign_product", "credential_prompt", "missing_operation", "duplicate_binding", "false_browser_complete"])
def test_local_profile_setup_rejects_false_green(setup_case, failure):
    fixture, browser, native, bindings, profile = deepcopy(setup_case)
    if failure == "missing_jira":
        del native["jira"]
    elif failure == "wrong_native":
        native["github"]["id"] = "36"
    elif failure == "no_provenance":
        profile["provenance"] = []
    elif failure == "false_coverage":
        profile["provenance"][0]["coverage"] = "partial"
    elif failure == "foreign_product":
        profile["product_id"] = "another-product"
    elif failure == "credential_prompt":
        browser["credential_entries"] = 1
    elif failure == "missing_operation":
        del browser["operations"]["jira"]
    elif failure == "duplicate_binding":
        bindings[1]["id"] = bindings[0]["id"]
    else:
        browser["complete"] = False
    cleanup = {"unlinked_binding_ids": {"owned-github", "owned-jira"},
               "remaining_owned_binding_ids": [], "shared_connections_retained": True}
    with pytest.raises((AssertionError, KeyError, StopIteration)):
        assert_profile_setup(fixture, browser, native, bindings, profile, cleanup)


def test_local_profile_setup_retires_only_owned_bindings_after_interruption(setup_case):
    fixture, ports, state = local_ports(setup_case, fail_browser=True)
    with pytest.raises(ConnectionError, match="response lost"):
        run_profile_setup(fixture, ports)
    assert state["cleaned"] == [{"owned-github", "owned-jira"}]
    assert state["bindings"] == []


def test_local_profile_setup_refuses_false_cleanup_or_missing_served_assets(setup_case):
    fixture, ports, state = local_ports(setup_case)
    ports["verify_assets"] = lambda: (_ for _ in ()).throw(AssertionError("wrong served revision"))
    with pytest.raises(AssertionError, match="wrong served revision"):
        run_profile_setup(fixture, ports)
    assert state["cleaned"] == [] and state["bindings"] == []

    fixture, ports, state = local_ports(setup_case)
    ports["unlink_owned"] = lambda ids: {"unlinked_binding_ids": ids,
                                           "remaining_owned_binding_ids": [], "shared_connections_retained": True}
    with pytest.raises(AssertionError):
        run_profile_setup(fixture, ports)
    assert {binding["id"] for binding in state["bindings"]} == {"owned-github", "owned-jira"}


@pytest.mark.live_only
def test_profile_setup(request):
    """SCN-01 live selector: requires the owned fixture and S10's authenticated browser/provider ports."""
    import os

    if os.environ.get("S11_PROFILE_SETUP_LIVE_ENABLED") != "1":
        pytest.skip("SCN-01 needs an authorized served UI and two S10 provider fixtures")
    from tests.e2e.product_onboarding.test_product_v06_live import owned_source_run

    def profile_setup_ports(provider, fixture, directory, *, page):
        from tests.e2e.product_onboarding.product_v06_live import SourcePrerequisite

        builder = getattr(provider, "s11_profile_setup_ports", None)
        if not callable(builder):
            raise SourcePrerequisite("S10 must publish authenticated GitHub/Jira source oracles and browser setup ports")
        return builder(fixture, directory, page=page)

    for variable in ("PRODUCT_EXPECTED_REVISION", "E2E_SCREENSHOT_DIR"):
        if not os.environ.get(variable):
            raise ValueError("SCN-01 needs " + variable + " from the authorized pinned workflow")
    with owned_source_run(request, builder=profile_setup_ports, report="s11-profile-setup") as (fixture, ports, _):
        result = run_profile_setup(fixture, ports)
        assert result["profile_revision"] > fixture["revision"]
        assert result["cleanup"]["remaining_owned_binding_ids"] == []


@pytest.fixture
def refresh_case(setup_case):
    from hashlib import sha256

    owned, _, _, bindings, filled = deepcopy(setup_case)
    manual = {"tenant_id": owned["tenant_id"], "project_id": owned["project_id"],
              "source_connection_id": "confirmed-private-source", "record_type": "repository",
              "record_id": "confirmed-private-record", "record_revision": "manual-v1"}
    initial = {**owned, "revision": 2, "product": {"purpose": "Original purpose"},
               "configuration": {"basic": {"repositories": [manual]}},
               "provenance": [{"paths": ["/configuration/basic/repositories"], "state": "accepted", "method": "manual"}]}
    tracker_proof = filled["provenance"][1]
    after_initial = {**initial, "revision": 3,
                     "configuration": {"basic": {"repositories": [manual], "trackers": filled["configuration"]["basic"]["trackers"]}},
                     "provenance": [initial["provenance"][0], tracker_proof]}
    after_edit = {**after_initial, "revision": 4, "product": {"purpose": "Concurrent peer edit"}}
    source = {"kind": "repository", "namespace": "team", "native_id": "35", "name": "release",
              "binding_id": bindings[0]["id"], "generation": 1, "observed_at": "2026-10-20T00:00:00Z"}
    updated = {**source, "name": "release-v2"}
    for candidate in (source, updated):
        candidate["revision"] = "observation-sha256:" + sha256("\x00".join(
            (candidate["kind"], candidate["namespace"], candidate["native_id"], candidate["name"])).encode()).hexdigest()
    fixture = {**owned, "revision": 2, "source_before": source["revision"], "source_after": updated["revision"],
               "observed_at": updated["observed_at"], "binding_id": bindings[0]["id"], "binding_generation": 1,
               "tracker_binding_id": bindings[1]["id"], "tracker_generation": 1,
               "tracker_revision": filled["configuration"]["basic"]["trackers"][0]["record_revision"],
               "keys": {name: "owned_refresh_" + name + "_123" for name in ("initial", "stale", "old", "fresh")}}
    first = {"status": 200, "revision": 3, "coverage": "complete", "applied": [tracker_proof],
             "suggestions": [], "conflicts": [{"field_path": "/configuration/basic/repositories", "reason": "confirmed_or_changed"}]}
    fresh = {"status": 200, "revision": 4, "coverage": "complete", "applied": [], "suggestions": [],
             "conflicts": [{"field_path": "/configuration/basic/" + field, "reason": "confirmed_or_changed"}
                           for field in ("repositories", "trackers")]}
    return fixture, initial, after_initial, after_edit, source, updated, first, fresh


def refresh_ports(refresh_case, *, interrupt=False):
    fixture, initial, after_initial, after_edit, source, updated, first, fresh = deepcopy(refresh_case)
    state = {"profile": initial, "source": source, "calls": [], "cleanup": [],
             "bindings": [{"id": fixture["binding_id"], "status": "healthy"},
                          {"id": fixture["tracker_binding_id"], "status": "healthy"}],
             "connections": ("shared-github", "shared-jira")}

    def apply(revision, source_revision, key):
        name = next(name for name, value in fixture["keys"].items() if value == key)
        state["calls"].append((name, revision, source_revision, key))
        if name == "initial":
            assert revision == 2 and source_revision == source["revision"]
            state["profile"] = after_initial
            if interrupt:
                raise ConnectionError("first apply committed before reply")
            return first
        if name == "stale":
            assert revision == 3 and state["profile"]["revision"] == 4
            return {"status": 412, "code": "stale_revision"}
        if name == "old":
            assert revision == 4 and source_revision != state["source"]["revision"]
            return {"status": 409, "code": "source_changed"}
        assert revision == 4 and source_revision == updated["revision"]
        return fresh

    def concurrent_edit(revision):
        assert revision == 3
        state["profile"] = after_edit

    def refresh_source():
        state["source"] = updated
        return deepcopy(updated)

    def unlink_owned(binding_ids):
        state["cleanup"].append(binding_ids)
        state["bindings"] = [{**binding, "status": "unlinked"} for binding in state["bindings"]]
        return {"unlinked_binding_ids": binding_ids, "remaining_owned_binding_ids": [], "shared_connections_retained": True}

    ports = {"verify_assets": lambda: None, "read_profile": lambda: deepcopy(state["profile"]),
             "read_source": lambda: deepcopy(state["source"]), "read_bindings": lambda: deepcopy(state["bindings"]),
             "read_connections": lambda: state["connections"], "apply": apply, "concurrent_edit": concurrent_edit,
             "refresh_source": refresh_source, "unlink_owned": unlink_owned}
    return fixture, ports, state


def test_local_refresh_conflicts_pins_revisions_and_cleans_owned_bindings(refresh_case):
    from tests.e2e.product_onboarding.devlake_refresh_conflicts import run_refresh_conflicts

    fixture, ports, state = refresh_ports(refresh_case)
    report = run_refresh_conflicts(fixture, ports)
    assert [call[0] for call in state["calls"]] == ["initial", "stale", "old", "fresh"]
    assert state["cleanup"] == [{"owned-github", "owned-jira"}]
    assert report["after_stale"] == report["after_edit"]


@pytest.mark.parametrize("fault", ["overwrite_manual", "lost_provenance", "untyped_ref", "wrong_clock", "false_stale",
                                   "stale_applied", "leaked_private", "wrong_revision", "unchanged_source", "missing_conflict",
                                   "no_cleanup", "wrong_generation", "unconfirmed_edit", "false_cleanup_readback"])
def test_local_refresh_conflicts_rejects_false_green(refresh_case, fault):
    from tests.e2e.product_onboarding.devlake_refresh_conflicts import assert_refresh_conflicts, run_refresh_conflicts

    fixture, ports, _ = refresh_ports(refresh_case)
    report = run_refresh_conflicts(fixture, ports)
    if fault == "overwrite_manual":
        report["after_edit"]["configuration"]["basic"]["repositories"] = [{"record_id": "overwritten"}]
    elif fault == "lost_provenance":
        report["after_initial"]["provenance"].pop()
    elif fault == "untyped_ref":
        report["after_initial"]["configuration"]["basic"]["trackers"][0]["record_type"] = "unknown"
    elif fault == "wrong_clock":
        report["native_after"]["observed_at"] = "2026-10-21T00:00:00Z"
    elif fault == "false_stale":
        report["stale"] = {"status": 200, "revision": 4}
    elif fault == "stale_applied":
        report["after_stale"]["revision"] = 5
    elif fault == "leaked_private":
        report["fresh"]["conflicts"][0]["current_value"] = "confirmed-private-record"
    elif fault == "wrong_revision":
        report["after_initial"]["revision"] = 4
    elif fault == "unchanged_source":
        report["native_after"]["revision"] = report["native_before"]["revision"]
    elif fault == "missing_conflict":
        report["fresh"]["conflicts"].pop()
    elif fault == "no_cleanup":
        report["cleanup"]["remaining_owned_binding_ids"] = ["owned-github"]
    elif fault == "wrong_generation":
        report["native_after"]["generation"] = 2
    elif fault == "unconfirmed_edit":
        report["after_edit"]["product"]["purpose"] = "Original purpose"
    else:
        report["bindings_after"][0]["status"] = "healthy"
    with pytest.raises(AssertionError):
        assert_refresh_conflicts(fixture, report)


def test_local_refresh_conflicts_cleans_up_after_uncertain_apply(refresh_case):
    from tests.e2e.product_onboarding.devlake_refresh_conflicts import run_refresh_conflicts

    fixture, ports, state = refresh_ports(refresh_case, interrupt=True)
    with pytest.raises(ConnectionError, match="committed before reply"):
        run_refresh_conflicts(fixture, ports)
    assert state["cleanup"] == [{"owned-github", "owned-jira"}]


@pytest.mark.live_only
def test_refresh_conflicts(request):
    """SCN-02 live selector: requires owned revisions and S10 provider/browser ports."""
    import os
    from pathlib import Path

    if os.environ.get("S11_REFRESH_CONFLICTS_LIVE_ENABLED") != "1":
        pytest.skip("SCN-02 needs an authorized served target and pinned provider revisions")
    from tests.e2e.new_ui.product_release import check_served_assets, private_capture_directory
    from tests.e2e.new_ui.test_product_resume import checked_fixture
    from tests.e2e.product_onboarding.devlake_refresh_conflicts import run_refresh_conflicts
    from tests.e2e.product_onboarding.product_v02_record import CaseEvidence
    from tests.e2e.product_onboarding.product_v06_live import SourcePrerequisite

    for variable in ("PRODUCT_EXPECTED_REVISION", "E2E_SCREENSHOT_DIR"):
        if not os.environ.get(variable):
            raise ValueError("SCN-02 needs " + variable + " from the authorized pinned workflow")
    provider = request.getfixturevalue("product_onboarding_live_provider")
    page = request.getfixturevalue("product_onboarding_live_page")
    record = CaseEvidence()
    directory = Path(private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"]))
    with provider.fixture("conflict", record) as owned:
        checked_fixture(owned, os.environ["PRODUCT_EXPECTED_REVISION"])
        check_served_assets(provider.request, provider.base_url, owned["frontend_release"], owned["source_revision"])
        builder = getattr(provider, "s11_refresh_conflicts_ports", None)
        if not callable(builder):
            raise SourcePrerequisite("S10 must publish authenticated source revision and concurrent-edit ports")
        fixture, ports = builder(owned, directory, page=page)
        report = run_refresh_conflicts(fixture, ports)
        assert report["stale"]["status"] == 412 and report["old"]["status"] == 409


@pytest.fixture
def denial_case():
    fixture = {"tenant_id": "owned-tenant", "product_id": "owned-product", "project_id": "owned-project",
               "session_id": "owned-session", "binding_id": "owned-binding", "shared_binding_id": "shared-binding",
               "operation_id": "partial-operation", "generation": 1, "revision": 2,
               "retry_key": "owned_retry_request_123", "unlink_key": "owned_unlink_request_123"}
    native = {"provider": "github", "namespace": "team", "native_id": "35", "revision": "native-rev"}
    return fixture, native


def denial_ports(denial_case, *, fault=None):
    fixture, native = deepcopy(denial_case)
    state = {"phase": "partial", "retry_calls": [], "unlink_calls": [], "cleaned": [], "revision": 2,
             "bindings": {"owned-binding": {"id": "owned-binding", "status": "healthy", "generation": 1},
                          "shared-binding": {"id": "shared-binding", "status": "healthy", "generation": 1}},
             "connections": ("shared-github", "shared-jira")}

    def read_session(surface):
        first = {"id": "partial-operation", "state": "succeeded", "coverage": "partial",
                 "complete": False, "resource_count": 1, "cursor": "partial-operation", "binding_ids": ["owned-binding"]}
        if state["phase"] == "denied":
            first = {"id": "partial-operation", "state": "cancelled", "coverage": "denied",
                     "complete": False, "resource_count": 0}
            if fault == "leaked_cursor":
                first["cursor"] = "partial-operation"
            if fault == "leaked_resource":
                first["resources"] = [{"native_id": "private-record"}]
            if fault == "false_complete" and surface == "browser":
                first["complete"] = True
        if fault == "partial_complete" and state["phase"] == "partial":
            first["complete"] = True
        entries = [first]
        if state["retry_calls"]:
            entries.insert(0, {"id": "retried-operation", "state": "succeeded", "coverage": "complete",
                               "complete": True, "resource_count": 2} if state["phase"] != "denied" else
                           {"id": "retried-operation", "state": "cancelled", "coverage": "denied",
                            "complete": False, "resource_count": 0})
        if fault == "stale_retry_completion" and state["phase"] == "denied":
            entries[0] = {"id": "retried-operation", "state": "succeeded", "coverage": "complete",
                          "complete": True, "resource_count": 2}
        if fault == "duplicate_operation" and len(state["retry_calls"]) > 1 and state["phase"] == "partial":
            entries.insert(0, {**entries[0], "id": "duplicated-operation"})
        view = {**fixture, "id": "owned-session", "revision": state["revision"], "discoveries": entries}
        if surface != "api":
            view["retry_available"] = state["phase"] != "denied" or fault == "stale_retry_control"
        return deepcopy(view)

    def read_operation(operation_id):
        assert operation_id == "partial-operation"
        if state["phase"] == "denied":
            return {"state": "cancelled", "coverage": "denied", "resources": []}
        return {"state": "succeeded", "coverage": "partial", "cursor": operation_id,
                "resources": [{"binding_id": "owned-binding", "native_id": native["native_id"]}]}

    def retry(operation_id, key, revision):
        assert (operation_id, key, revision) == ("partial-operation", fixture["retry_key"], 2)
        state["retry_calls"].append(key)
        if fault == "duplicate_binding" and len(state["retry_calls"]) == 1:
            state["bindings"]["unexpected-binding"] = {"id": "unexpected-binding", "status": "healthy", "generation": 1}
        if len(state["retry_calls"]) == 1:
            raise ConnectionError("discovery committed before response")
        return {"id": "retried-operation", "coverage": "complete", "state": "succeeded"}

    def unlink(binding_id, key, revision):
        assert (binding_id, key, revision) == (fixture["binding_id"], fixture["unlink_key"], 2)
        state["unlink_calls"].append(key)
        if len(state["unlink_calls"]) == 1:
            state["bindings"]["owned-binding"] = {"id": "owned-binding", "status": "unlinked", "generation": 2}
            state["revision"] = 3
            if fault == "shared_deleted":
                state["bindings"]["shared-binding"]["status"] = "unlinked"
            raise ConnectionError("unlink committed before response")
        return deepcopy(state["bindings"]["owned-binding"])

    def cleanup_owned(binding_ids):
        state["cleaned"].append(binding_ids)
        state["bindings"]["owned-binding"] = {"id": "owned-binding", "status": "unlinked", "generation": 2}
        return {"unlinked_binding_ids": binding_ids, "remaining_owned_binding_ids": [], "shared_connections_retained": True}

    def save_profile(revision):
        assert revision == state["revision"]
        state["revision"] += 1
        return {"revision": state["revision"]}

    ports = {"verify_assets": lambda: None, "read_session": read_session, "read_operation": read_operation,
             "read_bindings": lambda: deepcopy(list(state["bindings"].values())), "read_source": lambda: deepcopy(native),
             "read_connections": lambda: state["connections"], "retry": retry, "revoke": lambda: state.update(phase="denied"),
             "unlink": unlink, "save_profile": save_profile, "read_profile": lambda: {"revision": state["revision"]},
             "cleanup_owned": cleanup_owned}
    return fixture, ports, state


def test_local_resume_and_denial_agrees_across_clients_and_replays_receipts(denial_case):
    from tests.e2e.product_onboarding.devlake_resume_denial import run_resume_denial

    fixture, ports, state = denial_ports(denial_case)
    report = run_resume_denial(fixture, ports)
    assert report["saved"]["revision"] == 4
    assert state["retry_calls"] == [fixture["retry_key"]] * 2
    assert state["unlink_calls"] == [fixture["unlink_key"]] * 2
    assert state["cleaned"] == [{"owned-binding"}]
    assert state["bindings"]["shared-binding"]["status"] == "healthy"


@pytest.mark.parametrize("fault", ["leaked_cursor", "leaked_resource", "false_complete", "partial_complete",
                                   "stale_retry_control", "stale_retry_completion", "duplicate_operation",
                                   "duplicate_binding", "shared_deleted"])
def test_local_resume_and_denial_rejects_false_green(denial_case, fault):
    from tests.e2e.product_onboarding.devlake_resume_denial import run_resume_denial

    fixture, ports, state = denial_ports(denial_case, fault=fault)
    with pytest.raises(AssertionError):
        run_resume_denial(fixture, ports)
    assert state["cleaned"] == [{"owned-binding"}]


@pytest.mark.live_only
def test_resume_and_denial(request):
    """SCN-03 live selector: requires pinned served assets, installed CLI and S10 recovery ports."""
    import os
    from pathlib import Path

    if os.environ.get("S11_RESUME_DENIAL_LIVE_ENABLED") != "1":
        pytest.skip("SCN-03 needs served UI, installed CLI and an authorized revocable source fixture")
    from tests.e2e.new_ui.product_release import check_served_assets, private_capture_directory
    from tests.e2e.new_ui.test_product_resume import checked_fixture
    from tests.e2e.product_onboarding.devlake_resume_denial import run_resume_denial
    from tests.e2e.product_onboarding.product_v02_record import CaseEvidence
    from tests.e2e.product_onboarding.product_v06_live import SourcePrerequisite

    for variable in ("PRODUCT_EXPECTED_REVISION", "E2E_SCREENSHOT_DIR", "PRODUCT_RESUME_CLI_PATH"):
        if not os.environ.get(variable):
            raise ValueError("SCN-03 needs " + variable + " from the authorized pinned workflow")
    provider = request.getfixturevalue("product_onboarding_live_provider")
    page = request.getfixturevalue("product_onboarding_live_page")
    record = CaseEvidence()
    directory = Path(private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"]))
    with provider.fixture("recovery", record) as owned:
        checked_fixture(owned, os.environ["PRODUCT_EXPECTED_REVISION"])
        check_served_assets(provider.request, provider.base_url, owned["frontend_release"], owned["source_revision"])
        builder = getattr(provider, "s11_resume_denial_ports", None)
        if not callable(builder):
            raise SourcePrerequisite("S10 must publish authorized partial-page, revocation and installed-client ports")
        fixture, ports = builder(owned, directory, page=page)
        report = run_resume_denial(fixture, ports)
        assert report["saved"]["revision"] == fixture["revision"] + 2
        assert report["cleanup"]["remaining_owned_binding_ids"] == []


def optional_ports(setup_case, *, fault=None):
    fixture, browser, native, bindings, source_profile = deepcopy(setup_case)
    fixture.update(confirmed_purpose="Approved roadmap", confirmed_owners=["product-team"], new_name="New title")
    saved = {**fixture, "product": {"purpose": fixture["confirmed_purpose"], "name": "Original title"},
             "configuration": {"basic": {"owners": fixture["confirmed_owners"]}}, "provenance": []}
    state = {"mode": "missing", "profile": saved, "bindings": [], "connections": ("shared-github", "shared-jira"),
             "collection": [], "cleaned": [], "discoveries": 0}

    def read_features():
        if fault == "off_feature" and state["mode"] == "off":
            return {"devlake_connectors": True}
        return {} if state["mode"] == "missing" else {"devlake_connectors": state["mode"] == "enabled"}

    def read_clients():
        available = state["mode"] == "enabled" or fault == "off_actions"
        return {"browser": {"connector_available": available, "native_available": True,
                            "profile_edit_available": True},
                "installed_cli": {"connector_available": available, "native_available": True,
                                  "profile_edit_available": True},
                "capability_ready": True, "permission_granted": True}

    def edit_profile(revision):
        assert revision == fixture["revision"]
        state["profile"] = deepcopy({**state["profile"], "revision": revision + 1,
                                     "product": {"purpose": fixture["confirmed_purpose"],
                                                 "name": fixture["new_name"]}})
        return deepcopy(state["profile"])

    def discover(owned):
        assert owned == fixture and state["mode"] == "enabled"
        state["discoveries"] += 1
        state["bindings"] = bindings
        if fault == "interrupted":
            raise ConnectionError("response lost after binding creation")
        profile = deepcopy(source_profile)
        profile["product"] = deepcopy(state["profile"]["product"])
        profile["configuration"]["basic"]["owners"] = fixture["confirmed_owners"]
        if fault == "lost_decision":
            profile["configuration"]["basic"]["owners"] = ["discovered-team"]
        if fault == "lost_edit":
            profile["product"]["name"] = "Original title"
        if fault == "no_provenance":
            profile["provenance"] = []
        if fault == "collection_used":
            state["collection"] = ["insights-collection"]
        if fault == "setup_prompt":
            browser["second_setup_prompts"] = 1
        state["profile"] = profile
        return browser

    def unlink_owned(ids):
        state["cleaned"].append(ids)
        state["bindings"] = []
        if fault == "shared_deleted":
            state["connections"] = ()
        return {"unlinked_binding_ids": ids, "remaining_owned_binding_ids": [],
                "shared_connections_retained": fault != "false_cleanup"}

    ports = {"verify_assets": lambda: None, "read_profile": lambda: deepcopy(state["profile"]),
             "read_native": lambda: deepcopy(native), "read_bindings": lambda: deepcopy(state["bindings"]),
             "read_connections": lambda: state["connections"], "read_collection": lambda: deepcopy(state["collection"]),
             "select_config": lambda mode: state.update(mode=mode), "read_features": read_features,
             "read_clients": read_clients, "edit_profile": edit_profile,
             "discover": discover, "unlink_owned": unlink_owned}
    return fixture, ports, state


def test_local_optional_connectors_keep_native_profile_and_collection_absent(setup_case):
    from tests.e2e.product_onboarding.devlake_optional_connectors import run_optional_connectors

    fixture, ports, state = optional_ports(setup_case)
    report = run_optional_connectors(fixture, ports)
    assert report["profile_revision"] == 5
    assert state["discoveries"] == 1 and state["cleaned"] == [{"owned-github", "owned-jira"}]
    assert state["profile"]["configuration"]["basic"]["owners"] == ["product-team"]
    assert state["collection"] == []


@pytest.mark.parametrize("fault", ["off_actions", "off_feature", "lost_decision", "lost_edit", "no_provenance",
                                   "collection_used", "setup_prompt", "false_cleanup", "shared_deleted"])
def test_local_optional_connectors_reject_false_green(setup_case, fault):
    from tests.e2e.product_onboarding.devlake_optional_connectors import run_optional_connectors

    fixture, ports, state = optional_ports(setup_case, fault=fault)
    with pytest.raises(AssertionError):
        run_optional_connectors(fixture, ports)
    assert state["cleaned"] == ([set()] if fault in ("off_actions", "off_feature") else
                                [{"owned-github", "owned-jira"}])


def test_local_optional_connectors_cleanup_after_lost_response(setup_case):
    from tests.e2e.product_onboarding.devlake_optional_connectors import run_optional_connectors

    fixture, ports, state = optional_ports(setup_case, fault="interrupted")
    with pytest.raises(ConnectionError, match="response lost"):
        run_optional_connectors(fixture, ports)
    assert state["cleaned"] == [{"owned-github", "owned-jira"}]
    assert state["connections"] == ("shared-github", "shared-jira")


@pytest.mark.live_only
def test_optional_connectors(request):
    """SCN-04 live selector requires pre-authorized pinned off/on targets, not deployment mutations."""
    import os
    from pathlib import Path

    if os.environ.get("S11_OPTIONAL_CONNECTORS_LIVE_ENABLED") != "1":
        pytest.skip("SCN-04 needs authorized served feature controls and connector-only source fixture")
    from tests.e2e.new_ui.product_release import check_served_assets, private_capture_directory
    from tests.e2e.new_ui.test_product_resume import checked_fixture
    from tests.e2e.product_onboarding.devlake_optional_connectors import run_optional_connectors
    from tests.e2e.product_onboarding.product_v02_record import CaseEvidence
    from tests.e2e.product_onboarding.product_v06_live import SourcePrerequisite

    for variable in ("PRODUCT_EXPECTED_REVISION", "E2E_SCREENSHOT_DIR", "PRODUCT_RESUME_CLI_PATH"):
        if not os.environ.get(variable):
            raise ValueError("SCN-04 needs " + variable + " from the authorized pinned workflow")
    provider = request.getfixturevalue("product_onboarding_live_provider")
    page = request.getfixturevalue("product_onboarding_live_page")
    record = CaseEvidence()
    directory = Path(private_capture_directory(os.environ["E2E_SCREENSHOT_DIR"]))
    with provider.fixture("optional", record) as owned:
        checked_fixture(owned, os.environ["PRODUCT_EXPECTED_REVISION"])
        check_served_assets(provider.request, provider.base_url, owned["frontend_release"], owned["source_revision"])
        builder = getattr(provider, "s11_optional_connectors_ports", None)
        if not callable(builder):
            raise SourcePrerequisite("S10 must publish served feature toggles, provider oracle and collection-absence ports")
        fixture, ports = builder(owned, directory, page=page)
        report = run_optional_connectors(fixture, ports)
        assert report["cleanup"]["remaining_owned_binding_ids"] == []
