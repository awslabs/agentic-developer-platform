"""Owned policy cleanup cannot delete concurrent or uncertain writes."""

import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def scenario(monkeypatch):
    remote = Path(__file__).parents[1] / "e2e/cli_uplift/remote"
    monkeypatch.syspath_prepend(str(remote))
    spec = importlib.util.spec_from_file_location(
        "budget_scenario", remote / "budget_lifecycle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    return {
        "login_user_id": "admin-login",
        "canonical_user_id": "admin",
        "tenant_id": "owned",
        "ordinary_canonical_user_id": "ordinary",
        "owned_mutations_authorized": True,
        "exclusive_ordinary_fixture": True,
    }


class Cli:
    def __init__(self, fault=None):
        self.rows, self.calls, self.revision, self.fault = {}, [], 0, fault

    def json(self, argv, **kwargs):
        self.calls.append(argv)
        if argv == ["ratelimit", "me"]:
            return {
                "status": "ok",
                "detail": {
                    "runtime": {
                        "worker_convergence": "unknown",
                        "tpm": "unavailable_actual_usage_not_reconciled",
                    }
                },
            }
        family, action = argv[1:3]
        flags = {
            argv[i]: argv[i + 1]
            for i in range(3, len(argv) - 1)
            if argv[i].startswith("--") and not argv[i + 1].startswith("--")
        }
        key = (family, flags.get("--usage"), flags.get("--period"))
        row = self.rows.get(key)
        if action == "show":
            if self.fault == "foreign_revision" and row:
                row["updated_at"] = "foreign"
            return {
                "status": "ok" if row or family == "ratelimit" else "unavailable",
                "detail": {
                    "configuration" if family == "budget" else "saved": copy.deepcopy(
                        row
                    )
                },
            }
        if action == "status":
            return {"status": "ok", "detail": {"period_type": flags["--period"]}}
        if "--dry-run" in argv:
            return {"status": "dry_run", "detail": {}}
        if action == "delete":
            assert row["updated_at"] == flags["--expected-revision"]
            del self.rows[key]
            return {"status": "ok", "detail": {}}
        self.revision += 1
        row = copy.deepcopy(row or {})
        row["updated_at"] = str(self.revision)
        if family == "budget":
            row.update(
                period_type=flags["--period"],
                budget_amount_usd=flags["--amount-usd"],
                enforcement_mode=flags["--mode"],
            )
        else:
            for name in ("rpm", "tpm", "concurrent_requests"):
                flag = "--" + name.replace("_", "-")
                if flag in flags:
                    row[name] = int(flags[flag])
        self.rows[key] = row
        if self.fault == "unknown":
            return {
                "status": "pending",
                "detail": {"observed_configuration": copy.deepcopy(row)},
            }
        if self.fault == "transport":
            raise RuntimeError("Lost response")
        return {
            "status": "ok",
            "detail": {"configuration": copy.deepcopy(row)}
            if family == "budget"
            else {"observed": {"saved": copy.deepcopy(row)}},
        }

    def run(self, argv, **kwargs):
        return 5, {
            "status": "failed",
            "error": {"code": "http_error", "http_status": 409},
        }


class Ordinary:
    def run(self, argv, **kwargs):
        return 3, {"status": "failed", "error": {"code": "permission_denied"}}

    def json(self, argv):
        return Cli().json(argv)


def policies(scenario, tmp_path, admin):
    config = {
        "evaluation_id": "owned-budget",
        "gateway_url": "https://gateway.example",
        "budget_lifecycle": fixture(),
    }
    plan = scenario.recovery_plan(config)
    state = {"plan": plan, "checks": []}
    return scenario.Policies(admin, Ordinary(), plan, tmp_path / "journal.json", state)


def test_six_period_ledger_caps_and_dimension_patch_cleanly_restore_absence(
    scenario, tmp_path
):
    admin = Cli()
    driver = policies(scenario, tmp_path, admin)
    driver.exercise()
    assert admin.rows == {}
    assert driver.state["cleanup"] == "verified"
    assert len([c for c in admin.calls if c[2] == "delete"]) == 7
    assert len(driver.state["checks"]) == 3


@pytest.mark.parametrize("fault", ["unknown", "transport", "foreign_revision"])
def test_uncertain_delivery_or_concurrent_revision_never_deleted(
    scenario, tmp_path, fault
):
    admin = Cli(fault)
    driver = policies(scenario, tmp_path, admin)
    with pytest.raises(scenario.common.RemoteError):
        driver.exercise()
    assert admin.rows
    assert not any(c[2] == "delete" for c in admin.calls)
    assert driver.state["cleanup"] == "reconciliation_required"
    assert (tmp_path / "journal.json").exists()


def test_existing_policy_refuses_before_any_write(scenario, tmp_path):
    admin = Cli()
    admin.rows[("budget", "personal", "daily")] = {"updated_at": "existing"}
    driver = policies(scenario, tmp_path, admin)
    with pytest.raises(scenario.common.RemoteError, match="policy exists"):
        driver.exercise()
    assert all(c[2] == "show" for c in admin.calls)


def test_cleanup_continues_for_other_acknowledged_rows(scenario, tmp_path):
    admin = Cli()
    driver = policies(scenario, tmp_path, admin)
    driver.write(driver.entries[0], ["--amount-usd", "1", "--mode", "hard"])
    admin.fault = "unknown"
    with pytest.raises(scenario.common.RemoteError):
        driver.write(driver.entries[1], ["--amount-usd", "1", "--mode", "hard"])
    with pytest.raises(scenario.common.RemoteError):
        driver.cleanup()
    assert ("budget", "personal", "daily") not in admin.rows
    assert ("budget", "personal", "weekly") in admin.rows


def test_fixture_requires_independent_exclusive_ordinary(scenario):
    from tests.e2e.cli_uplift.fixtures import validate_fixture
    from tests.e2e.cli_uplift.config import ConfigError

    validate_fixture("budget_lifecycle", fixture())
    for change in (
        {"exclusive_ordinary_fixture": False},
        {"ordinary_canonical_user_id": "admin"},
        {"owned_mutations_authorized": False},
    ):
        with pytest.raises(ConfigError):
            validate_fixture("budget_lifecycle", {**fixture(), **change})


def test_usage_snapshot_retains_both_ledgers_and_refuses_omitted_cloud(scenario):
    class Own:
        omit = False

        def json(self, argv):
            period = argv[-1]
            rows = [{"entity_type": "user", "spend_usd": "0.123456"}]
            if not self.omit:
                rows.append({"entity_type": "root_user", "spend_usd": "0.050000"})
            return {"detail": {"period": {"period_type": period}, "lines": rows}}

    own = Own()
    result = scenario.usage_snapshot(own)
    assert set(result) == {"daily", "weekly", "monthly"}
    assert result["daily"]["spend"] == {"user": "0.123456", "root_user": "0.050000"}
    own.omit = True
    with pytest.raises(scenario.common.RemoteError, match="Both personal"):
        scenario.usage_snapshot(own)


def test_diagnostic_requires_manifest_before_remote_transport(tmp_path):
    from tests.e2e.cli_uplift import live, ports

    worker = live._run_worker(None, {}, lambda *args: pytest.fail("unexpected install"))
    with pytest.raises(ports.PortError, match="durable caller manifest"):
        worker("i-example", "budget_lifecycle", {})
