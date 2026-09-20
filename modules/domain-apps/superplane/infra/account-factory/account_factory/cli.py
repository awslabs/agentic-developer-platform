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
* `dependencies` — the verified pin set, and what would happen if it could not be verified.

Exit codes: 0 accepted, 1 refused (invalid request, unverifiable dependency set, or output
that failed its own checks), 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import cleanup, dependencies
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
    supplied = (
        args.authorized_organization,
        args.authorized_management_account,
        args.authorized_management_cluster,
        modes,
        args.authorized_workspace,
        target_accounts,
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
    result = render_module.render(request, _authorization(args))
    _emit(
        {
            "mode": request.mode.value,
            "workspace": request.workspace_id,
            "namespace": result.namespace,
            "cluster_ownership": request.cluster_ownership.value,
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
        return subparser

    add_common(
        subparsers.add_parser(
            "validate", help="accept or refuse a request; report unmade comparisons"
        )
    ).set_defaults(handler=_cmd_validate)
    add_common(
        subparsers.add_parser("render", help="the object set a request would apply")
    ).set_defaults(handler=_cmd_render)
    add_common(
        subparsers.add_parser(
            "cleanup-plan", help="what a teardown would delete, and what it retains"
        )
    ).set_defaults(handler=_cmd_cleanup_plan)
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


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
