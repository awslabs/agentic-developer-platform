"""Pinned C1 read-only browser probes and review-only retirement fixtures."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_demo1_cli import fixture_documents, identifier, run_cli, write_private

from superplane_acceptance.demo1_c1 import (
    inspect_original_details,
    inspect_reentry,
    inspect_retirement_review,
    inspect_review,
    retirement_preview,
)
from superplane_acceptance.demo1_evidence import DemoInput, EvidenceError


def preview_body(selected, phase, retirement_id):
    return {
        "request_id": retirement_id,
        "workspace_id": phase["workspace_id"],
        "source_operation_id": phase["operation_id"],
        "source_payload_digest": "f" * 64,
        "lifecycle_artifact_id": "artifact-1",
        "account_id": selected["account"],
        "region": selected["region"],
        "inventory_sha256": "a" * 64,
        "lifecycle_policy_sha256": "b" * 64,
        "runtime_config_sha256": "c" * 64,
        "steps": [
            {
                "step_id": "delete-cluster",
                "provider": "aws",
                "operation_kind": "delete",
                "target": "example-owned-cluster",
            }
        ],
        "preserved": ["example-shared-vpc"],
        "revision": "d" * 64,
        "admission_available": False,
        "blocked_reason": "staged_cleanup_access_required",
        "approval_request": None,
    }


def test_pinned_playwright_roles_probe_review_and_refresh_without_submitting():
    calls = []

    class Locator:
        def wait_for(self, *, state, timeout):
            assert (state, timeout) == ("visible", 5000)

        def is_visible(self):
            return True

        def click(self):
            calls.append(("select",))

    def get_by_role(role, *, name, exact=False):
        calls.append((role, name, exact))
        return Locator()

    page = SimpleNamespace(
        get_by_role=get_by_role, reload=lambda: calls.append(("reload",))
    )
    assert inspect_review(page)["status"] == "BLOCKED"
    assert inspect_reentry(page, "example-workspace")["status"] == "BLOCKED"
    assert ("group", "Review this plan", False) in calls
    assert ("region", "Review an operation approval", False) in calls
    assert calls.count(("button", "example-workspace", False)) == 2
    assert calls.count(("region", "Readiness", False)) == 2
    assert calls.count(("reload",)) == 1
    assert all("removal" not in str(call).lower() for call in calls)


def test_missing_workspace_after_refresh_stays_blocked():
    reloaded = []

    def get_by_role(role, *, name, exact=False):
        return SimpleNamespace(
            wait_for=lambda **kwargs: None,
            is_visible=lambda: not (name == "example-workspace" and reloaded),
            click=lambda: None,
        )

    page = SimpleNamespace(
        get_by_role=get_by_role, reload=lambda: reloaded.append(True)
    )
    result = inspect_reentry(page, "example-workspace")
    assert result["status"] == "BLOCKED"
    assert "refresh" in result["reason"]


@pytest.mark.parametrize("loading_phase", ["initial", "reloaded"])
@pytest.mark.parametrize("loading_role", ["heading", "button", "region"])
@pytest.mark.parametrize("times_out", [False, True])
def test_reentry_waits_for_initial_and_reloaded_landmarks(
    loading_phase, loading_role, times_out
):
    state = {"phase": "initial", "loaded": False, "reloads": 0}
    selections = []
    waits = []

    class Locator:
        def __init__(self, role):
            self.role = role

        def is_visible(self):
            return not (
                state["phase"] == loading_phase
                and self.role == loading_role
                and not state["loaded"]
            )

        def wait_for(self, *, state, timeout):
            assert (state, timeout) == ("visible", 5000)
            waits.append((current_phase(), self.role))
            if not self.is_visible():
                if times_out:
                    raise TimeoutError("private delayed workspace response")
                finish_loading()

        def click(self):
            assert self.role == "button" and self.is_visible()
            selections.append(state["phase"])

    def current_phase():
        return state["phase"]

    def finish_loading():
        state["loaded"] = True

    def get_by_role(role, *, name, exact=False):
        names = {
            "heading": "Workspaces",
            "button": "example-workspace",
            "region": "Readiness",
        }
        assert name == names[role]
        return Locator(role)

    def reload():
        assert selections == ["initial"]
        state["phase"] = "reloaded"
        state["reloads"] += 1

    result = inspect_reentry(
        SimpleNamespace(get_by_role=get_by_role, reload=reload), "example-workspace"
    )
    assert result["status"] == "BLOCKED"
    assert ("read-only re-entry visible" in result["reason"]) is not times_out
    assert "private delayed workspace response" not in str(result)
    expected_waits = [
        (phase, role)
        for phase in ("initial", "reloaded")
        for role in ("heading", "button", "region")
    ]
    if times_out:
        stopped_at = expected_waits.index((loading_phase, loading_role))
        expected_waits = expected_waits[: stopped_at + 1]
    assert waits == expected_waits
    expected_selections = [phase for phase, role in expected_waits if role == "region"]
    assert selections == expected_selections
    assert state["reloads"] == (0 if times_out and loading_phase == "initial" else 1)


@pytest.mark.parametrize("missing_after_refresh", [False, True])
@pytest.mark.parametrize("missing_identity", ["workspace", "operation", "request"])
def test_server_backed_reentry_checks_original_ids_after_details_refresh(
    missing_after_refresh, missing_identity
):
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    calls = []
    state = {"refreshes": 0}
    identities = {phase["workspace_id"], phase["operation_id"], selected["request_id"]}
    missing = {
        "workspace": phase["workspace_id"],
        "operation": phase["operation_id"],
        "request": selected["request_id"],
    }[missing_identity]

    def get_by_role(role, *, name, exact=False):
        calls.append((role, name))
        if name == "Workspace details":
            return SimpleNamespace(
                wait_for=lambda **kwargs: None,
                is_visible=lambda: True,
                get_by_text=lambda value, exact: SimpleNamespace(
                    wait_for=lambda **kwargs: None,
                    is_visible=lambda: (
                        value in identities
                        and not (
                            missing_after_refresh
                            and state["refreshes"]
                            and value == missing
                        )
                    ),
                ),
            )
        if name == "Refresh workspace details":
            return SimpleNamespace(
                wait_for=lambda **kwargs: None,
                is_visible=lambda: True,
                click=lambda: state.__setitem__("refreshes", state["refreshes"] + 1),
            )
        raise AssertionError(name)

    result = inspect_original_details(
        SimpleNamespace(get_by_role=get_by_role),
        phase["workspace_id"],
        selected["request_id"],
        phase["operation_id"],
    )
    assert result["status"] == "BLOCKED"
    assert (
        "not verified" if missing_after_refresh else "identities visible"
    ) in result["reason"]
    assert state["refreshes"] == 1
    assert all("create" not in name.lower() for _, name in calls)


@pytest.mark.parametrize("unavailable", ["details", "refresh", "identity", "read"])
def test_unavailable_original_details_do_not_refresh_or_claim_reentry(unavailable):
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    clicks = []

    def get_by_role(role, *, name, exact=False):
        if unavailable == "read":
            raise RuntimeError("private browser error")
        if name == "Workspace details":
            return SimpleNamespace(
                wait_for=lambda **kwargs: None,
                is_visible=lambda: unavailable != "details",
                get_by_text=lambda value, exact: SimpleNamespace(
                    wait_for=lambda **kwargs: None,
                    is_visible=lambda: unavailable != "identity",
                ),
            )
        assert (role, name, exact) == ("button", "Refresh workspace details", True)
        return SimpleNamespace(
            wait_for=lambda **kwargs: None,
            is_visible=lambda: unavailable != "refresh",
            click=lambda: clicks.append(name),
        )

    result = inspect_original_details(
        SimpleNamespace(get_by_role=get_by_role),
        phase["workspace_id"],
        selected["request_id"],
        phase["operation_id"],
    )
    assert result["status"] == "BLOCKED"
    assert "identities visible" not in result["reason"]
    assert "private browser error" not in str(result)
    assert clicks == []


@pytest.mark.parametrize("loading_phase", ["initial", "refresh"])
@pytest.mark.parametrize("loading_read", ["workspace", "operation"])
@pytest.mark.parametrize("times_out", [False, True])
def test_original_details_wait_for_async_c1_reads(
    loading_phase, loading_read, times_out
):
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    state = {"loading": loading_phase == "initial", "refreshes": 0}
    waits = []

    class Locator:
        def __init__(self, kind):
            self.kind = kind

        def is_visible(self):
            if self.kind == "region" or not state["loading"]:
                return True
            return self.kind == "button" and loading_read == "operation"

        def wait_for(self, *, state, timeout):
            waits.append((state, timeout))
            if not self.is_visible():
                if times_out:
                    raise TimeoutError("private delayed browser response")
                finish_loading()

        def get_by_text(self, value, *, exact):
            assert exact is True
            assert value in {
                phase["workspace_id"],
                selected["request_id"],
                phase["operation_id"],
            }
            return Locator("identity")

        def click(self):
            assert self.kind == "button"
            state["refreshes"] += 1
            state["loading"] = loading_phase == "refresh"

    def finish_loading():
        state["loading"] = False

    def get_by_role(role, *, name, exact=False):
        if role == "region":
            assert name == "Workspace details"
        else:
            assert (role, name, exact) == ("button", "Refresh workspace details", True)
        return Locator(role)

    result = inspect_original_details(
        SimpleNamespace(get_by_role=get_by_role),
        phase["workspace_id"],
        selected["request_id"],
        phase["operation_id"],
    )
    assert result["status"] == "BLOCKED"
    assert ("identities visible" in result["reason"]) is not times_out
    assert "private delayed browser response" not in str(result)
    assert state["refreshes"] == (0 if times_out and loading_phase == "initial" else 1)
    assert waits and all(wait == ("visible", 5000) for wait in waits)


@pytest.mark.parametrize("status_code", [200, 403, 503])
@pytest.mark.parametrize("enabled", [False, True])
def test_source_pinned_retirement_review_never_submits(status_code, enabled):
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    retirement_id = identifier(9)
    body = preview_body(selected, phase, retirement_id) if status_code == 200 else None
    calls = []
    identities = {retirement_id, phase["workspace_id"], phase["operation_id"]}

    def get_by_role(role, *, name, exact=False):
        calls.append((role, name))
        if name == "Remove workspace":
            return SimpleNamespace(is_visible=lambda: True, is_enabled=lambda: enabled)
        return SimpleNamespace(is_visible=lambda: True)

    def get_by_label(label, *, exact=False):
        assert (label, exact) == ("Retirement review", True)
        return SimpleNamespace(
            is_visible=lambda: True,
            get_by_role=get_by_role,
            get_by_text=lambda value, exact: SimpleNamespace(
                is_visible=lambda: value in identities or value.startswith("Unknown;")
            ),
        )

    result = inspect_retirement_review(
        SimpleNamespace(get_by_role=get_by_role, get_by_label=get_by_label),
        DemoInput.parse(selected),
        phase["workspace_id"],
        phase["operation_id"],
        retirement_id,
        status_code,
        body,
    )
    assert result["status"] == ("FAIL" if enabled and status_code == 200 else "BLOCKED")
    assert result["admission_available"] is False
    assert result["cost"] == "UNKNOWN"
    assert ("button", "Review removal") in calls
    if status_code != 200:
        assert ("button", "Remove workspace") not in calls
    assert all(name not in ("submit", "delete") for _, name in calls)


@pytest.mark.parametrize(
    "unavailable", ["region", "review", "preview", "remove", "cost", "read"]
)
def test_missing_retirement_controls_never_claim_visible_preview(unavailable):
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    retirement_id = identifier(9)
    clicks = []

    def get_by_role(role, *, name, exact=False):
        if unavailable == "read":
            raise RuntimeError("private browser error")
        control = {
            "Workspace retirement": "region",
            "Review removal": "review",
            "Remove workspace": "remove",
        }[name]
        return SimpleNamespace(
            is_visible=lambda: unavailable != control,
            is_enabled=lambda: False,
            click=lambda: clicks.append(name),
        )

    def get_by_label(label, *, exact=False):
        assert (label, exact) == ("Retirement review", True)
        return SimpleNamespace(
            is_visible=lambda: unavailable != "preview",
            get_by_role=get_by_role,
            get_by_text=lambda value, exact: SimpleNamespace(
                is_visible=lambda: (
                    not (unavailable == "cost" and value.startswith("Unknown;"))
                )
            ),
        )

    result = inspect_retirement_review(
        SimpleNamespace(get_by_role=get_by_role, get_by_label=get_by_label),
        DemoInput.parse(selected),
        phase["workspace_id"],
        phase["operation_id"],
        retirement_id,
        200,
        preview_body(selected, phase, retirement_id),
    )
    assert result["status"] == "BLOCKED"
    assert result["admission_available"] is False
    assert result["cost"] == "UNKNOWN"
    assert "preview visible" not in result["reason"]
    assert "private browser error" not in str(result)
    assert clicks == []


def test_swapped_rendered_retirement_identity_is_rejected():
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    page = SimpleNamespace(
        get_by_role=lambda role, *, name, exact=False: SimpleNamespace(
            is_visible=lambda: True, is_enabled=lambda: False
        ),
        get_by_label=lambda label, *, exact=False: SimpleNamespace(
            is_visible=lambda: True,
            get_by_role=lambda role, *, name, exact=False: SimpleNamespace(
                is_visible=lambda: True, is_enabled=lambda: False
            ),
            get_by_text=lambda value, exact: SimpleNamespace(
                is_visible=lambda: value != phase["operation_id"]
            ),
        ),
    )
    result = inspect_retirement_review(
        page,
        DemoInput.parse(selected),
        phase["workspace_id"],
        phase["operation_id"],
        identifier(9),
        200,
        preview_body(selected, phase, identifier(9)),
    )
    assert result["status"] == "FAIL"
    assert "identity" in result["reason"]


@pytest.mark.parametrize("status_code", [200, 403, 503])
@pytest.mark.parametrize("reused_identity", ["request", "operation"])
def test_retirement_cannot_reuse_creation_lineage(status_code, reused_identity):
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    retirement_id = (
        selected["request_id"]
        if reused_identity == "request"
        else phase["operation_id"]
    )
    body = preview_body(selected, phase, retirement_id) if status_code == 200 else None
    with pytest.raises(EvidenceError, match="identity reused from creation"):
        retirement_preview(
            DemoInput.parse(selected),
            phase["workspace_id"],
            phase["operation_id"],
            retirement_id,
            status_code,
            body,
        )


@pytest.mark.parametrize("status", [403, 503])
def test_denied_or_unavailable_preview_preserves_unknown_cost(status):
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    result = retirement_preview(
        DemoInput.parse(selected),
        phase["workspace_id"],
        phase["operation_id"],
        identifier(9),
        status,
        None,
    )
    assert result["status"] == "BLOCKED"
    assert result["cost"] == "UNKNOWN"
    assert result["admission_available"] is False


@pytest.mark.parametrize(
    "bad_field,value",
    [
        ("account_id", "000000000001"),
        ("source_operation_id", identifier(41)),
        ("request_id", identifier(42)),
        ("admission_available", True),
        ("approval_request", {"approval_id": identifier(43)}),
        ("blocked_reason", None),
    ],
)
def test_foreign_or_future_admission_cannot_prove_removal(bad_field, value):
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    response = preview_body(selected, phase, identifier(9))
    response[bad_field] = value
    with pytest.raises(EvidenceError):
        retirement_preview(
            DemoInput.parse(selected),
            phase["workspace_id"],
            phase["operation_id"],
            identifier(9),
            200,
            response,
        )


def test_fixture_cli_parses_c1_retirement_preview_without_deletion(tmp_path: Path):
    selected, fixture = fixture_documents()
    phase = fixture["phases"][0]
    fixture["retirement"] = {
        "status_code": 200,
        "operation_id": identifier(9),
        "body": preview_body(selected, phase, identifier(9)),
    }
    private_path = tmp_path / "private.json"
    fixture_path = tmp_path / "fixture.json"
    report_path = tmp_path / "report.json"
    write_private(private_path, selected)
    write_private(fixture_path, fixture)
    result = run_cli(
        "--mode",
        "fixture",
        "--private-input",
        str(private_path),
        "--fixture",
        str(fixture_path),
        "--report",
        str(report_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["scenario"]["checks"]["removal"]["status"] == "BLOCKED"
    assert report["scenario"]["retirement_preview"]["cost"] == "UNKNOWN"
    assert report["scenario"]["retirement_preview"]["admission_available"] is False
    assert "123456789012" not in json.dumps(report)
