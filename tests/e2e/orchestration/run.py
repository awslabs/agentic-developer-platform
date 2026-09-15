"""Qualification runner CLI (#5156).

    python -m tests.e2e.orchestration.run --config <path> --preflight
    python -m tests.e2e.orchestration.run --config <path> --run
    python -m tests.e2e.orchestration.run --config <path> --resume <qualification-id>
    python -m tests.e2e.orchestration.run --config <path> --cleanup <qualification-id>

The four modes are mutually exclusive and one is required; combining them is an
error rather than a silently chosen precedence. ``--preflight`` is read-only: it
validates the config, checks the actual target account and authorization, and
reports the resources a run WOULD create, without mutating anything.

Scenario adapters come from #5157 and are discovered through the registry in
:mod:`tests.e2e.orchestration.scenarios` when that package exists. Until then
the registry is empty, and an empty registry can never report PASS: the run
exits with :data:`EXIT_NO_SCENARIOS` and status ``incomplete``. A qualification
that executed nothing is not a passing qualification.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Sequence

from tests.e2e.orchestration.config import ConfigError, QualificationConfig, load_config
from tests.e2e.orchestration.fixtures import FixtureProvider, cleanup, resume
from tests.e2e.orchestration.inventory import (
    CREATED,
    RECONCILE_FAILED,
    Inventory,
    InventoryError,
    new_qualification_id,
    sanitize_evidence,
)

# Distinct exit codes so CI can tell "nothing ran" apart from "something failed".
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG_INVALID = 3
EXIT_NO_SCENARIOS = 4
EXIT_FAILED = 5
EXIT_INCOMPLETE = 6

STATUS_PASS = "pass"
STATUS_INCOMPLETE = "incomplete"
STATUS_FAILED = "failed"
STATUS_READY = "ready"


@dataclass(frozen=True)
class Outcome:
    """The runner's result: a status, an exit code and a JSON-able report."""

    status: str
    exit_code: int
    report: dict[str, Any]


def load_scenario_adapters(config: QualificationConfig) -> dict[str, Any]:
    """Discover the scenario adapters supplied by #5157.

    Returns a possibly-empty mapping of scenario id to adapter. An empty result
    is a legitimate state today (#5157 has not landed) and is handled by the
    callers as "nothing ran", never as a pass.
    """
    try:
        from tests.e2e.orchestration import scenarios  # type: ignore[attr-defined]
    except ImportError:
        return {}

    registry = getattr(scenarios, "REGISTRY", None)
    if not isinstance(registry, dict):
        return {}
    if not config.scenarios:
        return dict(registry)
    # A config naming a scenario the registry does not provide is an error, not
    # a silently smaller run.
    missing = [name for name in config.scenarios if name not in registry]
    if missing:
        raise ConfigError([f"config names scenario(s) with no registered adapter: {', '.join(sorted(missing))}"])
    return {name: registry[name] for name in config.scenarios}


def load_providers(config: QualificationConfig) -> dict[str, FixtureProvider]:
    """Collect fixture providers from the discovered scenario adapters.

    Providers live with the adapters (#5157) because the resource kinds a
    scenario needs are the scenario's business, not the harness's.
    """
    providers: dict[str, FixtureProvider] = {}
    for adapter in load_scenario_adapters(config).values():
        for provider in getattr(adapter, "providers", ()):
            providers[provider.kind] = provider
    return providers


def preflight(config: QualificationConfig) -> Outcome:
    """Read-only check of the real target, authorization and planned resources.

    Mutates nothing: no inventory is written, no fixture is created and no
    secret is resolved. The caller-identity read is the only AWS call, and a
    failure to make it is reported rather than assumed away.
    """
    adapters = load_scenario_adapters(config)
    report: dict[str, Any] = {
        "mode": "preflight",
        "environment": config.environment,
        "repository": config.repository,
        "connection_ref": config.connection_ref,
        "versions": dict(config.versions),
        "bounds": dict(config.bounds),
        "artifact_directory": str(config.artifact_directory),
        "secret_refs": sorted(config.secret_refs),  # names only, never values
        "scenarios": sorted(adapters),
        "planned_resources": [],
        "mutations": [],
    }

    identity, identity_error = _caller_identity()
    report["caller_identity"] = identity
    if identity_error:
        report["authorization_error"] = identity_error

    for name, adapter in sorted(adapters.items()):
        planner = getattr(adapter, "planned_fixtures", None)
        planned = list(planner(config)) if callable(planner) else []
        report["planned_resources"].extend(
            {"scenario": name, "fixture_id": item.fixture_id, "kind": item.kind} for item in planned
        )

    if identity_error:
        report["detail"] = "target account and authorization could not be verified"
        return Outcome(STATUS_FAILED, EXIT_FAILED, report)

    if not adapters:
        report["detail"] = (
            "no scenario adapters are registered, so a run would execute nothing; "
            "scenario adapters are supplied by #5157"
        )
        return Outcome(STATUS_INCOMPLETE, EXIT_NO_SCENARIOS, report)

    report["detail"] = "config and target verified; no mutation performed"
    return Outcome(STATUS_READY, EXIT_OK, report)


def _caller_identity() -> tuple[dict[str, str] | None, str | None]:
    """Read the real caller identity. Returns ``(identity, error)``."""
    try:
        import boto3

        identity = boto3.client("sts").get_caller_identity()
    except Exception as exc:
        return None, f"could not verify the target account via sts:GetCallerIdentity: {exc}"
    return {
        "account": str(identity.get("Account", "")),
        "arn": str(identity.get("Arn", "")),
    }, None


def run(config: QualificationConfig) -> Outcome:
    """Execute the qualification. Refuses to report PASS if nothing ran."""
    adapters = load_scenario_adapters(config)
    if not adapters:
        # The central guarantee: an empty registry is never a pass. No
        # inventory is created either, because there is nothing to provision.
        return Outcome(
            STATUS_INCOMPLETE,
            EXIT_NO_SCENARIOS,
            {
                "mode": "run",
                "environment": config.environment,
                "scenarios_executed": 0,
                "detail": (
                    "no scenario adapters are registered: nothing was executed and this run is "
                    "NOT a pass. Scenario adapters are supplied by #5157."
                ),
            },
        )

    qualification_id = new_qualification_id()
    inventory = Inventory.create(config.artifact_directory, qualification_id, config.environment)
    providers = load_providers(config)

    executed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for name, adapter in sorted(adapters.items()):
        if len(executed) >= config.max_runs:
            failures.append({"scenario": name, "error": f"bounds.max_runs={config.max_runs} reached"})
            break
        try:
            adapter.execute(config=config, inventory=inventory, providers=providers)
        except Exception as exc:
            failures.append({"scenario": name, "error": str(exc)})
            continue
        executed.append({"scenario": name})

    report: dict[str, Any] = {
        "mode": "run",
        "qualification_id": qualification_id,
        "environment": config.environment,
        "inventory": str(inventory.path),
        "scenarios_executed": len(executed),
        "scenarios_failed": len(failures),
        "failures": failures,
        "live_resources": inventory.live_resource_count(),
    }

    if failures:
        report["detail"] = "at least one scenario failed; fixtures are retained for --resume or --cleanup"
        return Outcome(STATUS_FAILED, EXIT_FAILED, report)
    if not executed:
        report["detail"] = "no scenario executed, so this run is not a pass"
        return Outcome(STATUS_INCOMPLETE, EXIT_NO_SCENARIOS, report)
    report["detail"] = "all registered scenarios executed"
    return Outcome(STATUS_PASS, EXIT_OK, report)


def resume_qualification(config: QualificationConfig, qualification_id: str) -> Outcome:
    """Reconcile a partially provisioned qualification."""
    inventory = Inventory.load(config.artifact_directory, qualification_id, config.environment)
    unresolved = resume(inventory, load_providers(config))
    stuck = inventory.in_state(RECONCILE_FAILED)

    report = {
        "mode": "resume",
        "qualification_id": qualification_id,
        "environment": config.environment,
        "inventory": str(inventory.path),
        "reconciled": [r.fixture_id for r in inventory.in_state(CREATED)],
        "unresolved": [r.fixture_id for r in unresolved],
        "reconcile_failed": [{"fixture_id": r.fixture_id, "detail": r.detail} for r in stuck],
    }
    if stuck:
        report["detail"] = (
            "some fixtures could not be reconciled and may be leaked; they are recorded as "
            "reconcile_failed and need a human"
        )
        return Outcome(STATUS_FAILED, EXIT_FAILED, report)
    report["detail"] = "inventory reconciled against the provider"
    return Outcome(STATUS_READY, EXIT_OK, report)


def cleanup_qualification(config: QualificationConfig, qualification_id: str) -> Outcome:
    """Delete verified owned fixtures and retain sanitized evidence."""
    inventory = Inventory.load(config.artifact_directory, qualification_id, config.environment)
    outcome = cleanup(inventory, config, load_providers(config))

    report = {
        "mode": "cleanup",
        "qualification_id": qualification_id,
        "environment": config.environment,
        "inventory": str(inventory.path),
        "deleted": list(outcome.deleted),
        "refused": [{"fixture_id": f, "reason": r} for f, r in outcome.refused],
        "failed": [{"fixture_id": f, "error": e} for f, e in outcome.failed],
        # Evidence is retained after cleanup so a leak stays investigable.
        "evidence": sanitize_evidence(inventory.fixtures),
    }
    if outcome.failed:
        report["detail"] = "one or more deletions failed; fixtures may still exist"
        return Outcome(STATUS_FAILED, EXIT_FAILED, report)
    if outcome.refused:
        report["detail"] = (
            "cleanup completed for verified fixtures; others were refused because ownership "
            "could not be positively verified and were left untouched"
        )
        return Outcome(STATUS_INCOMPLETE, EXIT_INCOMPLETE, report)
    report["detail"] = "all recorded fixtures deleted after ownership verification"
    return Outcome(STATUS_PASS, EXIT_OK, report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.e2e.orchestration.run",
        description="Run a bounded qualification with a recoverable fixture inventory.",
    )
    parser.add_argument("--config", required=True, help="path to the qualification config JSON")
    # A mutually exclusive required group turns "--run --cleanup X" into a usage
    # error instead of a guess about which mode the operator meant.
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true", help="read-only validation; mutates nothing")
    mode.add_argument("--run", action="store_true", help="execute the qualification")
    mode.add_argument("--resume", metavar="QUALIFICATION_ID", help="reconcile a partial qualification")
    mode.add_argument("--cleanup", metavar="QUALIFICATION_ID", help="delete verified owned fixtures")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_INVALID

    try:
        if args.preflight:
            outcome = preflight(config)
        elif args.run:
            outcome = run(config)
        elif args.resume:
            outcome = resume_qualification(config, args.resume)
        else:
            outcome = cleanup_qualification(config, args.cleanup)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_INVALID
    except InventoryError as exc:
        print(f"inventory error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(json.dumps({"status": outcome.status, **outcome.report}, indent=2, sort_keys=True))
    if outcome.status != STATUS_PASS:
        print(
            f"\nstatus={outcome.status} exit={outcome.exit_code}: {outcome.report.get('detail', '')}",
            file=sys.stderr,
        )
    return outcome.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
