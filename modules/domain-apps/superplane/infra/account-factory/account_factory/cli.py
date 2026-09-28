"""Offline entry point: validate, render, plan cleanup — Issue #5530 (w6-07).

## There is deliberately no `apply` subcommand

The legacy flow's provisioning step was `echo "$MANIFEST" | kubectl apply -f -` — building the
manifest and mutating the cluster in one statement, so the only way to see what it would do
was to let it do it. The issue asks for plan/apply boundaries to be explicit, and the way this
module makes them explicit is that the plan side exists here and the apply side does not exist
at all.

So every subcommand below is read-only and offline: it writes to stdout, and nothing else.
There is no code path from this module to a subprocess, a socket, an AWS client or a
Kubernetes client — `tests/test_no_legacy_targets.py` asserts that as a property of the source.
Applying reviewed output is a separate, separately-authorized operation performed by an
operator or a lane that has been given credentials, which this module never touches.

Issue creation and code merge authorize none of that: no account vending, no AWS or Kubernetes
apply, no feature activation and no workload spend. This CLI is how the offline half is
exercised and reviewed.

## Subcommands

* `validate`     — refuse or accept a request, and report which authorization comparisons were
                   NOT made (so "unchecked" is never mistaken for "passed").
* `render`       — the object set a request would apply, plus the shared prerequisites as a
                   separate list, as YAML or JSON.
* `cleanup-plan` — what a workspace teardown would delete, and what it deliberately retains.
* `creation-status` — whether CreateAccount may be called for a request, given what is
                   recorded about any previous attempt. Exits non-zero when it may not, so a
                   duplicate or an unresolved outcome cannot be walked past.
* `bootstrap-plan` — what child-account bootstrap must establish, in the order it must be
                   established, including the Auto Scaling service-linked role a workspace KMS
                   key policy depends on. A description; nothing is created or read.
* `recovery-report` — for a create-succeeded/bootstrap-failed account: what exists, what is
                   incomplete, what a retry would and would not repeat, and what is retained.
                   Exits non-zero while the account is not established-and-fully-read.
* `dependencies` — the verified pin set, and what would happen if it could not be verified.

Exit codes: 0 accepted, 1 refused (invalid request, creation not permitted, bootstrap refused,
an account not established or possibly unaccounted for, unverifiable dependency set, or output
that failed its own checks), 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import bootstrap, cleanup, creation, dependencies, recovery
from . import render as render_module
from .modes import (
    ModeError,
    OwnershipMode,
    ValidationAuthorization,
    from_mapping,
    validate,
)

__all__ = ["main"]

_EXIT_OK = 0
_EXIT_REFUSED = 1
_EXIT_USAGE = 2


def _load_request(path: Path):
    """Parse a request file. Refuses unknown and missing fields via `from_mapping`."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModeError(f"could not read the request file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ModeError(f"{path} is not valid YAML: {exc}") from exc
    return from_mapping(data)


def _authorization(args) -> ValidationAuthorization | None:
    """Build authorization from explicit flags only.

    Nothing is inferred from the ambient environment. The legacy check read the account from
    the same config file it was validating, which only ever confirmed the file matched itself;
    an authorization that came from the environment being acted on would repeat that mistake.
    """
    modes = None
    if args.permit_mode:
        try:
            modes = frozenset(OwnershipMode(value) for value in args.permit_mode)
        except ValueError as exc:
            raise ModeError(f"--permit-mode: {exc}") from exc
    target_accounts = None
    if args.authorized_target_account:
        target_accounts = frozenset(args.authorized_target_account)
    organizational_units = None
    if args.authorized_organizational_unit:
        organizational_units = frozenset(args.authorized_organizational_unit)
    supplied = (
        args.authorized_organization,
        args.authorized_management_account,
        args.authorized_management_cluster,
        modes,
        args.authorized_workspace,
        target_accounts,
        organizational_units,
    )
    if not any(value is not None for value in supplied):
        return None
    return ValidationAuthorization(
        organization_id=args.authorized_organization,
        management_account_id=args.authorized_management_account,
        management_cluster=args.authorized_management_cluster,
        permitted_modes=modes,
        # Taken from a flag here because this CLI is an offline operator tool with no facade
        # to resolve a principal from. In the served path the workspace comes from the
        # binding's resolved principal (`ValidationAuthorization.from_operation_binding`) and
        # is not caller-supplied at all. The flag is named `--authorized-workspace`, separate
        # from the request's own `workspace_id`, so that supplying it is an explicit statement
        # about authority rather than a restatement of the request.
        workspace_id=args.authorized_workspace,
        permitted_target_accounts=target_accounts,
        permitted_organizational_units=organizational_units,
    )


def _emit(payload, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            yaml.safe_dump(payload, sort_keys=False, default_flow_style=False).rstrip()
        )


def _report_unchecked(unchecked) -> None:
    """Print unchecked comparisons to stderr, never silently.

    An absent authorization value means a comparison was NOT MADE. Reporting it as a warning
    keeps "not verified" visually distinct from "verified", which is the distinction a
    completion report depends on.
    """
    if not unchecked:
        return
    print(
        "NOT VERIFIED (no authorization value supplied for these; they were not compared, "
        "which is not the same as having passed): " + ", ".join(sorted(unchecked)),
        file=sys.stderr,
    )


def _cmd_validate(args) -> int:
    request = _load_request(args.config)
    problems, unchecked = validate(request, _authorization(args))
    if problems:
        print(
            f"REFUSED before any mutation — {len(problems)} problem(s):",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return _EXIT_REFUSED
    print(f"accepted: mode={request.mode.value} workspace={request.workspace_id}")
    print(f"cluster ownership: {request.cluster_ownership.value}")
    _report_unchecked(unchecked)
    return _EXIT_OK


def _cmd_render(args) -> int:
    request = _load_request(args.config)
    result = render_module.render(
        request, _authorization(args), account_id=args.account_id
    )
    _emit(
        {
            "mode": request.mode.value,
            "workspace": request.workspace_id,
            "namespace": result.namespace,
            "cluster_ownership": request.cluster_ownership.value,
            # Stated in the output because it is the fact that makes the rendered set safe:
            # the account was opened by the fenced creation path and this manifest only binds
            # to it. `null` for the adopting modes, which create no account at all.
            "bound_to_account_opened_by_governed_path": args.account_id,
            # The shared installs are a SEPARATE list, never merged into `objects`. Applying
            # `objects` provisions one workspace and changes nothing shared.
            "prerequisites_run_once_per_management_cluster": [
                {
                    "name": operation.name,
                    "reason": operation.reason,
                    "scope": operation.scope,
                    "command": list(operation.command),
                }
                for operation in result.prerequisites
            ],
            "objects": [dict(obj) for obj in result.objects],
            "vendored_resource_graphs": list(result.resource_graph_files),
            "authorization_not_verified": list(result.unchecked_authorization),
            "applied": False,
            "note": (
                "Rendered offline. Nothing was applied and no AWS or Kubernetes call was "
                "made. Applying this output is a separate authorized operation."
            ),
        },
        args.format,
    )
    _report_unchecked(result.unchecked_authorization)
    return _EXIT_OK


def _cmd_cleanup_plan(args) -> int:
    request = _load_request(args.config)
    plan = cleanup.plan(request, _authorization(args))
    _emit(
        {
            "mode": request.mode.value,
            "workspace": request.workspace_id,
            "scope": plan.scope.value,
            "closes_account": plan.closes_account,
            "delete": [
                {
                    "kind": action.kind,
                    "name": action.name,
                    "namespace": action.namespace,
                    "reason": action.reason,
                }
                for action in plan.actions
            ],
            "retained": list(plan.retained),
            "deleted": False,
            "note": (
                "A plan, not an execution. Nothing was deleted. Account closure is never "
                "part of a workspace cleanup and must be requested explicitly."
            ),
        },
        args.format,
    )
    return _EXIT_OK


def _load_ledger(path: Path | None) -> creation.AttemptLedger:
    """Load the recorded creation attempts, or an empty ledger when none is supplied.

    An absent `--attempt-ledger` means no attempt was recorded, which is the honest reading
    for a first run. A path that was supplied but cannot be read is a REFUSAL, not an empty
    ledger: treating an unreadable store as "nothing has been attempted" is exactly how a
    second account gets opened for a workspace that already has one.
    """
    if path is None:
        return creation.AttemptLedger()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise creation.AccountCreationError(
            f"could not read the attempt ledger {path}: {exc}. An unreadable ledger is not "
            f"an empty one — proceeding could open a duplicate account"
        ) from exc
    except yaml.YAMLError as exc:
        raise creation.AccountCreationError(
            f"the attempt ledger {path} is not valid YAML: {exc}"
        ) from exc
    return creation.AttemptLedger.from_records(data)


def _cmd_creation_status(args) -> int:
    """Classify offline evidence without authorizing an AWS effect.

    Always exits nonzero: this CLI cannot authenticate an operation or commit
    the durable generation that every first call and retry requires.
    """
    request = _load_request(args.config)
    ledger = _load_ledger(args.attempt_ledger)
    observation = None
    if args.aws_unreadable is not None:
        observation = creation.CreateAccountObservation.unreadable(args.aws_unreadable)
    elif args.aws_status is not None:
        failure = None
        if args.aws_failure is not None:
            try:
                failure = creation.CreateAccountFailure(args.aws_failure)
            except ValueError as exc:
                raise creation.AccountCreationError(f"--aws-failure: {exc}") from exc
        try:
            status = creation.CreateAccountStatus(args.aws_status)
        except ValueError as exc:
            raise creation.AccountCreationError(f"--aws-status: {exc}") from exc
        observation = creation.CreateAccountObservation(
            status=status,
            create_account_request_id=args.aws_request_id,
            failure=failure,
            account_id=args.aws_account_id,
            detail=args.aws_detail or "",
        )

    decision = creation.assess_attempt(
        request, ledger, observation, _authorization(args)
    )
    # This interface has no trusted store or admitted executor. Even complete
    # caller-supplied flags cannot grant execution authority or persist a retry.
    intended = None

    _emit(
        {
            "mode": request.mode.value,
            "workspace": request.workspace_id,
            "disposition": decision.disposition.value,
            "may_create_account": decision.may_create_account,
            "reason": decision.reason,
            "account_possibly_unaccounted_for": decision.account_unaccounted_for,
            "recorded_attempt": (
                decision.attempt.as_record() if decision.attempt else None
            ),
            "record_before_calling": intended,
            "created": False,
            "note": (
                "Offline analysis only; this command never authorizes CreateAccount. No AWS call was made. "
                "Use the maintained account-provisioning runner with an admitted operation, "
                "trusted durable history and a committed generation for every attempt."
            ),
        },
        args.format,
    )
    if not decision.may_create_account:
        print(
            f"CREATION NOT PERMITTED ({decision.disposition.value}): {decision.reason}",
            file=sys.stderr,
        )
        return _EXIT_REFUSED
    return _EXIT_OK


def _cmd_bootstrap_plan(args) -> int:
    """Describe what bootstrapping this request's child account requires, in order.

    Emits `presence_meaning` alongside each rule rather than the slug alone: an operator told
    only `create-if-absent` has not been told that the read must come first, and a create that
    swallows an already-exists error cannot tell a present role from a denied call.
    """
    request = _load_request(args.config)
    plan = bootstrap.bootstrap_plan(request, _authorization(args))
    _emit(
        {
            "mode": request.mode.value,
            "workspace": plan.workspace_id,
            "organization": plan.organization_id,
            "steps": [
                {
                    "name": step.name,
                    "tier": step.tier.value if step.tier else None,
                    "presence": step.presence.value,
                    "presence_meaning": step.presence.meaning,
                    "reason": step.reason,
                    "command": list(step.command),
                    "scope": step.scope,
                    "precedes": step.precedes,
                    "adoptable_by_workspace": step.adoptable_by_workspace,
                    "retained_through_workspace_retirement": (
                        step.retained_through_workspace_retirement
                    ),
                    "if_denied": step.denial_remediation,
                }
                for step in plan.steps
            ],
            "account_wide_steps": [step.name for step in plan.account_wide_steps],
            "authorization_not_verified": list(plan.unchecked_authorization),
            "bootstrapped": False,
            "note": (
                "A description of what bootstrap must establish, in the order it must be "
                "established. Nothing was created, read or assumed: no role exists as a "
                "result of this command. The account-wide steps listed must never enter "
                "per-workspace Terraform state — a workspace that adopted one would delete "
                "it on teardown and break every other workspace in the account."
            ),
        },
        args.format,
    )
    _report_unchecked(plan.unchecked_authorization)
    return _EXIT_OK


def _cmd_recovery_report(args) -> int:
    """Report what exists, what is incomplete, what a retry repeats, and what is retained.

    Exits non-zero whenever the account is not established-and-fully-read, so a caller reading
    only the exit code cannot treat a half-built account as finished. `--observed` takes
    `step=state` pairs; any step not named becomes `not-checked`, which is neither present nor
    absent and blocks a retry rather than inviting one.
    """
    request = _load_request(args.config)
    authorization = _authorization(args)
    plan = bootstrap.bootstrap_plan(request, authorization)
    ledger = _load_ledger(args.attempt_ledger)
    decision = creation.assess_attempt(request, ledger, None, authorization)

    observed: dict[str, recovery.StepState] = {}
    details: dict[str, str] = {}
    for pair in args.observed or ():
        name, _, value = pair.partition("=")
        if not value:
            raise recovery.RecoveryError(
                f"--observed expects step=state, not {pair!r}. A state that cannot be parsed "
                f"must not fall back to a default: it would report an unread step as read"
            )
        try:
            observed[name] = recovery.StepState(value)
        except ValueError as exc:
            raise recovery.RecoveryError(f"--observed {name}: {exc}") from exc
    for pair in args.observed_detail or ():
        name, separator, value = pair.partition("=")
        if not separator or not value.strip():
            # Refused rather than stored empty. An empty detail on a denied step is what
            # `StepFinding` rejects as indistinguishable from a guess, and silently storing one
            # would turn a typo into that refusal, reported against the wrong cause.
            raise recovery.RecoveryError(
                f"--observed-detail expects step=text, not {pair!r}. An empty detail says "
                f"nothing about what was observed"
            )
        details[name] = value

    report = recovery.recovery_report(plan, decision, observed, details)
    blocking = recovery.blocking_prerequisites(report)
    _emit(
        {
            "workspace": report.workspace_id,
            "organization": report.organization_id,
            "account_id": report.account_id,
            "summary": report.summary,
            "creation_disposition": report.creation_disposition.value,
            "account_exists": report.account_exists,
            "account_may_exist_untracked": report.account_may_exist_untracked,
            "established": list(report.established),
            "incomplete": list(report.incomplete),
            "unchecked": list(report.unchecked),
            "denied": list(report.denied),
            "every_step_accounted_for": report.every_step_accounted_for,
            "account_is_usable": report.account_is_usable,
            "ready_for_workspace_provisioning": report.ready_for_workspace_provisioning,
            "blocks_workspace_provisioning": list(blocking),
            "authorization_not_verified": list(plan.unchecked_authorization),
            "retry_would_repeat": list(report.retry_would_repeat),
            "retry_would_skip": list(report.retry_would_skip),
            "retry_cannot_advance": list(report.retry_cannot_advance),
            "bootstrap_retry_is_safe": report.bootstrap_retry_is_safe,
            "creation_retry_is_safe": report.creation_retry_is_safe,
            "needs_operator": report.needs_operator,
            "retained": list(report.retained),
            "findings": [
                {
                    "name": finding.name,
                    "state": finding.state.value,
                    "detail": finding.detail,
                    "next_action": finding.next_action,
                }
                for finding in report.findings
            ],
            "recovered": False,
            "note": (
                "A report, not a recovery. Nothing was read, created, repaired or closed. "
                "Recovery never re-runs account creation and never closes an account: the "
                "account is retained, and closure is an irreversible 90-day suspension that "
                "must be requested by name. A step reported as not-checked is neither present "
                "nor absent — read it before acting on it."
            ),
        },
        args.format,
    )
    # Before the readiness verdict, not after it: a run that can exit 0 saying an account is
    # ready for a workspace must say in the same breath which ownership comparisons nobody made.
    _report_unchecked(plan.unchecked_authorization)
    if report.account_may_exist_untracked:
        print(
            "ACCOUNT POSSIBLY UNACCOUNTED FOR: the recorded creation attempt's outcome could "
            "not be established. Neither a retry nor releasing this workspace is authorized.",
            file=sys.stderr,
        )
        return _EXIT_REFUSED
    if not report.ready_for_workspace_provisioning:
        print(f"ACCOUNT NOT READY: {report.summary}", file=sys.stderr)
        if blocking:
            print(
                "Blocks workspace provisioning outright: " + ", ".join(blocking),
                file=sys.stderr,
            )
        return _EXIT_REFUSED
    return _EXIT_OK


def _cmd_dependencies(args) -> int:
    deps = dependencies.load()
    _emit(
        {
            "upstream_repository": deps.upstream_repository,
            "upstream_revision": deps.upstream_revision,
            "license": deps.license_name,
            "charts": [
                {
                    "name": chart.name,
                    "version": chart.version,
                    "reference": chart.oci_reference,
                    "namespace": chart.namespace,
                }
                for chart in deps.charts
            ],
            "vendored_resource_graphs": [
                {
                    "file": graph.filename,
                    "declares": graph.declares,
                    "sha256": graph.sha256,
                }
                for graph in deps.resource_graphs
            ],
            "verified": (
                "every chart is pinned by content digest and every vendored file matches "
                "its recorded checksum"
            ),
            "not_established": (
                "that any chart is installed, that it reconciles, or that these digests are "
                "what the registries serve today"
            ),
        },
        args.format,
    )
    return _EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m account_factory.cli",
        description=(
            "Offline Account Factory tooling. Validates, renders and plans; never applies. "
            "No AWS or Kubernetes call is made by any subcommand."
        ),
        epilog=(
            "There is no `apply` subcommand by design: the legacy flow rendered and applied "
            "in one statement, so nothing could be reviewed first."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser, *, needs_config=True):
        if needs_config:
            subparser.add_argument(
                "--config",
                type=Path,
                required=True,
                help="request file (see ../config.example.yaml)",
            )
        subparser.add_argument(
            "--format", choices=("yaml", "json"), default="yaml", help="output format"
        )
        subparser.add_argument(
            "--authorized-organization",
            help="organization id this run may act in; unset means the comparison is NOT made",
        )
        subparser.add_argument(
            "--authorized-management-account",
            help="management account id this run may act from",
        )
        subparser.add_argument(
            "--authorized-management-cluster",
            help="management cluster this run may act from",
        )
        subparser.add_argument(
            "--permit-mode",
            action="append",
            metavar="MODE",
            help="restrict the permitted modes; repeatable",
        )
        subparser.add_argument(
            "--authorized-workspace",
            help=(
                "workspace this run may act on; unset means the comparison is NOT made. In "
                "the served path this comes from the request binding's resolved principal, "
                "not from a flag"
            ),
        )
        subparser.add_argument(
            "--authorized-target-account",
            action="append",
            metavar="ACCOUNT_ID",
            help=(
                "account id this run may adopt or act in; repeatable. Unset means the "
                "comparison is NOT made"
            ),
        )
        subparser.add_argument(
            "--authorized-organizational-unit",
            action="append",
            metavar="OU_ID",
            help=(
                "organizational unit a created account may be placed into; repeatable. "
                "Applies to new-account-managed only. Unset means the comparison is NOT made"
            ),
        )
        return subparser

    add_common(
        subparsers.add_parser(
            "validate", help="accept or refuse a request; report unmade comparisons"
        )
    ).set_defaults(handler=_cmd_validate)
    render_cmd = add_common(
        subparsers.add_parser("render", help="the object set a request would apply")
    )
    render_cmd.add_argument(
        "--account-id",
        help=(
            "the account the governed creation path opened for this workspace, from its "
            "durable record. REQUIRED for new-account-managed and refused for the adopting "
            "modes: since #5531 the rendered AccountOwnership binds to an existing account "
            "instead of declaring an ACK Account that would create one, so rendering that "
            "mode without an id is refused rather than producing a manifest whose application "
            "would call CreateAccount outside the fence"
        ),
    )
    render_cmd.set_defaults(handler=_cmd_render)
    add_common(
        subparsers.add_parser(
            "cleanup-plan", help="what a teardown would delete, and what it retains"
        )
    ).set_defaults(handler=_cmd_cleanup_plan)
    creation_status = add_common(
        subparsers.add_parser(
            "creation-status",
            help="whether CreateAccount may be called for a request, and why",
        )
    )
    creation_status.add_argument(
        "--attempt-ledger",
        type=Path,
        help=(
            "file of previously recorded creation attempts. Omitted means none was recorded; "
            "a path that cannot be read is refused rather than read as empty"
        ),
    )
    creation_status.add_argument(
        "--aws-status",
        choices=tuple(
            status.value
            for status in creation.CreateAccountStatus
            if status is not creation.CreateAccountStatus.UNKNOWN
        ),
        help=(
            "what AWS reported about the recorded attempt. Use --aws-unreadable instead when "
            "AWS could not be consulted; there is no way to spell that as a status here"
        ),
    )
    creation_status.add_argument(
        "--aws-request-id", help="the CreateAccountStatus id the answer is about"
    )
    creation_status.add_argument(
        "--aws-failure",
        choices=tuple(failure.value for failure in creation.CreateAccountFailure),
        help="the reason AWS gave, required with --aws-status failed",
    )
    creation_status.add_argument(
        "--aws-account-id",
        help="the account id AWS created, required with --aws-status succeeded",
    )
    creation_status.add_argument(
        "--aws-detail", help="free text recorded alongside the answer"
    )
    creation_status.add_argument(
        "--aws-unreadable",
        metavar="WHY",
        help=(
            "AWS could not be consulted, with the reason. This is NOT a failure: it blocks "
            "both a retry and releasing the workspace"
        ),
    )
    creation_status.set_defaults(handler=_cmd_creation_status)

    add_common(
        subparsers.add_parser(
            "bootstrap-plan",
            help="what child-account bootstrap must establish, in order",
        )
    ).set_defaults(handler=_cmd_bootstrap_plan)

    recovery_report = add_common(
        subparsers.add_parser(
            "recovery-report",
            help="what exists, what is incomplete, what a retry repeats, what is retained",
        )
    )
    recovery_report.add_argument(
        "--attempt-ledger",
        type=Path,
        help=(
            "file of recorded creation attempts, so the report knows whether the account's "
            "existence is settled. Omitted means none was recorded"
        ),
    )
    recovery_report.add_argument(
        "--observed",
        action="append",
        metavar="STEP=STATE",
        help=(
            "what a read established about one bootstrap step; repeatable. Any step not named "
            "is reported as not-checked, which is neither present nor absent and blocks a "
            f"retry. States: {', '.join(state.value for state in recovery.StepState)}"
        ),
    )
    recovery_report.add_argument(
        "--observed-detail",
        action="append",
        metavar="STEP=TEXT",
        help="what was refused or seen, required for a step observed as denied",
    )
    recovery_report.set_defaults(handler=_cmd_recovery_report)

    add_common(
        subparsers.add_parser("dependencies", help="the verified pin set"),
        needs_config=False,
    ).set_defaults(handler=_cmd_dependencies)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a subcommand. Returns an exit code; raises nothing for expected refusals."""
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except ModeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return _EXIT_REFUSED
    except render_module.RenderError as exc:
        print(f"REFUSED (rendering): {exc}", file=sys.stderr)
        return _EXIT_REFUSED
    except cleanup.CleanupError as exc:
        print(f"REFUSED (cleanup): {exc}", file=sys.stderr)
        return _EXIT_REFUSED
    except dependencies.DependencyError as exc:
        print(f"REFUSED (dependencies): {exc}", file=sys.stderr)
        return _EXIT_REFUSED
    except creation.AccountCreationError as exc:
        print(f"REFUSED (account creation): {exc}", file=sys.stderr)
        return _EXIT_REFUSED
    except bootstrap.BootstrapError as exc:
        print(f"REFUSED (bootstrap): {exc}", file=sys.stderr)
        return _EXIT_REFUSED
    except recovery.RecoveryError as exc:
        print(f"REFUSED (recovery): {exc}", file=sys.stderr)
        return _EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
