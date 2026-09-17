"""Phase 12 verification checks: Superplane domain app.

Issue #5050 (U5), EPIC #4910.

Phase 12 is the verification counterpart of `deploy-all.sh`'s "Step 12/12: Superplane
domain app" — the last step of a deploy, because a domain app sits on top of the platform,
the gateway and the agent runtime. Everything checked here is created by U3
(`modules/domain-apps/superplane/infra/control-plane/`).

## Every check is read-only against the customer account

`ctx.customer_session` is read-only by contract (see `boto_helpers`), and these checks use
nothing but `Get*`/`Describe*`/`List*`. No check calls `secretsmanager:GetSecretValue`: the
only parameter VALUES read are the ones U3 documents as non-secret in `config.tf` (role
ARNs, namespaces, the CORS allowlist, and secret *names*), and they are read with
`WithDecryption=False` so a SecureString cannot be decrypted through this path even by
accident.

## Why absence is a SKIP unless the operator asked for Superplane

Superplane is an OPTIONAL domain app. `deploy-all.sh` gates step 12 behind
`SUPERPLANE_ENABLED` / `--superplane-only`, so on a gateway-only deploy none of these
resources exist and none of them should. If these checks hard-failed on absence, running
`phase: all` against a perfectly healthy gateway-only account would report a broken
deployment.

So absence is reported against the same signal the deploy uses: `SUPERPLANE_ENABLED`. Not
set means "not asked for" and a missing resource is a SKIP; set means the operator asked
for it and a missing resource is a real HARD failure. No new configuration input is
invented for this — reusing the deploy's own gate is what keeps the two from disagreeing.

Resources that DO exist are always checked regardless of the gate, so a deployed module
never goes unverified because an env var was forgotten.

## How this phase is invoked, and the step that is deliberately not added here

`python -m platform_deploy_mgmt.checks.runner --phase=12`, with `SUPERPLANE_ENABLED` set on
a deploy that included the domain app.

The module README's "How to Add a Phase" also asks for a step in
`.github/workflows/platform-deploy-mgmt-verify.yml`. That step is NOT added by this story:
U5's shared-edit allocation is the `PHASE_REGISTRY` entry and nothing else in
platform-deploy-mgmt, and that workflow's phase steps are the surface other units are
filling in (its stubs are labelled for sub-issues B–G). Adding a tenth branch to a file
several units are editing concurrently trades a merge conflict for a step that the runner
CLI already covers. Recorded here rather than left implicit, so whoever owns that workflow
next adds `--phase=12` alongside the phases they are enabling.

## Why the expected ECR repository set is discovered, not listed

`ecr.tf` derives the repository names from `releases/superplane.lock.yaml` precisely so
they cannot be retyped and drift. Restating them here would reintroduce the copy that
`ecr.tf` avoids, and parsing the lock would add a YAML dependency this module does not
have (`requirements.txt` is boto3 + botocore). The lock-to-repository correspondence is
already enforced where it belongs: `ecr.tf`'s plan-time preconditions fail the apply on a
duplicate or foreign repository name. What this phase adds is the part a plan cannot see —
that the repositories exist in the account and are configured the way the domain app's
digest pinning requires.
"""

from __future__ import annotations

import json
import os
import re
import time
from fnmatch import fnmatchcase
from typing import Any

from botocore.exceptions import ClientError

from .boto_helpers import Context
from .shape import CheckResult, CostClass, Result, Severity

# Values that mean "the operator asked for Superplane". Mirrors the truthiness
# deploy-all.sh applies to SUPERPLANE_ENABLED.
_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})

# SSM parameters the rollout lane (superplane-k8s-deploy.yml) cannot render without.
# `skypilot-namespace` is in this set on purpose: it is a separate input from `namespace`
# with a separate default, and a rollout that guessed one from the other renders service
# accounts that cannot assume their roles — which surfaces as opaque AWS 403s from inside
# the pod rather than as a rollout failure.
_REQUIRED_PARAMETERS = (
    "control-plane-role-arn",
    "skypilot-role-arn",
    "namespace",
    "skypilot-namespace",
    "aws-region",
    "cors-allowed-origins",
)

# Parameters that must hold a secret NAME. Their values are identifiers; the material they
# refer to never enters Terraform state, SSM, or this module.
_SECRET_NAME_PARAMETERS = ("database-secret-name", "jwt-secret-name")


def _name_prefix(ctx: Context) -> str:
    """The prefix every resource U3 creates carries: `adp-<env>-superplane`."""
    return f"adp-{ctx.environment}-superplane"


def _parameter_prefix(ctx: Context) -> str:
    """The SSM path U3's `config.tf` publishes configuration under."""
    return f"/adp/{ctx.environment}/superplane"


def _superplane_expected() -> bool:
    """Whether the operator asked for the Superplane domain app.

    Read from `SUPERPLANE_ENABLED`, the same variable `deploy-all.sh` gates step 12 on, so
    verification and deployment cannot disagree about whether the module should be there.
    """
    return os.environ.get("SUPERPLANE_ENABLED", "").strip().lower() in _ENABLED_VALUES


def _result(
    check_id: str,
    name: str,
    *,
    result: Result,
    severity: Severity,
    start: int,
    detail: str,
    evidence: dict[str, Any],
) -> CheckResult:
    """Build a CheckResult, timing it from `start` (a `perf_counter_ns` reading)."""
    return CheckResult(
        id=check_id,
        name=name,
        result=result,
        severity=severity,
        duration_ms=(time.perf_counter_ns() - start) // 1_000_000,
        detail=detail,
        evidence=evidence,
    )


def _absent(
    check_id: str,
    name: str,
    *,
    severity: Severity,
    start: int,
    detail: str,
    evidence: dict[str, Any],
) -> CheckResult:
    """A missing resource: FAIL if Superplane was asked for, SKIP if it was not.

    Centralised so every check reports absence the same way. A check that decided this for
    itself would eventually disagree with its neighbours, and the phase would report a
    gateway-only account as half-broken.
    """
    expected = _superplane_expected()
    if expected:
        suffix = " SUPERPLANE_ENABLED is set, so the resource should exist."
    else:
        suffix = (
            " Superplane is an optional domain app and SUPERPLANE_ENABLED is not set, "
            "so this is a skip rather than a failure."
        )
    return _result(
        check_id,
        name,
        result=Result.FAIL if expected else Result.SKIP,
        severity=severity,
        start=start,
        detail=detail + suffix,
        evidence={**evidence, "superplane_expected": expected},
    )


def _is_missing(error: ClientError) -> bool:
    """Whether a ClientError means "the resource is not there" rather than "call failed"."""
    code = error.response.get("Error", {}).get("Code", "")
    return code in {
        "NoSuchEntity",
        "ResourceNotFoundException",
        "RepositoryNotFoundException",
        "ParameterNotFound",
    }


def _error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "Unknown")


def check_12_1_control_plane_role_exists(ctx: Context) -> CheckResult:
    """Verify the Superplane API/controller IRSA role exists."""
    start = time.perf_counter_ns()
    check_id, name = "12.1", "Control-plane IRSA role exists"
    role_name = f"{_name_prefix(ctx)}-control-plane"
    iam = ctx.customer_session.client("iam")
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except ClientError as e:
        if _is_missing(e):
            return _absent(
                check_id,
                name,
                severity=Severity.HARD,
                start=start,
                detail=f"IAM role {role_name} not found.",
                evidence={"role": role_name},
            )
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"Could not read IAM role {role_name}: {_error_code(e)}",
            evidence={"role": role_name, "error_code": _error_code(e)},
        )
    return _result(
        check_id,
        name,
        result=Result.PASS,
        severity=Severity.HARD,
        start=start,
        detail=f"Role {role_name} exists.",
        evidence={"role": role_name, "role_id": role.get("RoleId", "")},
    )


def _service_account_subjects(trust_policy: dict[str, Any]) -> tuple[list[str], list[int]]:
    """Accept only explicit OIDC subjects; unsupported Allow paths fail verification."""
    subjects: list[str] = []
    unscoped: list[int] = []
    statements = trust_policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for index, statement in enumerate(statements):
        if statement.get("Effect") != "Allow":
            continue
        principal = statement.get("Principal", {})
        if not isinstance(principal, dict) or set(principal) != {"Federated"}:
            unscoped.append(index)
            continue
        provider = principal["Federated"]
        if not isinstance(provider, str) or ":oidc-provider/" not in provider or any(c in provider for c in "*?"):
            unscoped.append(index)
            continue
        issuer = provider.split(":oidc-provider/", 1)[1]
        conditions = statement.get("Condition") or {}
        equals = conditions.get("StringEquals", {})
        values = equals.get(issuer + ":sub", []) if isinstance(equals, dict) else []
        if isinstance(values, str):
            values = [values]
        if (
            not isinstance(values, list)
            or not values
            or any(
                not isinstance(value, str)
                or any(c in value for c in "*?")
                or len(value.split(":")) != 4
                or not value.startswith("system:serviceaccount:")
                or not all(value.split(":")[2:])
                for value in values
            )
        ):
            unscoped.append(index)
            continue
        subjects.extend(values)
    return subjects, unscoped


def check_12_2_control_plane_trust_is_scoped(ctx: Context) -> CheckResult:
    """Verify the control-plane role can only be assumed by named service accounts.

    U3's platform-isolation requirement is that a domain-app role is a *scoped* runtime
    identity. A trust policy scoped only to the cluster's OIDC provider satisfies "the role
    exists" while granting every pod in the cluster the ability to assume it, so existence
    alone is not the property worth checking.
    """
    start = time.perf_counter_ns()
    check_id, name = "12.2", "Control-plane trust policy scoped to named service accounts"
    role_name = f"{_name_prefix(ctx)}-control-plane"
    iam = ctx.customer_session.client("iam")
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except ClientError as e:
        if _is_missing(e):
            return _absent(
                check_id,
                name,
                severity=Severity.HARD,
                start=start,
                detail=f"IAM role {role_name} not found, so its trust policy cannot be checked.",
                evidence={"role": role_name},
            )
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"Could not read IAM role {role_name}: {_error_code(e)}",
            evidence={"role": role_name, "error_code": _error_code(e)},
        )

    trust_policy = role.get("AssumeRolePolicyDocument") or {}
    if isinstance(trust_policy, str):
        # Some botocore paths hand back the raw document rather than a decoded dict.
        trust_policy = json.loads(trust_policy)
    subjects, unscoped = _service_account_subjects(trust_policy)

    if unscoped:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=(
                f"{role_name} has trust statements without verified exact service-account scoping "
                f"(statement index {unscoped}); exclusive pod identity could not be established."
            ),
            evidence={"role": role_name, "unscoped_statements": unscoped, "subjects": subjects},
        )
    if not subjects:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"{role_name} has no service-account subject in its trust policy.",
            evidence={"role": role_name, "subjects": []},
        )
    non_service_account = [s for s in subjects if not s.startswith("system:serviceaccount:")]
    if non_service_account:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"{role_name} trusts subjects that are not service accounts: {non_service_account}",
            evidence={"role": role_name, "subjects": subjects},
        )
    return _result(
        check_id,
        name,
        result=Result.PASS,
        severity=Severity.HARD,
        start=start,
        detail=f"{role_name} is assumable only by {len(subjects)} named service account(s).",
        evidence={"role": role_name, "subjects": sorted(subjects)},
    )


def check_12_3_skypilot_role_exists(ctx: Context) -> CheckResult:
    """Verify the SkyPilot API server has its own IRSA role.

    Two roles rather than one is deliberate in U3: merging them would give the pod that
    terminates HTTP the ability to launch compute. This check is that the separation is
    actually present in the account, not just in the module.

    It does NOT check for a compute grant. U3 leaves `skypilot_compute_policy_arns` empty
    by design, because the account and the spend authorization are unresolved; asserting an
    attached compute policy here would fail a correct deployment.
    """
    start = time.perf_counter_ns()
    check_id, name = "12.3", "SkyPilot IRSA role exists and is distinct"
    prefix = _name_prefix(ctx)
    role_name = f"{prefix}-skypilot-api"
    iam = ctx.customer_session.client("iam")
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except ClientError as e:
        if _is_missing(e):
            return _absent(
                check_id,
                name,
                severity=Severity.HARD,
                start=start,
                detail=f"IAM role {role_name} not found.",
                evidence={"role": role_name},
            )
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"Could not read IAM role {role_name}: {_error_code(e)}",
            evidence={"role": role_name, "error_code": _error_code(e)},
        )
    return _result(
        check_id,
        name,
        result=Result.PASS,
        severity=Severity.HARD,
        start=start,
        detail=f"Role {role_name} exists and is separate from {prefix}-control-plane.",
        evidence={"role": role_name, "role_id": role.get("RoleId", "")},
    )


def _wildcard_secret_statements(policy: dict[str, Any]) -> list[str]:
    """Find broad or unsupported secret grants, without evaluating IAM deny precedence."""
    offenders: list[str] = []
    statements = policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for index, statement in enumerate(statements):
        if statement.get("Effect") != "Allow":
            continue
        sid = str(statement.get("Sid", f"statement[{index}]"))
        # Complement grants cannot establish this check's positive scope guarantee.
        if "NotAction" in statement or "NotResource" in statement:
            offenders.append(sid)
            continue
        actions = statement.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if not any(fnmatchcase("secretsmanager", str(a).lower().split(":", 1)[0]) for a in actions):
            continue
        resources = statement.get("Resource", [])
        if isinstance(resources, str):
            resources = [resources]
        scoped = bool(resources)
        for resource in resources:
            parts = str(resource).split(":", 5)
            if (
                len(parts) != 6
                or parts[0] != "arn"
                or parts[2] != "secretsmanager"
                or any(not part or any(c in part for c in "*?") for part in parts[1:5])
                or not parts[5].startswith("secret:")
                or not parts[5][7:]
                or parts[5][7] in "*?"
            ):
                scoped = False
        if not scoped:
            offenders.append(sid)
    return offenders


def _iam_items(iam: Any, method: str, key: str, **kwargs: Any) -> list[Any]:
    """Read every IAM list page; incomplete or repeated cursors fail closed."""
    items: list[Any] = []
    seen: set[str] = set()
    while True:
        page = getattr(iam, method)(**kwargs)
        values = page[key]
        if not isinstance(values, list):
            raise ValueError("invalid IAM list response")
        items.extend(values)
        if not page.get("IsTruncated", False):
            return items
        marker = page.get("Marker")
        if not isinstance(marker, str) or not marker or marker in seen:
            raise ValueError("incomplete IAM pagination")
        seen.add(marker)
        kwargs["Marker"] = marker


def check_12_4_secret_access_is_resource_scoped(ctx: Context) -> CheckResult:
    """Inspect every inline and attached default policy on both Superplane roles.

    This is a conservative grant audit, not a complete IAM effective-permissions
    evaluator. Broad grants and unsupported complement forms require investigation
    even if a separate deny, boundary or organization policy might restrict them.
    """
    start = time.perf_counter_ns()
    check_id, name = "12.4", "Secret access is resource-scoped on both roles"
    prefix = _name_prefix(ctx)
    role_names = [f"{prefix}-control-plane", f"{prefix}-skypilot-api"]
    iam = ctx.customer_session.client("iam")
    offenders: dict[str, list[str]] = {}
    inspected: dict[str, list[str]] = {}
    missing: list[str] = []
    for role_name in role_names:
        try:
            policy_names = _iam_items(iam, "list_role_policies", "PolicyNames", RoleName=role_name)
        except ClientError as error:
            if _is_missing(error):
                missing.append(role_name)
                continue
            return _result(
                check_id,
                name,
                result=Result.FAIL,
                severity=Severity.HARD,
                start=start,
                detail=f"Could not list policies on {role_name}: {_error_code(error)}",
                evidence={"role": role_name, "error_code": _error_code(error)},
            )
        except (ValueError, KeyError, TypeError):
            return _result(
                check_id,
                name,
                result=Result.FAIL,
                severity=Severity.HARD,
                start=start,
                detail="Incomplete IAM policy inventory.",
                evidence={"role": role_name},
            )
        inspected[role_name] = []
        try:
            documents = []
            for policy_name in policy_names:
                documents.append(
                    (policy_name, iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)["PolicyDocument"])
                )
            attached = _iam_items(iam, "list_attached_role_policies", "AttachedPolicies", RoleName=role_name)
            for policy in attached:
                arn = policy["PolicyArn"]
                version = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
                document = iam.get_policy_version(PolicyArn=arn, VersionId=version)["PolicyVersion"]["Document"]
                documents.append((arn, document))
            for policy_name, document in documents:
                if isinstance(document, str):
                    document = json.loads(document)
                found = _wildcard_secret_statements(document)
                inspected[role_name].append(policy_name)
                if found:
                    offenders[f"{role_name}/{policy_name}"] = found
        except ClientError as error:
            return _result(
                check_id,
                name,
                result=Result.FAIL,
                severity=Severity.HARD,
                start=start,
                detail=f"Could not inspect policies on {role_name}: {_error_code(error)}",
                evidence={"role": role_name, "error_code": _error_code(error)},
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return _result(
                check_id,
                name,
                result=Result.FAIL,
                severity=Severity.HARD,
                start=start,
                detail="Incomplete or malformed IAM policy evidence.",
                evidence={"role": role_name},
            )
    if missing and not inspected:
        return _absent(
            check_id,
            name,
            severity=Severity.HARD,
            start=start,
            detail=f"Neither Superplane role exists: {missing}.",
            evidence={"roles": role_names},
        )
    if offenders or missing:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail="Secret grant scope could not be verified on both Superplane roles.",
            evidence={"offending_statements": offenders, "inspected": inspected, "missing_roles": missing},
        )
    return _result(
        check_id,
        name,
        result=Result.PASS,
        severity=Severity.HARD,
        start=start,
        detail="All inline and attached default policies inspected; no broad secret grant found.",
        evidence={"inspected": inspected, "missing_roles": []},
    )


def _superplane_repositories(ctx: Context) -> list[dict[str, Any]]:
    """Every ECR repository the Superplane domain app owns, found by its `adp-superplane-` prefix.

    Discovered rather than listed: see the module docstring for why the expected set is not
    read back out of `releases/superplane.lock.yaml`.
    """
    ecr = ctx.customer_session.client("ecr")
    repositories: list[dict[str, Any]] = []
    paginator = ecr.get_paginator("describe_repositories")
    for page in paginator.paginate():
        for repository in page.get("repositories", []):
            if str(repository.get("repositoryName", "")).startswith("adp-superplane-"):
                repositories.append(repository)
    return repositories


def check_12_5_ecr_repositories_exist(ctx: Context) -> CheckResult:
    """Verify the domain app's ECR repositories exist in the account."""
    start = time.perf_counter_ns()
    check_id, name = "12.5", "Superplane ECR repositories exist"
    try:
        repositories = _superplane_repositories(ctx)
    except ClientError as e:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"Could not describe ECR repositories: {_error_code(e)}",
            evidence={"error_code": _error_code(e)},
        )
    names = sorted(str(r.get("repositoryName", "")) for r in repositories)
    if not names:
        return _absent(
            check_id,
            name,
            severity=Severity.HARD,
            start=start,
            detail="No ECR repository with the adp-superplane- prefix exists.",
            evidence={"repositories": []},
        )
    return _result(
        check_id,
        name,
        result=Result.PASS,
        severity=Severity.HARD,
        start=start,
        detail=f"{len(names)} Superplane repositor{'y' if len(names) == 1 else 'ies'} present.",
        evidence={"repositories": names},
    )


def check_12_6_ecr_tags_are_immutable(ctx: Context) -> CheckResult:
    """Verify the repositories enforce immutable tags and scan on push.

    A deploy resolves Superplane images by digest. Mutable tags let a tag be repointed at a
    different image after the digest was recorded, which defeats the pinning without
    breaking anything visible.
    """
    start = time.perf_counter_ns()
    check_id, name = "12.6", "Superplane ECR repositories enforce immutable tags"
    try:
        repositories = _superplane_repositories(ctx)
    except ClientError as e:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"Could not describe ECR repositories: {_error_code(e)}",
            evidence={"error_code": _error_code(e)},
        )
    if not repositories:
        return _absent(
            check_id,
            name,
            severity=Severity.HARD,
            start=start,
            detail="No Superplane ECR repository exists, so tag mutability cannot be checked.",
            evidence={"repositories": []},
        )

    mutable = sorted(
        str(r.get("repositoryName", "")) for r in repositories if r.get("imageTagMutability") != "IMMUTABLE"
    )
    unscanned = sorted(
        str(r.get("repositoryName", ""))
        for r in repositories
        if not (r.get("imageScanningConfiguration") or {}).get("scanOnPush", False)
    )
    if mutable:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"Repositories allow mutable tags, which defeats digest pinning: {mutable}",
            evidence={"mutable": mutable, "scan_on_push_missing": unscanned},
        )
    if unscanned:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.SOFT,
            start=start,
            detail=f"Repositories are immutable but do not scan on push: {unscanned}",
            evidence={"mutable": [], "scan_on_push_missing": unscanned},
        )
    return _result(
        check_id,
        name,
        result=Result.PASS,
        severity=Severity.HARD,
        start=start,
        detail="All Superplane repositories are IMMUTABLE and scan on push.",
        evidence={"repositories": sorted(str(r.get("repositoryName", "")) for r in repositories)},
    )


def _superplane_parameters(ctx: Context) -> dict[str, str]:
    """Read the domain app's SSM configuration, keyed by leaf name.

    `WithDecryption=False` on purpose: U3 writes only non-secret configuration here, and a
    read-only verification path should not be able to decrypt anything even if that ever
    changes.
    """
    ssm = ctx.customer_session.client("ssm")
    prefix = _parameter_prefix(ctx)
    values: dict[str, str] = {}
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=False):
        for parameter in page.get("Parameters", []):
            full_name = str(parameter.get("Name", ""))
            values[full_name.rsplit("/", 1)[-1]] = str(parameter.get("Value", ""))
    return values


def check_12_7_ssm_parameters_published(ctx: Context) -> CheckResult:
    """Verify the configuration the rollout lane reads is published.

    These parameters are how the rollout and the pods learn what Terraform decided. A
    missing one does not fail the apply; it fails the rollout later, or worse, renders a
    manifest with a guessed value.
    """
    start = time.perf_counter_ns()
    check_id, name = "12.7", "Superplane SSM configuration published"
    prefix = _parameter_prefix(ctx)
    try:
        parameters = _superplane_parameters(ctx)
    except ClientError as e:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"Could not read parameters under {prefix}: {_error_code(e)}",
            evidence={"prefix": prefix, "error_code": _error_code(e)},
        )
    if not parameters:
        return _absent(
            check_id,
            name,
            severity=Severity.HARD,
            start=start,
            detail=f"No parameters exist under {prefix}.",
            evidence={"prefix": prefix, "found": []},
        )
    missing = [p for p in _REQUIRED_PARAMETERS if not parameters.get(p, "").strip()]
    if missing:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"Configuration the rollout requires is missing under {prefix}: {missing}",
            evidence={"prefix": prefix, "missing": missing, "found": sorted(parameters)},
        )
    return _result(
        check_id,
        name,
        result=Result.PASS,
        severity=Severity.HARD,
        start=start,
        detail=f"All {len(_REQUIRED_PARAMETERS)} required parameters are published under {prefix}.",
        evidence={"prefix": prefix, "found": sorted(parameters)},
    )


def check_12_8_cors_allowlist_is_not_wildcard(ctx: Context) -> CheckResult:
    """Verify the deployed CORS allowlist is explicit rather than '*'.

    Upstream Superplane defaults to `["*"]`, and U3 overrides it because '*' with
    credentials is what turns any origin into a session-bearing caller. `variables.tf`
    validates it at plan time; this is the check that the value which actually reached the
    account is the validated one.
    """
    start = time.perf_counter_ns()
    check_id, name = "12.8", "CORS allowlist is explicit, not wildcard"
    prefix = _parameter_prefix(ctx)
    try:
        parameters = _superplane_parameters(ctx)
    except ClientError as e:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail=f"Could not read parameters under {prefix}: {_error_code(e)}",
            evidence={"prefix": prefix, "error_code": _error_code(e)},
        )
    raw = parameters.get("cors-allowed-origins")
    if raw is None:
        return _absent(
            check_id,
            name,
            severity=Severity.HARD,
            start=start,
            detail=f"{prefix}/cors-allowed-origins does not exist.",
            evidence={"prefix": prefix},
        )
    try:
        origins = json.loads(raw)
    except json.JSONDecodeError:
        origins = None
    if not isinstance(origins, list) or any(
        not isinstance(origin, str) or not re.fullmatch(r"https?://[A-Za-z0-9.:-]+", origin) for origin in origins
    ):
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail="CORS configuration must be a JSON list of explicit scheme-qualified origins.",
            evidence={"prefix": prefix, "invalid_origin_configuration": True},
        )

    if not origins:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.SOFT,
            start=start,
            detail="The CORS allowlist is empty; the API will reject every browser origin.",
            evidence={"origins": []},
        )
    if "*" in origins:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.HARD,
            start=start,
            detail="The deployed CORS allowlist contains '*', which with credentials allows any origin.",
            evidence={"origins": origins},
        )
    return _result(
        check_id,
        name,
        result=Result.PASS,
        severity=Severity.HARD,
        start=start,
        detail=f"The allowlist names {len(origins)} explicit origin(s).",
        evidence={"origins": origins},
    )


def _looks_like_secret_material(value: str) -> bool:
    """Whether a value looks like credential material rather than a secret's name.

    A shape check, not a proof: it cannot establish that no material is present, only catch
    the common mistake of pasting a connection string or a JSON credential blob into the
    parameter that is supposed to hold a name. It is SOFT for that reason.
    """
    candidate = value.strip()
    if not candidate:
        return False
    if candidate.startswith("{") or candidate.startswith("["):
        return True
    if "://" in candidate:
        return True
    lowered = candidate.lower()
    return any(marker in lowered for marker in ("password=", "secret=", "-----begin"))


def check_12_9_secret_parameters_hold_names(ctx: Context) -> CheckResult:
    """Verify the secret parameters hold NAMES, not material.

    SSM (and Terraform state behind it) is not a secret store: `aws_ssm_parameter` writes
    its value to state in plaintext whatever type the parameter is. U3 publishes secret
    names so a pod can resolve the material through its own scoped role at runtime; a value
    pasted here would be readable by anyone who can read a plan.
    """
    start = time.perf_counter_ns()
    check_id, name = "12.9", "Secret parameters hold names, not material"
    prefix = _parameter_prefix(ctx)
    try:
        parameters = _superplane_parameters(ctx)
    except ClientError as e:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.SOFT,
            start=start,
            detail=f"Could not read parameters under {prefix}: {_error_code(e)}",
            evidence={"prefix": prefix, "error_code": _error_code(e)},
        )
    present = [p for p in _SECRET_NAME_PARAMETERS if p in parameters]
    if not present:
        return _absent(
            check_id,
            name,
            severity=Severity.SOFT,
            start=start,
            detail=f"Neither secret-name parameter exists under {prefix}.",
            evidence={"prefix": prefix, "checked": list(_SECRET_NAME_PARAMETERS)},
        )
    # Only the parameter NAMES are recorded in evidence. The suspect value is never copied
    # into the evidence document, which is uploaded to S3 — reporting a leak by
    # republishing it would make the finding worse than the defect.
    offenders = sorted(
        p
        for p in _SECRET_NAME_PARAMETERS
        if not parameters.get(p, "").strip() or _looks_like_secret_material(parameters[p])
    )
    if offenders:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.SOFT,
            start=start,
            detail=(
                f"Parameters under {prefix} look like credential material rather than secret names: "
                f"{offenders}. Value not reproduced here."
            ),
            evidence={"prefix": prefix, "suspect_parameters": offenders},
        )
    return _result(
        check_id,
        name,
        result=Result.PASS,
        severity=Severity.SOFT,
        start=start,
        detail=f"{len(present)} secret parameter(s) hold name-shaped values.",
        evidence={"prefix": prefix, "checked": present},
    )


def check_12_10_repositories_are_tagged_for_teardown(ctx: Context) -> CheckResult:
    """Verify the domain app's repositories carry the DomainApp tag teardown looks for.

    The failure this guards is documented in the module itself: `modules/domain-apps/cyber/`
    is absent from `deploy-all.sh` entirely, which is why its resources survived teardown.
    A domain-owned resource that cannot be identified as domain-owned is the same class of
    orphan, and it is only visible in the account — a plan cannot show it.
    """
    start = time.perf_counter_ns()
    check_id, name = "12.10", "Superplane repositories tagged for teardown"
    ecr = ctx.customer_session.client("ecr")
    try:
        repositories = _superplane_repositories(ctx)
    except ClientError as e:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.SOFT,
            start=start,
            detail=f"Could not describe ECR repositories: {_error_code(e)}",
            evidence={"error_code": _error_code(e)},
        )
    if not repositories:
        return _absent(
            check_id,
            name,
            severity=Severity.SOFT,
            start=start,
            detail="No Superplane ECR repository exists, so tagging cannot be checked.",
            evidence={"repositories": []},
        )

    untagged: list[str] = []
    for repository in repositories:
        repository_name = str(repository.get("repositoryName", ""))
        try:
            tags = ecr.list_tags_for_resource(resourceArn=repository.get("repositoryArn", ""))
        except ClientError as e:
            return _result(
                check_id,
                name,
                result=Result.FAIL,
                severity=Severity.SOFT,
                start=start,
                detail=f"Could not list tags on {repository_name}: {_error_code(e)}",
                evidence={"repository": repository_name, "error_code": _error_code(e)},
            )
        pairs = {str(t.get("Key")): str(t.get("Value")) for t in tags.get("tags", [])}
        if pairs.get("DomainApp") != "superplane":
            untagged.append(repository_name)

    if untagged:
        return _result(
            check_id,
            name,
            result=Result.FAIL,
            severity=Severity.SOFT,
            start=start,
            detail=f"Repositories lack DomainApp=superplane, so teardown cannot identify them: {untagged}",
            evidence={"untagged": untagged},
        )
    return _result(
        check_id,
        name,
        result=Result.PASS,
        severity=Severity.SOFT,
        start=start,
        detail=f"All {len(repositories)} repositories carry DomainApp=superplane.",
        evidence={"repositories": sorted(str(r.get("repositoryName", "")) for r in repositories)},
    )


# Registry of all Phase 12 checks.
# Format: (id, name, fn, severity, cost_class)
CHECKS = [
    (
        "12.1",
        "Control-plane IRSA role exists",
        check_12_1_control_plane_role_exists,
        Severity.HARD,
        CostClass.CHEAP,
    ),
    (
        "12.2",
        "Control-plane trust policy scoped to named service accounts",
        check_12_2_control_plane_trust_is_scoped,
        Severity.HARD,
        CostClass.CHEAP,
    ),
    (
        "12.3",
        "SkyPilot IRSA role exists and is distinct",
        check_12_3_skypilot_role_exists,
        Severity.HARD,
        CostClass.CHEAP,
    ),
    (
        "12.4",
        "Secret access is resource-scoped on both roles",
        check_12_4_secret_access_is_resource_scoped,
        Severity.HARD,
        CostClass.CHEAP,
    ),
    (
        "12.5",
        "Superplane ECR repositories exist",
        check_12_5_ecr_repositories_exist,
        Severity.HARD,
        CostClass.CHEAP,
    ),
    (
        "12.6",
        "Superplane ECR repositories enforce immutable tags",
        check_12_6_ecr_tags_are_immutable,
        Severity.HARD,
        CostClass.CHEAP,
    ),
    (
        "12.7",
        "Superplane SSM configuration published",
        check_12_7_ssm_parameters_published,
        Severity.HARD,
        CostClass.CHEAP,
    ),
    (
        "12.8",
        "CORS allowlist is explicit, not wildcard",
        check_12_8_cors_allowlist_is_not_wildcard,
        Severity.HARD,
        CostClass.CHEAP,
    ),
    (
        "12.9",
        "Secret parameters hold names, not material",
        check_12_9_secret_parameters_hold_names,
        Severity.SOFT,
        CostClass.CHEAP,
    ),
    (
        "12.10",
        "Superplane repositories tagged for teardown",
        check_12_10_repositories_are_tagged_for_teardown,
        Severity.SOFT,
        CostClass.CHEAP,
    ),
]
