"""Offline selection checks; no fixture in this module is a live result."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding import live

SOURCE = "a" * 40
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def owned():
    return {
        "tenant_id": "owned-tenant", "other_tenant_id": "other-tenant",
        "owner_token": "fake-token", "run_id": "test-run", "actor_id": "actor-owned",
    }


@pytest.fixture
def state_store():
    return SimpleNamespace(load=lambda _: (None, None), save=lambda *_args, **_kwargs: True)


def test_workflow_dispatches_selected_driver_and_reports_failure():
    workflow = (ROOT / ".github/workflows/gateway-live-tests.yml").read_text()
    assert "product_cases:" in workflow and "expected_revision:" in workflow
    assert "if: inputs.product_cases != '' && inputs.product_cases != 'none'" in workflow
    assert "python -m tests.e2e.product_onboarding.live" in workflow
    assert "--cases \"$PRODUCT_CASES\" --expected-revision \"$PRODUCT_EXPECTED_REVISION\"" in workflow
    assert "- api_denial_oracle" in workflow
    assert "- api_recovery_cleanup" in workflow
    assert "- api_product_lifecycle,api_denial_oracle,api_recovery_cleanup" in workflow
    assert "- api_product_lifecycle,api_denial_oracle" in workflow
    assert "PRODUCT_SELECTED: ${{ inputs.product_cases != '' && inputs.product_cases != 'none' }}" in workflow
    assert "PRODUCT_OUTCOME: ${{ steps.product.outcome }}" in workflow
    assert "PRODUCT_CLEANUP_OK: ${{ steps.product.outputs.cleanup_ok }}" in workflow
    assert '[ "$PRODUCT_OUTCOME" = success ]' in workflow
    assert '[ "$PRODUCT_CLEANUP_OK" = true ]' in workflow
    selected_step = workflow.split("- name: Run selected Product API cases", 1)[1].split(
        "- name: Run live tests", 1
    )[0]
    for reference in (
        "INSTANCE_PROFILE", "SECURITY_GROUP_ID", "STATE_BUCKET", "CREDENTIAL_SECRET_NAME", "INTERNAL_INVOKE_URL"
    ):
        assert f"CLI_UPLIFT_EVAL_{reference}: ${{{{ vars.CLI_UPLIFT_EVAL_{reference} }}}}" in selected_step
    assert workflow.index('.venv/bin/pip install -e ".[dev]"') < workflow.index("Run selected Product API cases")


def test_workflow_cleanup_output_refuses_failed_selected_driver(tmp_path, monkeypatch):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    with pytest.raises(ValueError, match="source revision"):
        live.main(["--cases", "api_product_lifecycle", "--expected-revision", "missing"])
    assert output.read_text() == "cleanup_ok=false\n"


@pytest.mark.parametrize("missing", ["status", "cleanup_verified", "observed_revision",
                                     "observation_ref", "run_id", "selected_cases"])
def test_workflow_cleanup_output_needs_complete_owned_release_proof(tmp_path, monkeypatch, missing):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    report = {
        "status": "pass", "cleanup_verified": True,
        "expected_revision": SOURCE, "observed_revision": SOURCE,
        "workflow_revision": "b" * 40, "observation_ref": "verified-observation",
        "run_id": "product-123456-2", "selected_cases": ["api_product_lifecycle"],
    }
    live.publish_cleanup_boundary(report, str(output))
    assert output.read_text() == "cleanup_ok=false\n"
    live.publish_cleanup_boundary(report, str(output), completed=True)
    assert output.read_text() == "cleanup_ok=false\ncleanup_ok=true\n"
    report[missing] = None if missing != "status" else "incomplete"
    live.publish_cleanup_boundary(report, str(output), completed=True)
    assert output.read_text() == "cleanup_ok=false\ncleanup_ok=true\ncleanup_ok=false\n"


def test_scenario_invokes_real_driver_selection_and_source(monkeypatch):
    from tests.e2e.product_onboarding import test_v01

    selected = []
    monkeypatch.setenv("PRODUCT_EXPECTED_REVISION", SOURCE)
    monkeypatch.setattr(test_v01.live, "main", lambda arguments: selected.append(arguments))
    test_v01.api_product_lifecycle()
    assert selected == [["--cases", "api_product_lifecycle", "--expected-revision", SOURCE]]


def test_denial_scenario_invokes_selected_live_driver(monkeypatch):
    from tests.e2e.product_onboarding import test_v01

    selected = []
    monkeypatch.setenv("PRODUCT_EXPECTED_REVISION", SOURCE)
    monkeypatch.setattr(test_v01.live, "main", lambda arguments: selected.append(arguments))
    test_v01.api_denial_oracle()
    assert selected == [["--cases", "api_denial_oracle", "--expected-revision", SOURCE]]


def test_denial_selection_forwards_owned_receipt_and_distinct_actors(owned, monkeypatch):
    receipt = {"product_id": "product-owned", "project_id": "project-owned",
               "edit_key": "product-edit-original", "edited_fields": {"name": "edited"}}
    owned.update(receipt, foreign_token="foreign-token", revoked_token="revoked-token",
                 expected_product_id="product-owned")
    calls = []
    monkeypatch.setattr(live, "api_denial_oracle", lambda *args, **kwargs: calls.append(kwargs) or {"revision": 2})
    result = live.run("api_denial_oracle", SOURCE, owned, lambda: SOURCE, object(), lambda _: None)
    assert result == {"api_denial_oracle": {"revision": 2}}
    assert calls[0]["foreign_token"] == "foreign-token"
    assert calls[0]["revoked_token"] == "revoked-token"
    assert calls[0]["edit_key"] == "product-edit-original"
    assert calls[0]["expected_product_id"] == "product-owned"


@pytest.mark.parametrize("field,value", [
    ("foreign_token", None), ("foreign_token", "fake-token"),
    ("revoked_token", None), ("edit_key", None), ("edited_fields", {}),
])
def test_denial_refuses_unbound_actor_or_replay_receipt(owned, monkeypatch, field, value):
    owned.update(foreign_token="foreign-token", revoked_token="revoked-token",
                 product_id="product-owned", project_id="project-owned",
                 edit_key="product-edit-original", edited_fields={"name": "edited"})
    owned[field] = value
    monkeypatch.setattr(live, "api_denial_oracle", lambda *a, **kw: pytest.fail("denial started"))
    with pytest.raises(ValueError, match="sessions|receipt"):
        live.run("api_denial_oracle", SOURCE, owned, lambda: pytest.fail("snapshot started"), object(), lambda _: None)


def test_lifecycle_receipt_flows_to_denial_without_a_new_write(owned, state_store, monkeypatch):
    owned.update(foreign_token="foreign-token", revoked_token="revoked-token")
    receipt = {"product_id": "product-owned", "project_id": "project-owned",
               "edit_key": "original-edit", "edited_fields": {"name": "edited"}}
    calls = []
    monkeypatch.setattr(live, "api_product_lifecycle", lambda *a, **kw: calls.append("lifecycle") or receipt)
    monkeypatch.setattr(live, "api_denial_oracle", lambda *a, **kw: calls.append(kw) or {"revision": 2})
    result = live.run("api_product_lifecycle,api_denial_oracle", SOURCE, owned,
                      lambda: SOURCE, object(), lambda _: None, state_store=state_store)
    assert list(result) == ["api_product_lifecycle", "api_denial_oracle"]
    assert calls[0] == "lifecycle"
    assert calls[1]["edit_key"] == receipt["edit_key"]
    assert calls[1]["product_id"] == receipt["product_id"]
    assert calls[1]["expected_product_id"] == receipt["product_id"]
    with pytest.raises(ValueError, match="precede"):
        live.run("api_denial_oracle,api_product_lifecycle", SOURCE, owned,
                 lambda: pytest.fail("snapshot started"), object(), lambda _: None, state_store=state_store)


@pytest.mark.parametrize("selection", ["", "none", "api_product_lifecycle,api_product_lifecycle", "other-case"])
def test_unknown_or_duplicate_selection_fails(selection):
    with pytest.raises(ValueError, match="selection"):
        live.select(selection)


def test_selected_product_case_reaches_driver_with_pinned_release(owned, state_store, monkeypatch):
    calls = []
    monkeypatch.setattr(live, "api_product_lifecycle", lambda *args, **kwargs: calls.append(kwargs) or {"revision": 2})
    snapshots = []

    def observe():
        snapshots.append(SOURCE)
        return SOURCE

    result = live.run("api_product_lifecycle", SOURCE, owned, observe, object(),
                      lambda records: calls.append(records), state_store=state_store)
    assert result == {"api_product_lifecycle": {"revision": 2}}
    assert len(snapshots) == 2
    assert calls[0]["tenant_id"] == "owned-tenant"
    assert calls[1] == result


@pytest.mark.parametrize("source,observed", [("", SOURCE), (SOURCE, "b" * 40)])
def test_stale_or_missing_release_refuses_mutation(owned, state_store, monkeypatch, source, observed):
    monkeypatch.setattr(live, "api_product_lifecycle", lambda *a, **kw: pytest.fail("mutation started"))
    with pytest.raises(ValueError, match="revision|differs"):
        live.run("api_product_lifecycle", source, owned, lambda: observed, object(), lambda _: None, state_store=state_store)


def test_missing_cleanup_refuses_mutation(owned, monkeypatch):
    monkeypatch.setattr(live, "api_product_lifecycle", lambda *a, **kw: pytest.fail("mutation started"))
    with pytest.raises(TypeError, match="recovery"):
        live.run("api_product_lifecycle", SOURCE, owned, lambda: SOURCE, object(), None)


def test_unavailable_observation_refuses_mutation(owned, state_store, monkeypatch):
    monkeypatch.setattr(live, "api_product_lifecycle", lambda *a, **kw: pytest.fail("mutation started"))

    def unavailable():
        raise PermissionError("read-only Gateway observation denied")

    with pytest.raises(PermissionError, match="observation denied"):
        live.run("api_product_lifecycle", SOURCE, owned, unavailable, object(), lambda _: None, state_store=state_store)


@pytest.mark.parametrize("missing", ["state_store", "actor_id"])
def test_lifecycle_requires_durable_ownership_before_observation(owned, state_store, missing, monkeypatch):
    monkeypatch.setattr(live, "api_product_lifecycle", lambda *a, **kw: pytest.fail("mutation started"))
    if missing == "actor_id":
        owned.pop("actor_id")
    with pytest.raises(ValueError, match="durable ownership store and fixture actor"):
        live.run("api_product_lifecycle", SOURCE, owned, lambda: pytest.fail("observation started"),
                 object(), lambda _: pytest.fail("cleanup started"),
                 state_store=None if missing == "state_store" else state_store)


def test_cleanup_runs_after_failed_case(owned, state_store, monkeypatch):
    monkeypatch.setattr(live, "api_product_lifecycle", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("write failed")))
    cleaned = []
    with pytest.raises(RuntimeError, match="write failed"):
        live.run("api_product_lifecycle", SOURCE, owned, lambda: SOURCE, object(), cleaned.append, state_store=state_store)
    assert cleaned == [{}]


def test_interrupted_lifecycle_cleanup_receives_original_owned_manifest(owned, monkeypatch):
    from tests.e2e.product_onboarding.product_recovery import InterruptedWrite

    owned["actor_id"] = "actor-owned"
    manifest = {
        "run_id": owned["run_id"], "tenant_id": owned["tenant_id"],
        "actor_id": owned["actor_id"], "source_revision": SOURCE,
        "idempotency_key": "product-create-originalkey",
    }
    document = {**manifest, "attempts": [{"state": "response_unknown"}], "receipt": None}
    state_store = SimpleNamespace(load=lambda _: (document, manifest), save=lambda *_args, **_kwargs: True)
    cleaned = []

    def interrupted(*args, **kwargs):
        assert kwargs["state_store"] is state_store
        raise InterruptedWrite("response lost")

    monkeypatch.setattr(live, "api_product_lifecycle", interrupted)
    with pytest.raises(InterruptedWrite, match="response lost"):
        live.run("api_product_lifecycle", SOURCE, owned, lambda: SOURCE, object(),
                 cleaned.append, state_store=state_store)
    assert cleaned == [{"owned_manifest": manifest}]
    assert cleaned[0]["owned_manifest"]["idempotency_key"] == "product-create-originalkey"


@pytest.mark.parametrize("corrupt", ["missing", "wrong_owner", "different_key"])
def test_interrupted_lifecycle_refuses_unknown_cleanup_ownership(owned, monkeypatch, corrupt):
    owned["actor_id"] = "actor-owned"
    manifest = {
        "run_id": owned["run_id"], "tenant_id": owned["tenant_id"],
        "actor_id": owned["actor_id"], "source_revision": SOURCE,
        "idempotency_key": "product-create-originalkey",
    }
    document = dict(manifest)
    if corrupt == "missing":
        manifest = None
    elif corrupt == "wrong_owner":
        manifest["actor_id"] = "foreign-actor"
    else:
        manifest["idempotency_key"] = "product-create-anotherkey"
    state_store = SimpleNamespace(load=lambda _: (document, manifest), save=lambda *_args, **_kwargs: True)
    monkeypatch.setattr(live, "api_product_lifecycle", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("response lost")))
    with pytest.raises(ValueError, match="ownership manifest"):
        live.run("api_product_lifecycle", SOURCE, owned, lambda: SOURCE, object(),
                 lambda _: pytest.fail("unowned cleanup dispatched"), state_store=state_store)


def test_recovery_scenario_invokes_pinned_live_selection(monkeypatch):
    from tests.e2e.product_onboarding import test_v01

    selected = []
    monkeypatch.setenv("PRODUCT_EXPECTED_REVISION", SOURCE)
    monkeypatch.setattr(test_v01.live, "main", lambda arguments: selected.append(arguments))
    test_v01.api_recovery_cleanup()
    assert selected == [["--cases", "api_recovery_cleanup", "--expected-revision", SOURCE]]


@pytest.mark.parametrize("change,expected", [
    ({"foreign_token": "fake-token"}, "distinct"),
    ({"foreign_token": None}, "shared-tenant"),
    ({"shared_product_id": None}, "shared-tenant"),
    ({"shared_project_id": None}, "shared-tenant"),
])
def test_recovery_refuses_unbound_shared_fixture(owned, monkeypatch, change, expected):
    owned.update(foreign_token="foreign-token", shared_product_id="shared-product",
                 shared_project_id="shared-project")
    owned.update(change)
    monkeypatch.setattr(live, "api_recovery_cleanup", lambda *a, **kw: pytest.fail("write started"))
    with pytest.raises(ValueError, match=expected):
        live.run("api_recovery_cleanup", SOURCE, owned,
                 lambda: pytest.fail("snapshot started"), object(), lambda _: None,
                 state_store=SimpleNamespace(load=lambda *_: None, save=lambda *_: None))


def test_recovery_refuses_missing_durable_manifest_store(owned, monkeypatch):
    owned.update(foreign_token="foreign-token", shared_product_id="shared-product",
                 shared_project_id="shared-project")
    monkeypatch.setattr(live, "api_recovery_cleanup", lambda *a, **kw: pytest.fail("write started"))
    with pytest.raises(TypeError, match="durable state store"):
        live.run("api_recovery_cleanup", SOURCE, owned,
                 lambda: pytest.fail("snapshot started"), object(), lambda _: None)

def test_verified_member_revocation_precedes_denial_and_is_reported(owned, monkeypatch):
    owned.update(
        product_id="owned-product",
        project_id="owned-project",
        edit_key="edit-key",
        edited_fields={"name": "Updated"},
        foreign_token="foreign-token",
        revoked_token="revoked-token",
    )
    observed = []
    owned["revoke_actor"] = lambda: observed.append("revoked") or {
        "user_id": "owned-member",
        "membership_revoked": True,
    }
    monkeypatch.setattr(
        live,
        "api_denial_oracle",
        lambda *args, **kwargs: observed.append("denial") or {"revision": 2},
    )
    result = live.run(
        "api_denial_oracle",
        SOURCE,
        owned,
        lambda: SOURCE,
        object(),
        lambda _: observed.append("cleanup"),
    )
    assert observed == ["revoked", "denial", "cleanup"]
    assert result["api_denial_oracle"]["revocation"] == {
        "user_id": "owned-member",
        "membership_revoked": True,
    }


def test_incomplete_revocation_refuses_before_denial(owned, monkeypatch):
    owned.update(
        product_id="owned-product",
        project_id="owned-project",
        edit_key="edit-key",
        edited_fields={"name": "Updated"},
        foreign_token="foreign-token",
        revoked_token="revoked-token",
        revoke_actor=lambda: {"membership_revoked": False},
    )
    monkeypatch.setattr(
        live, "api_denial_oracle", lambda *a, **kw: pytest.fail("denial began")
    )
    with pytest.raises(ValueError, match="revocation"):
        live.run(
            "api_denial_oracle", SOURCE, owned, lambda: SOURCE, object(), lambda _: None
        )

def test_release_hold_requires_isolated_lifecycle_before_mutation(
    owned, state_store, monkeypatch
):
    monkeypatch.setattr(
        live, "api_product_lifecycle", lambda *a, **kw: pytest.fail("mutation started")
    )
    with pytest.raises(ValueError, match="isolated Product lifecycle"):
        live.run(
            "api_product_lifecycle,api_denial_oracle",
            SOURCE,
            owned,
            lambda: pytest.fail("observation started"),
            object(),
            lambda _: None,
            state_store=state_store,
            retain_owned=lambda _: True,
        )


def test_release_hold_retains_verified_manifest_without_cleanup(owned, monkeypatch):
    manifest = {
        "run_id": owned["run_id"],
        "tenant_id": owned["tenant_id"],
        "actor_id": owned["actor_id"],
        "source_revision": SOURCE,
        "idempotency_key": "product-create-originalkey",
        "product_id": "owned-product",
        "project_id": "owned-project",
    }
    document = {
        **manifest,
        "receipt": {"product_id": "owned-product", "project_id": "owned-project"},
    }
    store = SimpleNamespace(
        load=lambda _: (document, manifest), save=lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        live, "api_product_lifecycle", lambda *a, **kw: {"product_id": "owned-product"}
    )
    held, cleaned = [], []
    assert live.run(
        "api_product_lifecycle",
        SOURCE,
        owned,
        lambda: SOURCE,
        object(),
        cleaned.append,
        state_store=store,
        retain_owned=lambda receipt: held.append(receipt) or True,
    ) == {"api_product_lifecycle": {"product_id": "owned-product"}}
    assert held == [
        {
            "api_product_lifecycle": {"product_id": "owned-product"},
            "owned_manifest": manifest,
        }
    ]
    assert cleaned == []


@pytest.mark.parametrize("fail", ["case", "hold"])
def test_failed_release_hold_retires_original_owned_fixture(owned, monkeypatch, fail):
    manifest = {
        "run_id": owned["run_id"],
        "tenant_id": owned["tenant_id"],
        "actor_id": owned["actor_id"],
        "source_revision": SOURCE,
        "idempotency_key": "product-create-originalkey",
    }
    store = SimpleNamespace(
        load=lambda _: ({**manifest, "receipt": None}, manifest),
        save=lambda *_args, **_kwargs: True,
    )
    if fail == "case":
        monkeypatch.setattr(
            live,
            "api_product_lifecycle",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("case failed")),
        )
    else:
        monkeypatch.setattr(
            live,
            "api_product_lifecycle",
            lambda *a, **kw: {"product_id": "owned-product"},
        )
    cleaned = []
    with pytest.raises(RuntimeError, match="case failed|hold failed"):
        live.run(
            "api_product_lifecycle",
            SOURCE,
            owned,
            lambda: SOURCE,
            object(),
            cleaned.append,
            state_store=store,
            retain_owned=lambda _: (_ for _ in ()).throw(RuntimeError("hold failed")),
        )
    assert cleaned == [
        {
            "owned_manifest": manifest,
            **(
                {"api_product_lifecycle": {"product_id": "owned-product"}}
                if fail == "hold"
                else {}
            ),
        }
    ]
