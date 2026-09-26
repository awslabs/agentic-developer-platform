"""D06: owned ordinary budget and rate-limit policy lifecycle; no inference."""

import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

import common
from budget_lifecycle_plan import recovery_plan
from capability_contrast import _operations, _write_session
from machine_lifecycle import ordinary_tokens, require_refusal


def detail(result):
    common.require(isinstance(result, dict), "Missing CLI envelope")
    return result.get("detail") or {}


def ordinary_refusal(result):
    code = ((result[1] or {}).get("error") or {}).get("code")
    require_refusal(
        result,
        code="permission_denied" if code == "permission_denied" else None,
        http=None if code == "permission_denied" else 403,
    )


class Policies:
    """Fixed CLI policy operations with durable revision-fenced cleanup."""

    def __init__(self, admin, ordinary, plan, journal, state):
        self.admin, self.ordinary = admin, ordinary
        self.plan, self.journal, self.state = plan, journal, state
        fixture = plan["fixture"]
        self.selection = ["--org", fixture["tenant_id"]]
        self.target = fixture["ordinary_canonical_user_id"]
        self.entries = []
        for target in plan["budgets"]:
            self.entries.append(
                {
                    "family": "budget",
                    "flags": [
                        *self.selection,
                        "--user",
                        self.target,
                        "--usage",
                        target["usage"],
                        "--period",
                        target["period"],
                    ],
                    "expected": None,
                    "attempted": False,
                }
            )
        self.entries.append(
            {
                "family": "ratelimit",
                "flags": [*self.selection, "--scope", "user", "--target", self.target],
                "expected": None,
                "attempted": False,
            }
        )
        state["policies"] = self.entries

    def persist(self):
        temporary = self.journal.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(self.state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.journal)

    def command(self, entry, action, *extra):
        return ["admin", entry["family"], action, *entry["flags"], *extra]

    def read(self, entry):
        result = self.admin.json(self.command(entry, "show"), expected=None)
        common.require(
            result.get("status") in {"ok", "unavailable"}, "Policy read failed"
        )
        row = detail(result)
        key = "configuration" if entry["family"] == "budget" else "saved"
        common.require(key in row, "Policy absence was not established")
        return row[key]

    def write(self, entry, values):
        expected = entry["expected"]
        common.require(self.read(entry) == expected, "Policy changed before mutation")
        revision = (
            ["--expected-revision", expected["updated_at"]]
            if expected
            else ["--expect-absent"]
        )
        args = self.command(entry, "set", *values)
        preview = self.admin.json([*args, "--dry-run"])
        common.require(preview.get("status") == "dry_run", "Policy preview failed")
        common.require(self.read(entry) == expected, "Policy preview changed state")
        entry["attempted"] = True
        entry["delivery"] = "unknown"
        self.persist()
        result = self.admin.json([*args, *revision, "--yes"], expected=None)
        common.require(
            result.get("status") == "ok",
            "Policy mutation outcome requires reconciliation",
        )
        row = detail(result)
        saved = (
            row.get("configuration")
            if entry["family"] == "budget"
            else (row.get("observed") or {}).get("saved")
        )
        common.require(
            isinstance(saved, dict) and saved.get("updated_at"),
            "Missing acknowledged policy revision",
        )
        entry["expected"] = saved
        entry["delivery"] = "acknowledged"
        self.persist()
        common.require(
            self.read(entry) == saved, "Acknowledged policy changed at readback"
        )
        return saved

    def cleanup(self):
        failures = []
        for entry in reversed(self.entries):
            if not entry["attempted"]:
                continue
            try:
                common.require(
                    entry.get("delivery") == "acknowledged", "Unknown mutation delivery"
                )
                expected = entry["expected"]
                common.require(
                    expected is not None and self.read(entry) == expected,
                    "Policy changed before cleanup",
                )
                result = self.admin.json(
                    self.command(
                        entry,
                        "delete",
                        "--expected-revision",
                        expected["updated_at"],
                        "--yes",
                    ),
                    expected=None,
                )
                common.require(
                    result.get("status") == "ok" and self.read(entry) is None,
                    "Policy removal unverified",
                )
                entry["cleanup"] = "verified_absent"
            except Exception:
                entry["cleanup"] = "reconciliation_required"
                failures.append(entry["flags"])
            self.persist()
        self.state["cleanup"] = "reconciliation_required" if failures else "verified"
        self.persist()
        common.require(
            not failures,
            "Owned policy cleanup requires reconciliation; retained journal identifies exact targets",
        )

    def exercise(self):
        for entry in self.entries:
            common.require(
                self.read(entry) is None,
                "Selected fixture policy exists; no existing or shared policy may be changed",
            )
        self.persist()
        try:
            for entry in self.entries[:-1]:
                self.write(entry, ["--amount-usd", "1", "--mode", "hard"])
            self.state["checks"].append("six_exact_ledger_period_caps_coexist")
            daily = self.entries[0]
            old = daily["expected"]["updated_at"]
            self.write(daily, ["--amount-usd", "2", "--mode", "soft"])
            require_refusal(
                self.admin.run(
                    self.command(
                        daily,
                        "set",
                        "--amount-usd",
                        "3",
                        "--mode",
                        "hard",
                        "--expected-revision",
                        old,
                        "--yes",
                    ),
                    expected=None,
                ),
                http=409,
            )
            ordinary_refusal(
                self.ordinary.run(
                    self.command(
                        daily,
                        "set",
                        "--amount-usd",
                        "3",
                        "--mode",
                        "hard",
                        "--expected-revision",
                        daily["expected"]["updated_at"],
                        "--yes",
                    ),
                    expected=None,
                )
            )
            for entry in self.entries[:-1]:
                common.require(
                    self.read(entry) == entry["expected"],
                    "Another period or ledger changed",
                )
                status = self.admin.json(self.command(entry, "status"))
                common.require(
                    detail(status).get("period_type")
                    == entry["expected"]["period_type"],
                    "Status period mismatch",
                )
            self.state["checks"].append(
                "budget_update_stale_and_ordinary_refusal_preserve_other_caps"
            )
            rate = self.entries[-1]
            saved = self.write(
                rate, ["--rpm", "60", "--tpm", "1000", "--concurrent-requests", "2"]
            )
            changed = self.write(rate, ["--rpm", "61"])
            common.require(
                all(
                    changed.get(key) == saved.get(key)
                    for key in ("tpm", "concurrent_requests")
                ),
                "Single dimension update changed another limit",
            )
            ordinary_refusal(
                self.ordinary.run(
                    self.command(
                        rate,
                        "set",
                        "--rpm",
                        "62",
                        "--expected-revision",
                        changed["updated_at"],
                        "--yes",
                    ),
                    expected=None,
                )
            )
            runtime = detail(self.ordinary.json(["ratelimit", "me"]))["runtime"]
            common.require(
                runtime.get("worker_convergence") == "unknown"
                and runtime.get("tpm") == "unavailable_actual_usage_not_reconciled",
                "Runtime limitations were not explicit",
            )
            self.state["checks"].append("rate_dimension_patch_and_ordinary_refusal")
        finally:
            self.cleanup()


def usage_snapshot(cli):
    result = {}
    for period in ("daily", "weekly", "monthly"):
        observed = detail(cli.json(["budget", "me", "--period", period]))
        rows = observed.get("lines")
        common.require(isinstance(rows, list), "Missing own settled usage")
        values = {
            row["entity_type"]: row.get("spend_usd")
            for row in rows
            if row.get("entity_type") in {"user", "root_user"}
        }
        common.require(
            set(values) == {"user", "root_user"},
            "Both personal and cloud ledgers must be observable",
        )
        common.require(
            all(
                isinstance(value, str)
                and Decimal(value).is_finite()
                and Decimal(value) >= 0
                for value in values.values()
            ),
            "Malformed settled usage",
        )
        result[period] = {"period": observed.get("period"), "spend": values}
    return result


def execute(config, evidence):
    os.umask(0o077)
    fixture = config.get("budget_lifecycle") or {}
    common.require(
        fixture.get("owned_mutations_authorized") is True
        and fixture.get("exclusive_ordinary_fixture") is True,
        "Explicit exclusive owned ordinary fixture required",
    )
    common.require(
        config.get("test_user_id") == fixture.get("login_user_id"),
        "Administrator login mismatch",
    )
    common.require(
        fixture.get("ordinary_canonical_user_id") != fixture.get("canonical_user_id"),
        "Independent ordinary identity required",
    )
    plan = recovery_plan(config)
    common.require(
        config.get("recovery_plan") == plan,
        "Externally retained policy recovery plan required",
    )
    journal = Path(config["work_dir"]) / "budget-lifecycle-journal.json"
    common.require(
        not journal.exists(),
        "Existing policy journal requires reconciliation; never restart a mutation sequence",
    )
    state = {
        "plan": plan,
        "checks": [],
        "cleanup": "not_started",
        "qualification": "Metadata lifecycle only; no inference, spend reset or enforcement claim",
        "live_holds": [
            "actual Claude/Codex denial and restoration",
            "person override/default requires independently owned github person anchor",
            "team/department/tenant budget scopes",
            "real spend-through",
            "TPM actual usage reconciliation",
        ],
    }
    evidence["detail"] = state
    with tempfile.TemporaryDirectory(prefix="adp-budget-lifecycle-") as directory:
        root = Path(directory)

        def session(name, tokens):
            home = root / name
            home.mkdir(mode=0o700)
            env = common.clean_env(
                config,
                HOME=home,
                ADP_TENANT=fixture["tenant_id"],
                BG_CONFIG_DIR=home / ".bedrock-gateway",
                ADP_HOME=home / ".adp",
            )
            for key in (
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
                "XDG_STATE_HOME",
                "XDG_RUNTIME_DIR",
                "CODEX_HOME",
            ):
                folder = home / key
                folder.mkdir(mode=0o700)
                env[key] = str(folder)
            _write_session(home, config["gateway_url"], tokens)
            return common.Cli(
                config["cli_path"], env, evidence["transcript"], timeout=45
            ), env

        admin, env = session("admin", common.session_tokens(config))
        ordinary, _ = session("ordinary", ordinary_tokens(config, env))
        for cli, principal in (
            (admin, fixture["canonical_user_id"]),
            (ordinary, fixture["ordinary_canonical_user_id"]),
        ):
            observed = detail(cli.json(["models", "mappings", "list"]))
            common.require(
                observed.get("tenant_id") == fixture["tenant_id"]
                and observed.get("principal_id") == principal,
                "Fixture canonical identity or tenant mismatch",
            )
        for cli, permitted in ((admin, "yes"), (ordinary, "no")):
            operations = _operations(cli.json(["capabilities", "--refresh"]))
            for name in ("budget.managed.write", "ratelimit.managed.write"):
                operation = operations.get(name) or {}
                common.require(
                    operation.get("permitted") == permitted,
                    "Fixture policy authority mismatch before mutations",
                )
                if permitted == "yes":
                    common.require(
                        operation.get("supported") == "yes"
                        and operation.get("enabled") == "yes",
                        "Required policy mutation capability unavailable",
                    )
        state["usage_before"] = usage_snapshot(ordinary)
        # Exclusive creation prevents two processes from owning one recovery journal.
        with journal.open("x") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        driver = Policies(admin, ordinary, plan, journal, state)
        driver.exercise()
        state["usage_after"] = usage_snapshot(ordinary)
        common.require(
            state["usage_before"] == state["usage_after"],
            "Policy lifecycle changed usage or crossed a period boundary; evidence is incomplete",
        )
        state["checks"].append("settled_usage_unchanged_after_policy_removal")
        driver.persist()
    evidence.update(success=True, stage="complete")
