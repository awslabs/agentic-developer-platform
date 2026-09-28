"""What this domain owns, and how ownership is decided — Issue #5042 (U3), EPIC #4910.

## Why this module exists

PR #5283's review (finding 3) reproduced a P1: the apply and destroy guards decided
ownership by matching the *prefix* of a resource type or address. That accepted
`module.core.aws_vpc.main will be destroyed` and `aws_iam_role.gateway will be destroyed`,
because both begin with a string the guard tolerated. As the review put it: "resource
type/address prefixes are not ownership."

Prefix matching fails in three separate directions, and the fixes are different:

1.  A *type* can be allowed while the *instance* belongs to somebody else.
    `aws_iam_role` is a type this module legitimately creates — but
    `aws_iam_role.gateway` is the gateway's role. Ownership has to be decided on the
    resource's NAME/ARN, not only its type.
2.  A *nested module address* hides the type. A guard anchored at the start of the
    address never sees `aws_vpc` inside `module.core.aws_vpc.main`, so the check it
    thought it was doing did not happen at all.
3.  A *near-miss type* passes an unanchored alternation.
    `aws_iam_role_policies_exclusive` — a real resource type whose purpose is to DELETE
    inline role policies absent from configuration — matches a pattern written as
    `aws_iam_role_policy|...`.

So this module decides ownership structurally: it walks to the leaf type through any
module nesting, requires the type to be in an explicit allowlist, and validates the
resource's identifying values field-by-field against the naming family that type belongs to,
in the selected account and environment. Anything it cannot positively attribute is DENIED —
the review's "deny unknown ownership".

## The 84e3f7ee correction: type-aware and ANCHORED, not one substring prefix

A later checkpoint review reproduced a fourth failure direction, in this module's own first
version. It asserted one flat prefix (`adp-superplane-`) for every resource and an SSM prefix
`/adp/superplane/`, but the module creates `adp-<env>-superplane-*` and
`/adp/<env>/superplane/*`. Two consequences, pulling opposite ways:

4.  It DENIED the module's own `adp-dev-superplane-control-plane` and
    `/adp/dev/superplane/namespace`, so the first legitimate apply could never run — and a
    guard that makes its lane unusable gets removed rather than fixed.
5.  Its test was `DOMAIN_PREFIX in value`, unanchored, so `gateway-adp-superplane-api`
    passed by merely CONTAINING the prefix.

Hence the naming families below, anchored patterns, per-type identity fields, and ECR
ownership decided against U2's lock inventory rather than name shape. See the block comment
above `DOMAIN_PREFIX`.

## Why an allowlist, in both dimensions

Types are allowlisted because a denylist passes whatever nobody thought to forbid, and
these guards protect one-way operations. Names are matched against this domain's own anchored
naming patterns for the same reason: enumerating every platform-owned name would need this
file to track the whole platform, and a name it had not heard of would default to allowed.

## What this module deliberately does NOT decide

It does not decide whether an apply is *authorized* — that is the typed confirmation and
the destructive-approval label. It only answers "is this resource this domain's to touch".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# The domain's naming contract, DERIVED FROM THE MODULE SOURCE.
#
# A checkpoint review of `84e3f7ee` reproduced the defect this section replaces. The
# previous version asserted a single flat prefix `adp-superplane-` for everything and an SSM
# prefix `/adp/superplane/`. Neither is what the module actually creates:
#
#     main.tf:69    name_prefix      = "adp-${var.environment}-superplane"
#     config.tf:23  parameter_prefix = "/adp/${var.environment}/superplane"
#
# So the guard DENIED the real `aws_iam_role` named `adp-dev-superplane-control-plane` and
# the real `aws_ssm_parameter` named `/adp/dev/superplane/namespace` — including on the very
# first legitimate apply — while `_value_is_domain_owned` used an unanchored substring test
# (`DOMAIN_PREFIX in value`) that ACCEPTED the foreign name `gateway-adp-superplane-api`.
# A guard that blocks the resources it exists to protect and admits ones it exists to
# exclude is worse than no guard, because its passing is read as evidence.
#
# The correction is deliberately NOT "rename the deployed resources to fit the guard" and
# NOT "broaden the prefix until the tests go green". The names below are read off the module
# source above; the patterns are anchored; and validation is TYPE-AWARE, because which field
# identifies a resource — and which naming family that field belongs to — differs per type.
#
# Three naming families exist, and the difference between them is a design fact rather than
# an inconsistency:
#
#   FLAT  `adp-<env>-superplane-*`   IAM roles and inline policies. Environment-scoped, so
#                                    the environment is bound POSITIVELY here.
#   SSM   `/adp/<env>/superplane/*`  Configuration parameters. Path-shaped, environment-scoped.
#   ECR   `adp-superplane-*`         Repository names, deliberately environment-INDEPENDENT:
#                                    they come from U2's lock and hold images, which are the
#                                    same artifact in every environment. Embedding an
#                                    environment would mean rebuilding one digest per
#                                    environment, defeating the point of a digest pin.
#
# Binding the environment by *excluding* a list of other environment words (the previous
# approach) is not evidence that the selected environment owns a name: it passes any name
# whose environment token nobody thought to enumerate. The FLAT and SSM patterns therefore
# require the selected environment to be PRESENT, and ECR's exemption is stated explicitly
# below rather than emerging from a gap in a denylist.
# ---------------------------------------------------------------------------

# Retained as the ECR family's prefix — repository names carry no environment segment.
DOMAIN_PREFIX = "adp-superplane-"

# Environment tokens this repo uses. Now used ONLY to reject an ECR repository name that
# looks environment-scoped (see `_check_ecr_name`); ownership of FLAT/SSM identifiers is
# decided by positive match against the selected environment, not by this list.
KNOWN_ENVIRONMENTS = ("dev", "staging", "prod", "embark1")

# An environment token must look like one before it is interpolated into a pattern, so a
# hostile `--environment` value cannot inject regex metacharacters or widen the match.
VALID_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9]{0,15}$")

# Naming families, referenced by the per-type field rules below.
FLAT = "flat"
SSM = "ssm"
ECR = "ecr"
EXTERNAL_POLICY = "external_policy"

# Which fields identify each type, and which family each belongs to.
#
# `aws_iam_role_policy_attachment.policy_arn` is EXTERNAL_POLICY, and that is the second
# false denial this rewrite fixes. `irsa.tf:186` attaches
# `var.skypilot_compute_policy_arns` — AWS-managed policy ARNs such as
# `arn:aws:iam::aws:policy/AmazonEC2FullAccess`. Those carry neither the domain prefix nor
# the selected account (their ARN account field is the literal `aws`), so the previous
# generic check would have denied a legitimate SkyPilot compute grant the moment one was
# authorized. The attachment's `role` must still be domain-owned — that is what stops a
# policy being attached to somebody else's role.
IDENTITY_RULES: dict[str, dict[str, str]] = {
    "aws_iam_role": {"name": FLAT, "arn": FLAT},
    "aws_iam_role_policy": {"name": FLAT, "role": FLAT},
    "aws_iam_policy": {"name": FLAT, "arn": FLAT},
    "aws_iam_role_policy_attachment": {"role": FLAT, "policy_arn": EXTERNAL_POLICY},
    "aws_ecr_repository": {"name": ECR, "arn": ECR},
    "aws_ecr_lifecycle_policy": {"repository": ECR},
    "aws_ssm_parameter": {"name": SSM, "arn": SSM},
}

# The AWS service each family's ARNs must name. Checking this stops a value of the right
# shape but the wrong kind from satisfying a rule.
FAMILY_SERVICE = {FLAT: "iam", ECR: "ecr", SSM: "ssm"}

# ---------------------------------------------------------------------------
# Resource TYPES this module may create, update or destroy.
#
# Kept deliberately identical in spirit to tests/test_platform_isolation.py's allowlist:
# IAM identities for IRSA, ECR repositories for the pinned images, SSM parameters for
# configuration. No VPC, no EKS cluster, no database.
#
# `aws_db_instance` and friends are absent BY DECISION, not oversight: Decision 2 (shared
# vs. separate database instance) is unresolved, and this story may not claim an isolation,
# backup, retention or restore target. Whoever resolves Decision 2 adds the type here and
# records which durability property they are claiming.
# ---------------------------------------------------------------------------
ALLOWED_RESOURCE_TYPES = frozenset(
    {
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
        "aws_iam_policy",
        "aws_ecr_repository",
        "aws_ecr_lifecycle_policy",
        "aws_ssm_parameter",
        # Not an AWS resource: the plan-time precondition carrier in ecr.tf. Creates
        # nothing in the account.
        "terraform_data",
    }
)

# Types that must NEVER appear, at any nesting depth, whatever they are named. Redundant
# with the allowlist by construction — every one of these is already outside it — and kept
# as a named tripwire so that a future widening of the allowlist cannot quietly re-admit
# core platform infrastructure. If these two rules ever disagree, that is the bug.
PLATFORM_OWNED_TYPES = frozenset(
    {
        "aws_vpc",
        "aws_subnet",
        "aws_route_table",
        "aws_nat_gateway",
        "aws_internet_gateway",
        "aws_eks_cluster",
        "aws_eks_node_group",
        "aws_eks_fargate_profile",
        "aws_db_instance",
        "aws_rds_cluster",
        "aws_rds_cluster_instance",
        "aws_cloudfront_distribution",
        "aws_iam_openid_connect_provider",
        "aws_s3_bucket",
        "aws_dynamodb_table",
        "aws_lambda_function",
        "aws_api_gateway_rest_api",
        "aws_apigatewayv2_api",
        "aws_lb",
        "aws_lb_listener",
        "aws_lb_target_group",
    }
)

# Any Terraform action set containing one of these deletes something. Enumerated as a set
# membership test rather than a string match on the joined actions, because the orderings
# differ: a replacement is `["delete", "create"]` (destroy-then-create) or
# `["create", "delete"]` (create-before-destroy), and the pre-fix text-matching guard
# reported "No destroys in plan" for both.
DESTRUCTIVE_ACTIONS = frozenset({"delete"})

# Terraform's complete action vocabulary for `resource_changes[].change.actions`
# (`no-op`, `create`, `read`, `update`, `delete`, and the two replacement orderings which
# appear as two-element lists). Anything outside this set means the document is not a plan
# this guard understands — see the refusal in `validate_plan`.
VALID_ACTIONS = frozenset({"no-op", "create", "read", "update", "delete"})


class OwnershipError(Exception):
    """A plan or state artifact could not be validated. Always fail closed on this."""


@dataclass(frozen=True)
class Violation:
    address: str
    resource_type: str
    reason: str

    def __str__(self) -> str:
        return f"{self.address} [{self.resource_type}]: {self.reason}"


@dataclass
class OwnershipReport:
    """Outcome of validating a set of resource identities."""

    violations: list[Violation] = field(default_factory=list)
    destructive: list[str] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def has_destructive_changes(self) -> bool:
        return bool(self.destructive)


def leaf_type_and_name(address: str) -> tuple[str, str]:
    """Reduce a Terraform address to its leaf resource type and name.

    Handles arbitrary module nesting and instance keys, so that
    `module.core.module.net.aws_vpc.main[0]` yields `("aws_vpc", "main")` rather than
    hiding the type behind a `module.` prefix — which is how the pre-fix guard let a VPC
    destroy through.

    A `data.` prefix is reported as type `data.<type>`: reads are harmless but must be
    distinguishable from managed resources.
    """
    if not address or not isinstance(address, str):
        raise OwnershipError(f"malformed resource address: {address!r}")

    # Strip module nesting: repeatedly drop leading `module.<name>[key].`
    remainder = address
    while remainder.startswith("module."):
        parts = remainder.split(".", 2)
        if len(parts) < 3:
            raise OwnershipError(f"malformed module address: {address!r}")
        remainder = parts[2]

    is_data = remainder.startswith("data.")
    if is_data:
        remainder = remainder[len("data.") :]

    segments = remainder.split(".")
    if len(segments) < 2:
        raise OwnershipError(
            f"cannot determine resource type from address: {address!r}"
        )

    resource_type = segments[0]
    # Drop any instance key: `name[0]` / `name["key"]` -> `name`
    resource_name = re.sub(r"\[.*\]$", "", segments[1])

    if is_data:
        resource_type = f"data.{resource_type}"
    return resource_type, resource_name


def _parse_arn(value: str) -> dict[str, str] | None:
    """Split an ARN into its fields, or return None if it is not ARN-shaped.

    Field-wise parsing rather than `startswith("arn:")` plus string search: an ARN's account
    lives in field 4 and its service in field 2, and a substring test cannot tell an account
    id in the account field from the same digits appearing inside a resource name.
    """
    if not value.startswith("arn:"):
        return None
    parts = value.split(":", 5)
    if len(parts) < 6:
        return None
    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account": parts[4],
        "resource": parts[5],
    }


def _flat_pattern(environment: str) -> re.Pattern[str]:
    """`adp-<env>-superplane-<suffix>` — anchored at BOTH ends.

    Anchoring is the point. The previous unanchored substring test accepted
    `gateway-adp-superplane-api`, because the domain prefix appeared *somewhere* in it.
    """
    return re.compile(rf"^adp-{re.escape(environment)}-superplane-[a-z0-9][a-z0-9-]*$")


def _ssm_pattern(environment: str) -> re.Pattern[str]:
    """`/adp/<env>/superplane/<name>` — a path prefix, anchored at the start."""
    return re.compile(rf"^/adp/{re.escape(environment)}/superplane/[A-Za-z0-9._/-]+$")


ECR_NAME_PATTERN = re.compile(r"^adp-superplane-[a-z0-9][a-z0-9._-]*$")

# The repositories U2's lock declares. ECR ownership is decided against THIS INVENTORY, not
# against the name shape alone.
#
# Writing the shape check first was not enough, and the regression suite caught it:
# `adp-superplane-api-gateway` matches `adp-superplane-*` perfectly while being no repository
# this module creates. Shape says "plausibly ours"; the lock says "ours". The review asked for
# a "lock-owned ECR inventory", and this is the difference — a name invented by a lock edit,
# or by a hostile plan, is refused even when it is shaped correctly.
#
# Read lazily and cached: import time must not depend on the lock being readable, so a
# missing lock surfaces as a refusal at validation time with a reason.
_LOCK_PATH = Path(__file__).resolve().parents[2] / "releases" / "superplane.lock.yaml"
_ecr_inventory_cache: frozenset[str] | None = None


def lock_ecr_repositories() -> frozenset[str]:
    """Repository names declared in `releases/superplane.lock.yaml`.

    Parsed with a narrow regex rather than a YAML dependency: this module is imported by
    guards that run in the apply lane, and `ecr.tf` reads the same `ecr_repository:` keys, so
    the two stay in agreement by reading the same lines.
    """
    global _ecr_inventory_cache
    if _ecr_inventory_cache is None:
        try:
            text = _LOCK_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise OwnershipError(
                f"could not read the release lock at {_LOCK_PATH} to establish which ECR "
                f"repositories this domain owns: {exc}"
            ) from exc
        found = frozenset(re.findall(r"^\s*ecr_repository:\s*(\S+)\s*$", text, re.M))
        if not found:
            # An empty derivation must not make the check vacuous — the same failure mode as
            # an empty grep pattern matching everything.
            raise OwnershipError(
                f"no 'ecr_repository' entries found in {_LOCK_PATH}; refusing to validate "
                f"ECR ownership against an empty inventory"
            )
        _ecr_inventory_cache = found
    return _ecr_inventory_cache


def _require_environment(environment: str | None) -> str:
    """Environment-scoped families cannot be validated without knowing the environment.

    Refusing here is deliberate. If a missing `--environment` silently skipped the
    environment check, the most dangerous invocation — one that forgot to say which
    environment it was targeting — would be the least constrained.
    """
    if not environment:
        raise OwnershipError(
            "environment is required to validate environment-scoped resource names "
            "(IAM and SSM identifiers are named adp-<env>-superplane-* and "
            "/adp/<env>/superplane/*); refusing to skip the check"
        )
    if not VALID_ENVIRONMENT.match(environment):
        raise OwnershipError(
            f"environment {environment!r} is not a valid environment token "
            f"(lowercase alphanumeric, max 16 chars)"
        )
    return environment


def _check_value(
    value: str,
    family: str,
    *,
    account_id: str | None,
    environment: str | None,
) -> str | None:
    """Validate one identifying value against its naming family.

    Returns a reason string when the value is NOT attributable to this domain, or None when
    it is. Anything unrecognised yields a reason — deny unknown ownership.
    """
    if family == EXTERNAL_POLICY:
        # An AWS-managed or customer-managed policy ARN being ATTACHED to our role. The
        # policy is not ours and is not expected to carry our naming; what matters is that
        # it is a well-formed IAM policy ARN. The attachment's `role` field is checked
        # separately under FLAT, which is what prevents attaching to a foreign role.
        arn = _parse_arn(value)
        if (
            arn is None
            or arn["service"] != "iam"
            or not arn["resource"].startswith("policy/")
        ):
            return f"{value!r} is not a well-formed IAM policy ARN"
        return None

    arn = _parse_arn(value)
    if arn is not None:
        expected_service = FAMILY_SERVICE[family]
        if arn["service"] != expected_service:
            return (
                f"ARN {value!r} names service {arn['service']!r}, but this field must be "
                f"a {expected_service!r} ARN"
            )
        if account_id and arn["account"] and arn["account"] != account_id:
            return (
                f"ARN {value!r} names account {arn['account']}, but this run selected "
                f"{account_id}"
            )
        # Validate the resource portion under the same family rules as a bare name.
        # `arn:aws:ssm:…:parameter/adp/dev/superplane/x` -> `/adp/dev/superplane/x`
        resource = arn["resource"]
        for prefix in ("role/", "policy/", "repository/", "parameter"):
            if resource.startswith(prefix):
                resource = resource[len(prefix) :]
                break
        if family == SSM and not resource.startswith("/"):
            resource = "/" + resource
        value_to_match = resource
    else:
        value_to_match = value

    if family == FLAT:
        env = _require_environment(environment)
        if not _flat_pattern(env).match(value_to_match):
            return (
                f"{value_to_match!r} does not match this domain's environment-scoped "
                f"naming 'adp-{env}-superplane-*'"
            )
        return None

    if family == SSM:
        env = _require_environment(environment)
        if not _ssm_pattern(env).match(value_to_match):
            return (
                f"{value_to_match!r} is not under this domain's parameter path "
                f"'/adp/{env}/superplane/'"
            )
        return None

    if family == ECR:
        if not ECR_NAME_PATTERN.match(value_to_match):
            return (
                f"{value_to_match!r} does not match this domain's repository naming "
                f"'adp-superplane-*'"
            )
        # ECR names are environment-independent BY DESIGN (images are the same artifact
        # everywhere), so no environment is required. But a name that embeds an environment
        # token contradicts that design and is more likely a mistake than an intent —
        # `adp-superplane-dev-api` would give one environment its own copy of a digest.
        suffix = value_to_match[len(DOMAIN_PREFIX) :]
        leading = suffix.split("-", 1)[0]
        if leading in KNOWN_ENVIRONMENTS:
            return (
                f"{value_to_match!r} embeds the environment token {leading!r}; repository "
                f"names are environment-independent by design because they hold images"
            )
        # Shape is necessary but NOT sufficient — see lock_ecr_repositories(). A correctly
        # shaped name that the lock does not declare is not a repository this module creates.
        inventory = lock_ecr_repositories()
        if value_to_match not in inventory:
            return (
                f"{value_to_match!r} is not one of the repositories declared in "
                f"releases/superplane.lock.yaml ({', '.join(sorted(inventory))}); the lock "
                f"is what defines this domain's ECR inventory"
            )
        return None

    return f"no ownership rule for naming family {family!r}"


def validate_identity(
    address: str,
    values_before: dict | None = None,
    values_after: dict | None = None,
    *,
    account_id: str | None = None,
    environment: str | None = None,
) -> list[Violation]:
    """Validate one resource identity. Returns the violations found (empty == owned).

    BOTH before and after values are inspected. Checking only `after` would miss a
    destroy (whose `after` is null) and an update that renames a resource away from the
    domain — and a destroy is the case where being wrong is unrecoverable.
    """
    violations: list[Violation] = []
    resource_type, _ = leaf_type_and_name(address)

    bare_type = resource_type.removeprefix("data.")

    # Data sources are checked FIRST, and allowed, because reading a platform interface is
    # the sanctioned way to consume one. The requirement is "consume approved platform
    # interfaces without importing, applying or destroying core platform resources" — a
    # `data.aws_eks_cluster` read of the platform cluster is the approved pattern, not a
    # violation. Ordering matters: checking PLATFORM_OWNED_TYPES first would reject exactly
    # the read this module is supposed to perform.
    if resource_type.startswith("data."):
        return violations

    if bare_type in PLATFORM_OWNED_TYPES:
        violations.append(
            Violation(
                address,
                resource_type,
                "platform-owned resource type; this domain must consume it read-only",
            )
        )
        return violations

    if bare_type not in ALLOWED_RESOURCE_TYPES:
        violations.append(
            Violation(
                address,
                resource_type,
                "resource type is not in this domain's allowlist; add it deliberately in "
                "domain_ownership.py with the reasoning, or it is not ours to touch",
            )
        )
        return violations

    if bare_type == "terraform_data":
        return violations

    rules = IDENTITY_RULES.get(bare_type)
    if rules is None:
        # In the allowlist but with no identity rule: a combination nobody has decided how
        # to attribute. Refuse, rather than fall back to a generic test that would be the
        # unanchored substring check this replaces.
        violations.append(
            Violation(
                address,
                resource_type,
                "no identity rule defined for this type in IDENTITY_RULES; ownership "
                "cannot be established",
            )
        )
        return violations

    # BOTH before and after are inspected. `after` alone misses a destroy (whose `after` is
    # null) and an update that renames a resource out of the domain — and a destroy is the
    # case where being wrong is unrecoverable. A no-op/refresh carries equal before and
    # after; both are checked and agree, so it passes without a special case.
    checked_any = False
    for values in (values_before, values_after):
        if not isinstance(values, dict):
            continue
        for field_name, family in rules.items():
            value = values.get(field_name)
            if not isinstance(value, str) or not value:
                # Absent here is not a violation on its own: a create has no `before`, and
                # plan-time computed values are legitimately unknown. The
                # "some identifying field must be present somewhere" requirement is enforced
                # after both sides have been walked.
                continue
            checked_any = True
            reason = _check_value(
                value,
                family,
                account_id=account_id,
                environment=environment,
            )
            if reason is not None:
                violations.append(
                    Violation(address, resource_type, f"{field_name}: {reason}")
                )

    if not checked_any:
        # Deny unknown ownership. A resource whose identifying values are all unknown cannot
        # be attributed, and this guard's job is to refuse rather than to guess.
        violations.append(
            Violation(
                address,
                resource_type,
                f"none of the identifying field(s) {sorted(rules)} carry a known value, so "
                f"ownership cannot be established; refusing rather than assuming",
            )
        )

    return violations


def validate_plan(
    plan: dict,
    *,
    account_id: str | None = None,
    environment: str | None = None,
) -> OwnershipReport:
    """Validate a `terraform show -json <planfile>` document.

    Raises OwnershipError on anything malformed. A guard that cannot parse its input must
    not conclude "nothing to worry about" — that is the fail-open shape this replaces.
    """
    if not isinstance(plan, dict):
        raise OwnershipError("plan JSON is not an object")

    if "resource_changes" not in plan:
        # An empty plan legitimately has no changes, but `format_version` tells us we are
        # looking at a real plan document rather than, say, a truncated file or an error
        # payload that happened to be valid JSON.
        if "format_version" not in plan:
            raise OwnershipError(
                "plan JSON has neither 'resource_changes' nor 'format_version'; refusing "
                "to treat an unrecognised document as an empty plan"
            )

    report = OwnershipReport()

    # Schema BEFORE emptiness. The checkpoint review of `84e3f7ee` reproduced this: the
    # previous line was
    #
    #     changes = plan.get("resource_changes") or []
    #
    # which coerced every falsy wrong type — `false`, `0`, `""`, `{}` — into an accepted
    # empty list, so the `isinstance` check below it could never fire. The CLI printed
    # `Validated 0 resource change(s)` and exited 0 on
    # `{"format_version":"1.2","resource_changes":false}`. A guard that reports a malformed
    # document as "nothing to do" is the fail-open shape this module exists to remove.
    #
    # A legitimately EMPTY plan is preserved: `resource_changes` absent, or present as `[]`,
    # both mean "no changes" and both pass. The distinction is absent-or-list versus
    # present-but-not-a-list.
    if "resource_changes" in plan:
        changes = plan["resource_changes"]
        if not isinstance(changes, list):
            raise OwnershipError(
                f"'resource_changes' must be a list, got "
                f"{type(changes).__name__} ({changes!r}); refusing to treat a malformed "
                f"plan document as an empty plan"
            )
    else:
        changes = []

    for change in changes:
        if not isinstance(change, dict):
            raise OwnershipError(f"resource_changes entry is not an object: {change!r}")
        address = change.get("address")
        if not address:
            raise OwnershipError(f"resource_changes entry has no address: {change!r}")

        detail = change.get("change")
        if not isinstance(detail, dict):
            raise OwnershipError(f"{address}: 'change' is missing or not an object")
        actions = detail.get("actions")
        if not isinstance(actions, list) or not actions:
            raise OwnershipError(
                f"{address}: 'change.actions' is missing or not a list"
            )
        # Validate against Terraform's actual action vocabulary. Without this, an
        # unrecognised or misspelled action — `"destroy"` instead of `"delete"`, or a
        # truncated/hand-edited plan — would simply fail to intersect DESTRUCTIVE_ACTIONS
        # and be reported as a safe change. Deletion must be detected by recognising the
        # vocabulary, not by failing to recognise it.
        unknown = [a for a in actions if a not in VALID_ACTIONS]
        if unknown:
            raise OwnershipError(
                f"{address}: unrecognised action(s) {unknown!r} in 'change.actions'; valid "
                f"actions are {sorted(VALID_ACTIONS)}. Refusing to classify a plan whose "
                f"action vocabulary is not understood."
            )

        report.checked += 1

        if DESTRUCTIVE_ACTIONS.intersection(actions):
            # Covers ["delete"], ["delete","create"] and ["create","delete"] — a plain
            # destroy and both replacement orderings.
            report.destructive.append(f"{address} (actions: {', '.join(actions)})")

        # An import brings an existing resource under this state's management. If it is not
        # ours, a later destroy deletes somebody else's resource — so imports are validated
        # like any other change.
        report.violations.extend(
            validate_identity(
                address,
                detail.get("before"),
                detail.get("after"),
                account_id=account_id,
                environment=environment,
            )
        )

    # `terraform show -json` records imports here in some versions; validate them too.
    for imported in plan.get("resource_drift") or []:
        if isinstance(imported, dict) and imported.get("address"):
            report.violations.extend(
                validate_identity(
                    imported["address"],
                    (imported.get("change") or {}).get("before"),
                    (imported.get("change") or {}).get("after"),
                    account_id=account_id,
                    environment=environment,
                )
            )

    return report


def validate_state_addresses(
    addresses: list[str],
    *,
    account_id: str | None = None,
    environment: str | None = None,
) -> OwnershipReport:
    """Validate `terraform state list` output.

    State, not configuration, is what `terraform destroy` acts on. A resource that was
    declared here once, removed from source, and left in state is invisible to any
    source-reading check and still gets destroyed.

    Type-level only: `state list` prints addresses without values, so this cannot check
    names. It is a first gate — the destroy lane additionally validates the saved destroy
    plan, which does carry values.
    """
    report = OwnershipReport()
    for address in addresses:
        address = address.strip()
        if not address:
            continue
        report.checked += 1
        resource_type, _ = leaf_type_and_name(address)
        bare_type = resource_type.removeprefix("data.")

        if resource_type.startswith("data."):
            continue
        if bare_type in PLATFORM_OWNED_TYPES:
            report.violations.append(
                Violation(
                    address,
                    resource_type,
                    "platform-owned resource type present in domain state",
                )
            )
        elif bare_type not in ALLOWED_RESOURCE_TYPES:
            report.violations.append(
                Violation(
                    address,
                    resource_type,
                    "resource type is not in this domain's allowlist",
                )
            )
    return report
