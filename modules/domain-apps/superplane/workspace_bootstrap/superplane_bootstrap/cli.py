"""The operator entry point: run the bootstrap gate sequence against a real cluster.

Issue #5533 (w6-10), EPIC #4910. Added by the F1 repair.

## What this closes

Review finding F1: "the implementation is not connected to any runtime path —
`bootstrap_workspace` has no caller outside its own tests". This module is that caller.
It builds the production adapters from `adapters.py`, reads the expectations from the
workspace Terraform module's published outputs, and runs the sequence under a bound
operation identity.

So there are now exactly two callers of `bootstrap_workspace`: this CLI and the tests.
That is the point — a package whose only caller is its own test suite proves that its
logic is self-consistent, not that anything uses it.

## Subcommands, and what each may do

| Subcommand | Reads | Mutates |
|------------|-------|---------|
| `plan`      | Terraform outputs, AWS         | nothing |
| `bootstrap` | the same, plus the cluster     | the workspace's own namespace, CRDs, taint, registration |
| `recover`   | durable state, the cluster     | restores the taint, releases a reservation |
| `state`     | durable state                  | nothing |

There is no `cleanup` subcommand, and no `--delete` anywhere. `retire.plan_cleanup`
produces a plan and this package has no delete code path at all (`access.py` has no
`delete` method), so executing a teardown is #5534's mode-aware retirement, holding its
own inventory. `state` reports what a teardown would need to know.

`plan` is the default, following `installation/__main__.py` ("plan is the default and
performs no network operations" — network reads only, here, since the expectations must
be compared against something). An operator who runs this with no subcommand cannot
mutate a cluster by accident.

## Why expectations come from a Terraform state file and not from flags

`target.py`'s rule: "the expected values come from Terraform outputs, not from the
request", because "a caller-supplied CA would make this check compare the request
against itself". This CLI honours that by taking `--outputs`, the JSON from
`terraform output -json` on `../infra/workspaces/`, and refusing to accept any of those
values as a flag. There is deliberately no `--cluster-arn` or
`--certificate-authority-data` option: adding one would reintroduce exactly the
comparison `verify_target` exists to prevent.

The one identity that does NOT come from the outputs file is `org_id`/`workspace_id`
used for authorization — those come from the server-held operation binding, loaded
by `--binding-resolver` using the operation ID in `--binding`. The outputs file publishes `org_id` and `workspace_id` too, and this CLI
compares them, but the AUTHORITY is the binding. An outputs file is a plan artifact; a
binding is a resolved principal.

## Why this file runs no `terraform` command

It reads a JSON file an operator produced. Invoking `terraform output` here would make
this CLI capable of reading (and, with the wrong subcommand, writing) remote state, and
the state backend holds more than this package's expectations. Handing it a file keeps
the blast radius at "a file the operator chose to show us".

## Exit codes

0 accepted, 1 refused, 2 usage error — matching
`../infra/account-factory/account_factory/cli.py`.

Merging this file authorizes no deployment. `bootstrap` mutates a cluster only when an
operator runs it with credentials, against a cluster that already exists, under a
binding that already resolved. That is a separately authorized operation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .adapters import (
    AwsObserver,
    AwsPrerequisiteAccess,
    IamNodeRoleFacts,
    KubectlClusterAccess,
    SubprocessRunner,
)
from .errors import BootstrapRefused, failure_kind
from .prerequisites import ExpectedPrerequisites
from .readiness import REQUIRED_SYSTEM_WORKLOADS
from .registry import SqlRegistrationStore
from .state import FileStateStore, load_state
from .target import verify_target
from .workspace import (
    WORKSPACE_CONTROLLER_NAME,
    BootstrapOutcome,
    bootstrap_workspace,
    recover_interrupted_bootstrap,
)

__all__ = ["main"]

_EXIT_OK = 0
_EXIT_REFUSED = 1
_EXIT_USAGE = 2

# Outputs this CLI requires from the workspace Terraform module. Every one is an
# expectation a gate compares an observation against; a missing one is a usage error
# rather than a skipped check, because a check that silently does not run is the
# failure mode this whole package is built to refuse.
_REQUIRED_OUTPUTS: tuple[str, ...] = (
    "account_id",
    "aws_region",
    "cluster_name",
    "cluster_arn",
    "cluster_certificate_authority_data",
    "cluster_security_group_id",
    # The role the nodes assume. Required because the CNI credential-scope proof asks
    # whether THIS role still carries CNI and account-wide ECR permissions; without it
    # that proof has no subject and cannot be verified, which holds the taint on.
    "node_role_arn",
    "vpc_id",
    "org_id",
    "workspace_id",
    "tenant_scheduling_prerequisites",
)


def _read_json(path: Path, what: str) -> Any:
    """Read and parse a JSON file, or raise a refusal naming the file.

    F9 applies here too, and this is the input side of it. The files read are terraform
    outputs and an operation binding — `terraform output -json` for a workspace includes
    every output the module declares, so a parse failure is raised over a buffer that may
    hold sensitive values. The refusal names the FILE and the failure's shape; the
    position comes from the exception's structured fields rather than its message, so a
    malformed file still gets an actionable line and column without any of its bytes.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BootstrapRefused(
            f"could not read the {what} file {path}: {failure_kind(exc)}"
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BootstrapRefused(
            f"{path} is not valid JSON (at line {exc.lineno}, column {exc.colno})"
        ) from exc


def _terraform_outputs(path: Path) -> dict[str, Any]:
    """Load `terraform output -json`, unwrapping each output's `value`.

    Accepts both the wrapped form (`{"cluster_arn": {"value": "...", "type": "string"}}`)
    and an already-unwrapped mapping, because operators produce both — `terraform output
    -json` gives the first, and a saved `jq 'map_values(.value)'` gives the second.
    Refusing one of them would just teach people to reshape the file by hand, which is
    another place for a value to be edited.
    """
    payload = _read_json(path, "Terraform outputs")
    if not isinstance(payload, dict):
        raise BootstrapRefused(
            f"{path} does not contain a JSON object of Terraform outputs"
        )
    unwrapped: dict[str, Any] = {}
    for name, entry in payload.items():
        if isinstance(entry, dict) and "value" in entry:
            unwrapped[str(name)] = entry["value"]
        else:
            unwrapped[str(name)] = entry

    missing = [name for name in _REQUIRED_OUTPUTS if name not in unwrapped]
    if missing:
        raise BootstrapRefused(
            f"{path} is missing required workspace outputs: "
            + ", ".join(sorted(missing))
            + ". These are the expectations the verification gates compare against; "
            "without them the gates would not be relaxed, they would be absent"
        )
    return unwrapped


def _text_output(outputs: dict[str, Any], name: str) -> str:
    """One output as non-blank text.

    A blank expectation is refused rather than compared. `verify_target` compares the
    observed CA against the expected one in constant time, and comparing against a
    blank string would refuse every cluster with a message about a mismatched
    certificate — technically true, uselessly misleading.
    """
    value = outputs.get(name)
    if not isinstance(value, str) or not value.strip():
        raise BootstrapRefused(
            f"the Terraform output {name!r} is empty or not a string; refusing to "
            "verify against a blank expectation"
        )
    return value


def _binding(path: Path, outputs: dict[str, Any], resolver: str | None = None):
    """Resolve an opaque operation ID through trusted facade composition.

    The file is a request handle, never an authority document. The resolver uses
    the authenticated facade and returns the server-held OperationBinding.
    """
    from importlib import import_module
    from .target import _binding_identity

    payload = _read_json(path, "operation request")
    if not isinstance(payload, dict) or set(payload) != {"operation_id"}:
        raise BootstrapRefused(
            "binding file must contain only an opaque operation_id; caller-supplied identity is not authority"
        )
    operation_id = payload["operation_id"]
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise BootstrapRefused("operation_id must be a nonempty opaque handle")
    if not resolver or ":" not in resolver:
        raise BootstrapRefused(
            "a trusted facade binding resolver (module:callable) is required"
        )
    module, name = resolver.rsplit(":", 1)
    try:
        binding = getattr(import_module(module), name)(operation_id)
    except Exception as exc:
        raise BootstrapRefused(
            f"operation binding resolution failed: {failure_kind(exc)}"
        ) from exc
    org_id, workspace_id = _binding_identity(binding)
    if binding.operation_id != operation_id:
        raise BootstrapRefused("facade returned a different operation binding")
    for name, bound in (("org_id", org_id), ("workspace_id", workspace_id)):
        if outputs.get(name) != bound:
            raise BootstrapRefused(
                f"the operation binding resolves {name} differently from the Terraform outputs"
            )
    return binding


def _expected_prerequisites(
    outputs: dict[str, Any], management_security_group_id: str
) -> ExpectedPrerequisites:
    """The F4 gate's expectations.

    `management_security_group_id` is a flag rather than an output because the
    workspace module does not publish it — the management surface lives outside that
    module, and `../infra/workspaces/outputs.tf` has no output naming it. It is
    therefore an operator-supplied value, and required: F4's finding was precisely that
    these prerequisites were optional and unverified, so an absent flag refuses instead
    of skipping the rule check.
    """
    return ExpectedPrerequisites(
        account_id=_text_output(outputs, "account_id"),
        vpc_id=_text_output(outputs, "vpc_id"),
        cluster_security_group_id=_text_output(
            outputs, "workspace_api_security_group_id"
        ),
        management_security_group_id=management_security_group_id,
        node_security_group_id=_text_output(
            outputs, "workspace_node_security_group_id"
        ),
        sts_endpoint_vpc_id=_text_output(outputs, "sts_endpoint_vpc_id"),
        sts_endpoint_security_group_id=_text_output(
            outputs, "sts_endpoint_security_group_id"
        ),
    )


def _taint_key(outputs: dict[str, Any]) -> str:
    """The bootstrap taint key, from the Terraform output that declares it.

    `tenant_scheduling_prerequisites.bootstrap_taint_key` is where the infrastructure
    module states which taint it applied. Reading it rather than assuming the constant
    means a workspace provisioned with a different key is bootstrapped correctly instead
    of having its interlock silently ignored — removing a taint that is not there
    succeeds trivially, and that is the dangerous success.
    """
    prerequisites = outputs.get("tenant_scheduling_prerequisites")
    if not isinstance(prerequisites, dict):
        raise BootstrapRefused(
            "the Terraform output `tenant_scheduling_prerequisites` is not an object; "
            "it declares the taint the interlock depends on"
        )
    key = str(prerequisites.get("bootstrap_taint_key", "") or "")
    if not key.strip():
        raise BootstrapRefused(
            "`tenant_scheduling_prerequisites.bootstrap_taint_key` is empty; refusing "
            "to remove an unnamed taint, because removing a taint that does not exist "
            "succeeds and would report an interlock that never held"
        )
    return key


def _declared_proofs(outputs: dict[str, Any]) -> tuple[str, ...]:
    """The proofs the infrastructure module requires before the taint may be cleared.

    Passed through to `prove_tenant_isolation` as the declared set, so the gate checks
    what `outputs.tf` says must be proved rather than what this package happens to
    implement. If Terraform grows a requirement this package cannot prove, the result is
    a refusal — not a silent pass on a stale list.
    """
    prerequisites = outputs.get("tenant_scheduling_prerequisites")
    if not isinstance(prerequisites, dict):
        return ()
    declared = prerequisites.get("required_proofs")
    if not isinstance(declared, (list, tuple)):
        return ()
    return tuple(str(entry) for entry in declared)


def _cluster_access(
    args: argparse.Namespace, outputs: dict[str, Any]
) -> KubectlClusterAccess:
    """The kubectl adapter, with the CRD manifests the operator declared.

    `--crd-manifest NAME=PATH`, repeatable. `establish_crds` refuses a CRD name with no
    declared manifest, so this is how an operator states what may be applied. A default
    manifest directory would let a file appear on disk and be applied without anyone
    naming it.

    The node-role reader is built from the `node_role_arn` Terraform output rather than a
    flag, because the workspace module publishes it and an operator retyping a role ARN
    is an operator who can point the CNI scope proof at the wrong role.
    """
    manifests: dict[str, Path] = {}
    for entry in args.crd_manifest or ():
        name, separator, path = entry.partition("=")
        if not separator or not name.strip() or not path.strip():
            raise BootstrapRefused(f"--crd-manifest expects NAME=PATH, got {entry!r}")
        manifests[name.strip()] = Path(path.strip())
    runner = SubprocessRunner()
    return KubectlClusterAccess(
        runner=runner,
        kubeconfig=args.kubeconfig,
        controller_namespace=args.controller_namespace,
        controller_service_account=args.controller_service_account,
        controller_image=args.controller_image,
        node_role=IamNodeRoleFacts(
            runner=runner, node_role_arn=_text_output(outputs, "node_role_arn")
        ),
        manifests=manifests,
        tenant_identity_reader=lambda: AwsPrerequisiteAccess(
            runner=runner, region=_text_output(outputs, "aws_region")
        ).tenant_principals(_text_output(outputs, "cluster_arn"), ""),
        imds_probe_image=args.imds_probe_image,
    )


def _report(outcome: BootstrapOutcome) -> dict[str, Any]:
    """The operator-facing summary. States what was NOT established, not only what was.

    Every field an operator needs to decide what to do next, and in particular the three
    that say whether the cluster was left safe: `taint_cleared`, `taint_restored` and
    `nodes_left_schedulable`. That last one is the F5 alarm — true means tenant
    workloads can schedule on a workspace that is not registered, which needs a human
    now.
    """
    readiness = outcome.readiness
    return {
        "status": "registered" if outcome.registered else "refused",
        "workspace_id": getattr(outcome.target, "workspace_id", ""),
        "cluster_arn": getattr(outcome.target, "cluster_arn", ""),
        "namespace": getattr(outcome.installation, "namespace", ""),
        "prerequisites_verified": outcome.inventory is not None,
        "reservation_held": outcome.reservation is not None,
        "readiness_usable": bool(getattr(readiness, "usable", False)),
        "readiness_failures": list(getattr(readiness, "failures", ()) or ()),
        "isolation_proved": bool(getattr(outcome.evidence, "may_clear_taint", False)),
        "isolation_unverified": list(getattr(outcome.evidence, "unverified", ()) or ()),
        "taint_cleared": outcome.taint_cleared,
        "taint_restored": outcome.taint_restored,
        "taint_restore_failed": outcome.restore_failed,
        "reservation_released": outcome.reservation_released,
        "registered": outcome.registered,
        "nodes_left_schedulable": outcome.nodes_left_schedulable,
        "refusal": str(outcome.refusal) if outcome.refusal is not None else "",
    }


def _run_bootstrap(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Build the production adapters and run the full gate sequence."""
    outputs = _terraform_outputs(args.outputs)
    binding = _binding(args.binding, outputs, args.binding_resolver)
    runtime = _authority_runtime(args, binding, outputs)
    region = _text_output(outputs, "aws_region")
    runner = SubprocessRunner()
    observer = (
        runtime.observer if runtime else AwsObserver(runner=runner, region=region)
    )

    access = runtime.access if runtime else _cluster_access(args, outputs)
    try:
        outcome = bootstrap_workspace(
            binding=binding,
            # Resolved here, immediately before the gate that uses it, never cached from
            # an earlier phase — `ProviderIdentity`'s own rule.
            provider=observer.provider_identity(),
            access=access,
            prerequisite_access=runtime.prerequisite_access
            if runtime
            else AwsPrerequisiteAccess(runner=runner, region=region),
            store=runtime.registration_store if runtime else _registration_store(args),
            authority_factory=runtime.authority if runtime else None,
            state_store=FileStateStore(args.state_dir),
            observed_cluster=observer.cluster_identity(
                _text_output(outputs, "cluster_name")
            ),
            expected_account_id=_text_output(outputs, "account_id"),
            expected_region=region,
            expected_cluster_name=_text_output(outputs, "cluster_name"),
            expected_cluster_arn=_text_output(outputs, "cluster_arn"),
            expected_certificate_authority_data=_text_output(
                outputs, "cluster_certificate_authority_data"
            ),
            expected_cni_role_arn=_cni_role_arn(outputs),
            expected_prerequisites=_expected_prerequisites(
                outputs, args.management_security_group_id
            ),
            cluster_ownership=args.cluster_ownership,
            namespace=args.namespace,
            enforce_version=args.enforce_version,
            credential_reference_id=args.credential_reference_id,
            contract_version=args.contract_version,
            screen=_screen(),
            controller_name=args.controller_name,
            required_system_workloads=tuple(
                args.required_system_workload or REQUIRED_SYSTEM_WORKLOADS
            ),
            declared_proofs=_declared_proofs(outputs) or None,
            taint_key=_taint_key(outputs),
        )
    finally:
        access.close()
        if runtime:
            runtime.authority.close()
    report = _report(outcome)
    return (_EXIT_OK if outcome.registered else _EXIT_REFUSED), report


def _authority_runtime(args, binding, outputs):
    """Load the service's trusted operation/vault/client composer."""
    from importlib import import_module
    from .authority_runtime import BootstrapRuntime

    resolver = getattr(args, "authority_resolver", None)
    if not resolver:
        return None  # The production orchestration refuses before any mutation.
    if ":" not in resolver:
        raise BootstrapRefused("authority resolver must name a trusted module:callable")
    module, name = resolver.rsplit(":", 1)
    try:
        runtime = getattr(import_module(module), name)(binding, outputs)
    except Exception as exc:
        raise BootstrapRefused(
            "bootstrap credential composition failed: " + failure_kind(exc)
        ) from exc
    if not isinstance(runtime, BootstrapRuntime):
        raise BootstrapRefused(
            "authority resolver did not return production bootstrap composition"
        )
    return runtime


def _cni_role_arn(outputs: dict[str, Any]) -> str:
    """The IRSA role aws-node must use, from the prerequisites output."""
    prerequisites = outputs.get("tenant_scheduling_prerequisites")
    arn = (
        str(prerequisites.get("cni_role_arn", "") or "")
        if isinstance(prerequisites, dict)
        else ""
    )
    if not arn.strip():
        raise BootstrapRefused(
            "`tenant_scheduling_prerequisites.cni_role_arn` is empty; the CNI "
            "credential-scope proof compares aws-node's role against it and cannot be "
            "run without it"
        )
    return arn


def _screen() -> Any:
    """The contract's real `assert_no_secret_material`.

    Imported here rather than at module scope so the package keeps its
    standard-library-only rule at import time: nothing that merely imports
    `superplane_bootstrap` pulls in `superplane_contracts`. The CLI is the edge where
    the real dependency is acceptable, and it must be the GENUINE function — an adapter
    would let this package's idea of secret material drift from the contract's.
    """
    try:
        from superplane_contracts.secrets import assert_no_secret_material
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise BootstrapRefused(
            "the shared contracts package is not importable, so the registration "
            "record cannot be screened for secret material; refusing to register "
            "an unscreened record"
        ) from exc
    return assert_no_secret_material


def _registration_store(args: argparse.Namespace) -> Any:
    """The production registration store.

    Deliberately constructed from an injected connection factory the operator names,
    rather than from a database URL flag. A `--database-url` option would put a
    credential on the command line, where it lands in shell history and in every
    process listing on the host.
    """
    from importlib import import_module

    module_name, separator, attribute = args.registration_store.rpartition(":")
    if not separator:
        raise BootstrapRefused(
            "--registration-store expects module:callable, naming a zero-argument "
            f"callable returning a TransactionalStore; got {args.registration_store!r}"
        )
    try:
        factory = getattr(import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        # F9: an ImportError raised deep in a driver's import chain reports that chain,
        # and a store factory module is exactly where a DSN tends to sit at import time.
        raise BootstrapRefused(
            f"could not load the registration store factory "
            f"{args.registration_store!r}: {failure_kind(exc)}"
        ) from exc
    return SqlRegistrationStore(store=factory())


def _run_plan(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Verify the target and report. Mutates nothing.

    Runs gate 1 only. That is a deliberate scope: gate 2 onward read the cluster, but
    the first thing that must be true is that the cluster being read is the one the
    reviewed plan described, and an operator wants that answer before anything else
    touches it.
    """
    outputs = _terraform_outputs(args.outputs)
    binding = _binding(args.binding, outputs, args.binding_resolver)
    region = _text_output(outputs, "aws_region")
    observer = AwsObserver(runner=SubprocessRunner(), region=region)
    target = verify_target(
        binding=binding,
        provider=observer.provider_identity(),
        observed=observer.cluster_identity(_text_output(outputs, "cluster_name")),
        expected_account_id=_text_output(outputs, "account_id"),
        expected_region=region,
        expected_cluster_name=_text_output(outputs, "cluster_name"),
        expected_cluster_arn=_text_output(outputs, "cluster_arn"),
        expected_certificate_authority_data=_text_output(
            outputs, "cluster_certificate_authority_data"
        ),
        cluster_ownership=args.cluster_ownership,
    )
    return _EXIT_OK, {
        "status": "verified",
        "workspace_id": target.workspace_id,
        "org_id": target.org_id,
        "cluster_arn": target.cluster_arn,
        "cluster_ownership": target.cluster_ownership,
        "is_adopted": target.is_adopted,
        "taint_key": _taint_key(outputs),
        "declared_proofs": list(_declared_proofs(outputs)),
        "note": (
            "Gate 1 only. Nothing was mutated. `bootstrap` runs the full sequence."
        ),
    }


def _run_recover(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Restore the interlock after a bootstrap died between taint removal and registration.

    The F5 recovery path, reachable by an operator. It reads durable state to find out
    whether the interruption actually happened — it does not take the operator's word
    for it, because restoring a taint on a healthy registered workspace would make its
    nodes unschedulable.
    """
    outputs = _terraform_outputs(args.outputs)
    # Identity from the binding, not from the outputs file. This subcommand restores a
    # NoSchedule taint and can release a registration reservation, so running it against
    # a workspace the caller is not bound to would make another tenant's nodes
    # unschedulable — a denial of service reachable by supplying someone else's outputs.
    binding = _binding(args.binding, outputs, args.binding_resolver)
    runtime = _authority_runtime(args, binding, outputs)
    observer = (
        runtime.observer
        if runtime
        else AwsObserver(
            runner=SubprocessRunner(), region=_text_output(outputs, "aws_region")
        )
    )
    target = verify_target(
        binding=binding,
        provider=observer.provider_identity(),
        observed=observer.cluster_identity(_text_output(outputs, "cluster_name")),
        expected_account_id=_text_output(outputs, "account_id"),
        expected_region=_text_output(outputs, "aws_region"),
        expected_cluster_name=_text_output(outputs, "cluster_name"),
        expected_cluster_arn=_text_output(outputs, "cluster_arn"),
        expected_certificate_authority_data=_text_output(
            outputs, "cluster_certificate_authority_data"
        ),
        cluster_ownership=args.cluster_ownership,
    )
    access = runtime.access if runtime else _cluster_access(args, outputs)
    try:
        access.bind_target(target, binding)
        outcome = recover_interrupted_bootstrap(
            access=access,
            store=runtime.registration_store if runtime else _registration_store(args),
            authority_factory=runtime.authority if runtime else None,
            binding=binding,
            target=target,
            state_store=FileStateStore(args.state_dir),
            workspace_id=binding.principal.workspace_id,
            cluster_arn=_text_output(outputs, "cluster_arn"),
            taint_key=_taint_key(outputs),
        )
    finally:
        access.close()
        if runtime:
            runtime.authority.close()
    report = _report(outcome)
    # A recovery that failed to restore the taint is the worst outcome in this package,
    # so it exits non-zero even though the recovery itself "ran".
    code = (
        _EXIT_REFUSED
        if outcome.nodes_left_schedulable or outcome.refusal is not None
        else _EXIT_OK
    )
    return code, report


def _run_state(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Report the durable record of this workspace's bootstrap. Reads one file.

    Deliberately NOT a cleanup plan. `retire.plan_cleanup` needs a full
    `ComponentInstallation` and `PrerequisiteInventory`, and durable state records
    ownership FACTS (the created namespace and its uid, which CRDs were established,
    whether prerequisites were recorded) rather than those objects. Reconstructing them
    from partial state would mean inventing the missing fields, and a cleanup plan built
    on invented fields is exactly the plan that deletes something adjacent.

    Mode-aware retirement belongs to #5534 (w6-11), which composes `plan_cleanup` with
    the inventory its provider already holds. This subcommand gives an operator the
    input to that decision — chiefly `interlock_restoration_pending`, which says whether
    nodes are schedulable RIGHT NOW for a bootstrap that never finished.
    """
    outputs = _terraform_outputs(args.outputs)
    # The binding is checked even though this subcommand mutates nothing. A state file
    # records which namespace ADP created and its uid — the facts a teardown decides
    # deletions from — so reading one is not a neutral act, and the workspace it is read
    # for must be the workspace the caller is bound to.
    binding = _binding(args.binding, outputs, args.binding_resolver)
    state = load_state(
        FileStateStore(args.state_dir),
        workspace_id=binding.principal.workspace_id,
        cluster_arn=_text_output(outputs, "cluster_arn"),
    )
    namespace = state.namespace
    return _EXIT_OK, {
        "status": "read",
        "workspace_id": state.workspace_id,
        "cluster_arn": state.cluster_arn,
        "created_namespace": namespace.name if namespace is not None else "",
        "created_namespace_uid": namespace.uid if namespace is not None else "",
        "crds_established": list(state.crds_established),
        "prerequisites_recorded": state.prerequisites_recorded,
        "registration_reserved": state.registration_reserved,
        "taint_cleared": state.taint_cleared,
        "registration_finalized": state.registration_finalized,
        "taint_restored": state.taint_restored,
        "interrupted_after_taint_cleared": state.interrupted_after_taint_cleared,
        # F12: the historical fact above and the decision below are different questions,
        # and reporting only the first is what made a recovered workspace look like an
        # unrecovered one forever. Both are published because an operator needs both: the
        # history explains why the workspace was touched, `recovery_pending` says whether
        # anything is outstanding right now. The two halves are broken out because they
        # have different urgency — a pending interlock restoration means tenant work can
        # schedule onto an unverified cluster, while a pending release means only that the
        # next attempt will be refused as a conflict.
        "recovery_pending": state.recovery_pending,
        "interlock_restoration_pending": state.interlock_restoration_pending,
        "reservation_release_pending": state.reservation_release_pending,
        "note": (
            "Durable state only; nothing was read from the cluster and nothing was "
            "mutated. If `recovery_pending` is true, run `recover`; when "
            "`interlock_restoration_pending` is also true, do it before anything "
            "schedules work on this workspace."
        ),
    }


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Options every subcommand needs: the outputs file, the binding, durable state."""
    parser.add_argument(
        "--outputs",
        required=True,
        type=Path,
        help="`terraform output -json` from infra/workspaces/. The expectations every "
        "gate compares against; deliberately not overridable by flags",
    )
    parser.add_argument(
        "--binding",
        required=True,
        type=Path,
        help="JSON containing only an opaque operation_id; resolved by the trusted facade",
    )
    parser.add_argument(
        "--binding-resolver",
        metavar="MODULE:CALLABLE",
        help="Trusted service resolver using the authenticated facade",
    )
    parser.add_argument(
        "--authority-resolver",
        metavar="MODULE:CALLABLE",
        help="Trusted operation/vault composer for production bootstrap and recovery clients",
    )
    parser.add_argument(
        "--state-dir",
        required=True,
        type=Path,
        help="Directory for durable bootstrap state. Must survive the process: the "
        "recovery and cleanup paths read it after a crash",
    )
    parser.add_argument(
        "--cluster-ownership",
        required=True,
        help="From #5530's ClusterOwnership. Recorded at verification rather than "
        "inferred, because a supplied cluster inside an ADP-managed account would be "
        "guessed wrong",
    )


def _add_cluster(parser: argparse.ArgumentParser) -> None:
    """Options for the subcommands that talk to the cluster."""
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--controller-namespace", default="superplane-system")
    parser.add_argument("--controller-service-account", default="superplane-controller")
    parser.add_argument(
        "--imds-probe-image",
        required=True,
        help="Release-pinned Python probe image with @sha256 digest",
    )
    parser.add_argument(
        "--controller-image",
        required=True,
        help="Image for the workspace controller Deployment. Required with no default: "
        "a default here would pin a controller version in argparse, where nobody "
        "looks for one, and every workspace bootstrapped afterwards would silently "
        "get whatever it said",
    )
    parser.add_argument(
        "--crd-manifest",
        action="append",
        metavar="NAME=PATH",
        help="Repeatable. A CRD with no declared manifest is refused rather than "
        "applied from a default location",
    )
    parser.add_argument(
        "--registration-store",
        required=True,
        metavar="MODULE:CALLABLE",
        help="Zero-argument callable returning a TransactionalStore. Not a database "
        "URL: a URL on the command line is a credential in shell history",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m superplane_bootstrap",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="subcommand")

    plan = subcommands.add_parser(
        "plan", help="Verify the target against the Terraform outputs. Mutates nothing."
    )
    _add_common(plan)
    plan.set_defaults(handler=_run_plan)

    run = subcommands.add_parser(
        "bootstrap",
        help="Run the full gate sequence. Mutates the workspace's own namespace, "
        "CRDs, taint and registration.",
    )
    _add_common(run)
    _add_cluster(run)
    run.add_argument("--namespace", required=True)
    run.add_argument(
        "--enforce-version",
        required=True,
        help="Pinned Pod Security admission enforce-version. Required, because `latest` "
        "means the policy can change under a workspace that was proved against another",
    )
    run.add_argument("--credential-reference-id", required=True)
    run.add_argument("--contract-version", required=True)
    run.add_argument("--controller-name", default=WORKSPACE_CONTROLLER_NAME)
    run.add_argument(
        "--management-security-group-id",
        required=True,
        help="The management surface's security group. Required: F4's finding was that "
        "these prerequisites were optional and unverified",
    )
    run.add_argument(
        "--required-system-workload",
        action="append",
        help=f"Repeatable. Defaults to {', '.join(REQUIRED_SYSTEM_WORKLOADS)}",
    )
    run.set_defaults(handler=_run_bootstrap)

    recover = subcommands.add_parser(
        "recover",
        help="Restore the interlock after a bootstrap died between clearing the taint "
        "and registering.",
    )
    _add_common(recover)
    _add_cluster(recover)
    recover.set_defaults(handler=_run_recover)

    state = subcommands.add_parser(
        "state",
        help="Report the durable bootstrap record, including whether a run was "
        "interrupted after the taint was cleared. Reads one file; mutates nothing.",
    )
    _add_common(state)
    state.set_defaults(handler=_run_state)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the subcommand, print one JSON object.

    Every outcome is JSON on stdout, including refusals, so an operator's wrapper does
    not have to parse prose to find out what happened. A refusal is exit 1 with the
    reason; an unexpected exception is exit 1 with a message that does NOT include the
    exception text, following `installation/__main__.py` — a cloud SDK or database error
    can carry a credential in its string, and this is the one place it would be printed.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "handler", None) is None:
        # No subcommand. `plan` is the default in spirit, but defaulting silently would
        # mean a bare invocation makes network calls, so this prints usage instead.
        parser.print_help()
        return _EXIT_USAGE
    try:
        code, report = args.handler(args)
    except BootstrapRefused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, indent=2))
        return _EXIT_REFUSED
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": (
                        "A bootstrap stage failed. Inspect the state directory and the "
                        "cluster; no readiness or registration is claimed. If a "
                        "bootstrap was interrupted after the taint was cleared, run "
                        "`recover` before anything schedules work here."
                    ),
                },
                indent=2,
            )
        )
        return _EXIT_REFUSED
    print(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":  # pragma: no cover - process entry
    sys.exit(main())
