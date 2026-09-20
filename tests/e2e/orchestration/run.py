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
import importlib
import json
import os
from datetime import UTC, datetime
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tests.e2e.orchestration.config import (
    ConfigError,
    ConnectionResolver,
    QualificationConfig,
    TargetVerification,
    load_config,
    verify_target,
)
from tests.e2e.orchestration.fixtures import FixtureProvider, cleanup, resume
from tests.e2e.orchestration.inventory import (
    CREATED,
    RECONCILE_FAILED,
    Inventory,
    InventoryError,
    new_qualification_id,
    restore_inventory,
    sanitize_evidence,
)

# Distinct exit codes so CI can tell "nothing ran" apart from "something failed".
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG_INVALID = 3
EXIT_NO_SCENARIOS = 4
EXIT_FAILED = 5
EXIT_INCOMPLETE = 6
# Distinct from EXIT_FAILED: "we refused to touch this account" is a different
# operator action from "the qualification ran and failed".
EXIT_TARGET_UNVERIFIED = 7

STATUS_PASS = "pass"
STATUS_INCOMPLETE = "incomplete"
STATUS_FAILED = "failed"
STATUS_READY = "ready"
STATUS_REFUSED = "refused"


@dataclass(frozen=True)
class Outcome:
    """The runner's result: a status, an exit code and a JSON-able report."""

    status: str
    exit_code: int
    report: dict[str, Any]


def _scenarios_module() -> Any:
    """Return #5157's scenarios module, or ``None`` while it has not landed."""
    try:
        scenarios = importlib.import_module("tests.e2e.orchestration.scenarios")
    except ImportError:
        return None
    return scenarios


def load_scenario_adapters(config: QualificationConfig) -> dict[str, Any]:
    """Discover the scenario adapters supplied by #5157.

    Returns a possibly-empty mapping of scenario id to adapter. An empty result
    is a legitimate state today (#5157 has not landed) and is handled by the
    callers as "nothing ran", never as a pass.
    """
    scenarios = _scenarios_module()
    if scenarios is None:
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
        raise ConfigError(
            [
                f"config names scenario(s) with no registered adapter: {', '.join(sorted(missing))}"
            ]
        )
    return {name: registry[name] for name in config.scenarios}


def load_providers(config: QualificationConfig) -> dict[str, FixtureProvider]:
    """Collect fixture providers from the discovered scenario adapters.

    Providers live with the adapters (#5157) because the resource kinds a
    scenario needs are the scenario's business, not the harness's.
    """
    providers: dict[str, FixtureProvider] = {}
    for adapter in load_scenario_adapters(config).values():
        factory = getattr(adapter, "fixture_providers", None)
        available = (
            factory(config) if callable(factory) else getattr(adapter, "providers", ())
        )
        for provider in available:
            providers[provider.kind] = provider
    return providers


def load_connection_resolver(config: QualificationConfig) -> ConnectionResolver | None:
    """Find the connection registry lookup, if one is available.

    Discovered from #5157's slot exactly like :func:`load_providers`, because the
    registry that knows which connections are registered is the platform's, not
    this harness's. Returns ``None`` when nothing supplies one, which
    :func:`verify_target` treats as a refusal — an unverifiable target must never
    fall back to the config's own claim about itself.
    """
    module = _scenarios_module()
    factory = getattr(module, "connection_resolver", None)
    if callable(factory):
        return factory(config)
    resolver = getattr(module, "CONNECTION_RESOLVER", None)
    if resolver is not None:
        return resolver
    for adapter in load_scenario_adapters(config).values():
        candidate = getattr(adapter, "connection_resolver", None)
        if candidate is not None:
            return candidate
    return None


def verified_target(config: QualificationConfig) -> TargetVerification:
    """Resolve the selected connection and check it against the real identity.

    Two reads: the connection registry (which account/org is this ref actually
    authorized for?) and one ``sts:GetCallerIdentity`` (which account are we
    really in?). Every mutating mode calls this first.
    """
    identity, identity_error = _caller_identity()
    return verify_target(
        config, identity, identity_error, load_connection_resolver(config)
    )


def _refusal(
    mode: str, config: QualificationConfig, target: TargetVerification, **extra: Any
) -> Outcome:
    """The outcome for a mode that refused to mutate an unverified target."""
    return Outcome(
        STATUS_REFUSED,
        EXIT_TARGET_UNVERIFIED,
        {
            "mode": mode,
            "environment": config.environment,
            "connection_ref": config.connection_ref,
            "target": target.to_json(),
            "mutations": [],
            "detail": target.reason,
            **extra,
        },
    )


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
        "expected_account_id": config.expected_account_id,
        "expected_org": config.expected_org,
        "versions": dict(config.versions),
        "bounds": dict(config.bounds),
        "artifact_directory": str(config.artifact_directory),
        "secret_refs": sorted(config.secret_refs),  # names only, never values
        "scenarios": sorted(adapters),
        "planned_resources": [],
        "mutations": [],
    }

    identity, identity_error = _caller_identity()
    target = verify_target(
        config, identity, identity_error, load_connection_resolver(config)
    )
    report["caller_identity"] = identity
    report["target"] = target.to_json()
    if identity_error:
        report["authorization_error"] = identity_error

    for name, adapter in sorted(adapters.items()):
        planner = getattr(adapter, "planned_fixtures", None)
        planned = list(planner(config)) if callable(planner) else []
        report["planned_resources"].extend(
            {"scenario": name, "fixture_id": item.fixture_id, "kind": item.kind}
            for item in planned
        )

    from tests.e2e.orchestration.fixtures import resource_units

    report["planned_resource_units"] = sum(
        resource_units(row["kind"]) for row in report["planned_resources"]
    )
    if report["planned_resource_units"] > config.max_resources:
        report["detail"] = "planned fixtures exceed the accepted resource bound"
        return Outcome(STATUS_INCOMPLETE, EXIT_INCOMPLETE, report)

    # Preflight reports rather than refuses — it mutates nothing either way — but
    # it reports the COMPARISON, so an operator sees a mismatch before dispatching
    # a run that would be refused.
    if not target.verified:
        report["detail"] = target.reason
        return Outcome(STATUS_FAILED, EXIT_TARGET_UNVERIFIED, report)

    if not adapters:
        report["detail"] = (
            "no scenario adapters are registered, so a run would execute nothing; scenario adapters are supplied by #5157"
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
        return (
            None,
            f"could not verify the target account via sts:GetCallerIdentity: {exc}",
        )
    return {
        "account": str(identity.get("Account", "")),
        "arn": str(identity.get("Arn", "")),
    }, None


def run(config: QualificationConfig, *, evaluation_context=None) -> Outcome:
    """Execute the qualification. Refuses to report PASS if nothing ran."""
    if evaluation_context is not None:
        from tests.e2e.orchestration.evaluation_receipt import validate_context

        validate_context(evaluation_context, config)
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
                    "no scenario adapters are registered: nothing was executed and this run is NOT a pass. Scenario adapters are supplied by #5157."
                ),
            },
        )

    # Verified before the inventory is created: a refused run must leave no trace
    # in the artifact directory, exactly like preflight.
    target = verified_target(config)
    if not target.verified:
        return _refusal("run", config, target, scenarios_executed=0, attempts=0)

    qualification_id = new_qualification_id()
    inventory = Inventory.create(
        config.artifact_directory, qualification_id, config.environment
    )
    providers = load_providers(config)

    started_at = datetime.now(UTC)
    observations = []
    executed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    scenario_reports = []
    attempts = 0
    for name, adapter in sorted(adapters.items()):
        # Every ATTEMPT counts against max_runs, including one that fails. A cap
        # that only counted successes would let a repeatedly-failing adapter
        # invoke the engine without limit — each failed attempt still consumed
        # the run it was budgeted for.
        if attempts >= config.max_runs:
            failures.append(
                {
                    "scenario": name,
                    "error": (
                        f"bounds.max_runs={config.max_runs} reached after {attempts} attempt(s); not invoked"
                    ),
                }
            )
            break
        attempts += 1
        try:
            if evaluation_context is not None and callable(
                getattr(adapter, "execute_evaluation", None)
            ):
                observed = adapter.execute_evaluation(
                    config=config,
                    inventory=inventory,
                    providers=providers,
                    context=evaluation_context,
                )
            else:
                observed = adapter.execute(
                    config=config, inventory=inventory, providers=providers
                )
            from tests.e2e.orchestration.report import ScenarioReport, write_report

            if isinstance(observed, ScenarioReport):
                from tests.e2e.orchestration.scenarios.manifest import load_manifest

                _, expected_manifest_hash = load_manifest(config)
                result = write_report(
                    observed,
                    config=config,
                    inventory=inventory,
                    manifest_hash=expected_manifest_hash,
                )
                scenario_reports.append(str(inventory.path.parent / "report.json"))
                if result["overall"] != "PASS":
                    incomplete.append(
                        {"scenario": name, "blockers": result["blockers"]}
                    )
            elif evaluation_context is not None and isinstance(observed, dict):
                observations.append(observed)
            else:
                incomplete.append(
                    {
                        "scenario": name,
                        "blockers": [
                            "adapter returned no validated criterion evidence"
                        ],
                    }
                )
        except Exception as exc:  # any adapter failure is data, not a crash
            # One scenario failing must not skip the others or abandon the
            # inventory: the run reports FAILED and the fixtures stay cleanable.
            failures.append({"scenario": name, "error": str(exc)})
            continue
        executed.append({"scenario": name})

    report: dict[str, Any] = {
        "mode": "run",
        "qualification_id": qualification_id,
        "environment": config.environment,
        "target": target.to_json(),
        "inventory": str(inventory.path),
        "attempts": attempts,
        "scenarios_executed": len(executed),
        "scenarios_failed": len(failures),
        "failures": failures,
        "incomplete": incomplete,
        "scenario_reports": scenario_reports,
        "live_resources": inventory.live_resource_count(),
    }

    if evaluation_context is not None:
        from tests.e2e.orchestration.evaluation_receipt import emit

        try:
            report["evaluation_bundle"] = str(
                emit(
                    evaluation_context,
                    observations,
                    config=config,
                    target=target,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                )
            )
        except (ValueError, KeyError, OSError, TypeError):
            report["detail"] = (
                "evaluation evidence was incomplete or unverifiable; fixtures remain in the inventory"
            )
            return Outcome(STATUS_INCOMPLETE, EXIT_INCOMPLETE, report)

    if failures:
        report["detail"] = (
            "at least one scenario failed; fixtures are retained for --resume or --cleanup"
        )
        return Outcome(STATUS_FAILED, EXIT_FAILED, report)
    if incomplete:
        report["detail"] = (
            "mandatory criterion evidence is incomplete; this is not a qualifying PASS"
        )
        return Outcome(STATUS_INCOMPLETE, EXIT_INCOMPLETE, report)
    if not executed:
        report["detail"] = "no scenario executed, so this run is not a pass"
        return Outcome(STATUS_INCOMPLETE, EXIT_NO_SCENARIOS, report)
    report["detail"] = "all registered scenarios executed"
    return Outcome(STATUS_PASS, EXIT_OK, report)


def resume_qualification(config: QualificationConfig, qualification_id: str) -> Outcome:
    """Reconcile a partially provisioned qualification."""
    # Resume adopts and re-creates real resources, so it is a mutation and gets
    # the same target gate as a run.
    target = verified_target(config)
    if not target.verified:
        return _refusal("resume", config, target, qualification_id=qualification_id)

    inventory = Inventory.load(
        config.artifact_directory, qualification_id, config.environment
    )
    unresolved = resume(inventory, load_providers(config))
    stuck = inventory.in_state(RECONCILE_FAILED)

    report = {
        "mode": "resume",
        "qualification_id": qualification_id,
        "environment": config.environment,
        "target": target.to_json(),
        "inventory": str(inventory.path),
        "reconciled": [r.fixture_id for r in inventory.in_state(CREATED)],
        "unresolved": [r.fixture_id for r in unresolved],
        "reconcile_failed": [
            {"fixture_id": r.fixture_id, "detail": r.detail} for r in stuck
        ],
    }
    if stuck:
        report["detail"] = (
            "some fixtures could not be reconciled and may be leaked; they are recorded as reconcile_failed and need a human"
        )
        return Outcome(STATUS_FAILED, EXIT_FAILED, report)
    report["detail"] = "inventory reconciled against the provider"
    return Outcome(STATUS_READY, EXIT_OK, report)


def cleanup_qualification(
    config: QualificationConfig, qualification_id: str
) -> Outcome:
    """Delete verified owned fixtures and retain sanitized evidence."""
    # Deletion against the wrong account is the worst outcome available here, so
    # cleanup verifies the target before reading the inventory.
    target = verified_target(config)
    if not target.verified:
        return _refusal(
            "cleanup", config, target, qualification_id=qualification_id, deleted=[]
        )

    inventory = Inventory.load(
        config.artifact_directory, qualification_id, config.environment
    )
    outcome = cleanup(inventory, config, load_providers(config))

    report = {
        "mode": "cleanup",
        "qualification_id": qualification_id,
        "environment": config.environment,
        "target": target.to_json(),
        "inventory": str(inventory.path),
        "deleted": list(outcome.deleted),
        "retained_audit": list(outcome.retained),
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
            "cleanup completed for verified fixtures; others were refused because ownership could not be positively verified and were left untouched"
        )
        return Outcome(STATUS_INCOMPLETE, EXIT_INCOMPLETE, report)
    report["detail"] = "all recorded fixtures deleted after ownership verification"
    return Outcome(STATUS_PASS, EXIT_OK, report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.e2e.orchestration.run",
        description="Run a bounded qualification with a recoverable fixture inventory.",
    )
    parser.add_argument(
        "--config", required=True, help="path to the qualification config JSON"
    )
    # A mutually exclusive required group turns "--run --cleanup X" into a usage
    # error instead of a guess about which mode the operator meant.
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preflight", action="store_true", help="read-only validation; mutates nothing"
    )
    mode.add_argument("--run", action="store_true", help="execute the qualification")
    mode.add_argument(
        "--resume", metavar="QUALIFICATION_ID", help="reconcile a partial qualification"
    )
    mode.add_argument(
        "--cleanup", metavar="QUALIFICATION_ID", help="delete verified owned fixtures"
    )
    # Not a mode: a modifier for resume/cleanup, which in a separate workflow run
    # start with an empty workspace and no inventory to act on.
    parser.add_argument(
        "--restore-from",
        metavar="DIRECTORY",
        help=(
            "directory holding the originating run's downloaded artifact; its verified inventory "
            "is copied into the configured artifact directory before --resume or --cleanup"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_INVALID

    if args.restore_from and not (args.resume or args.cleanup):
        parser.error("--restore-from applies only to --resume or --cleanup")

    evaluation_context = None
    raw_context = os.environ.get("ORCHESTRATION_EVALUATION_CONTEXT", "")
    if raw_context:
        if not args.run:
            parser.error("evaluation evidence applies only to --run")
        from tests.e2e.orchestration.evaluation_receipt import (
            parse_context,
            validate_context,
        )

        try:
            evaluation_context = parse_context(raw_context)
            validate_context(evaluation_context, config)
        except ValueError:
            print("evaluation context is invalid", file=sys.stderr)
            return EXIT_CONFIG_INVALID

    try:
        if args.restore_from:
            qualification_id = args.resume or args.cleanup
            restored = restore_inventory(
                config.artifact_directory,
                qualification_id,
                args.restore_from,
                config.environment,
            )
            print(
                f"restored inventory for {qualification_id} to {restored}",
                file=sys.stderr,
            )

        if args.preflight:
            outcome = preflight(config)
        elif args.run:
            outcome = (
                run(config, evaluation_context=evaluation_context)
                if evaluation_context
                else run(config)
            )
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

    print(
        json.dumps(
            {"status": outcome.status, **outcome.report}, indent=2, sort_keys=True
        )
    )
    if outcome.status != STATUS_PASS:
        print(
            f"\nstatus={outcome.status} exit={outcome.exit_code}: {outcome.report.get('detail', '')}",
            file=sys.stderr,
        )
    return outcome.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
