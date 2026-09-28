"""Gate a saved workspace plan before apply — Issue #5532 (w6-09), design item 4.

Design item 4, in full: *"Produce saved plans, change inventories and bounded cost/resource
estimates; deny unexpected replacements/deletes or ownership changes without exact plan
authorization."*

Four obligations, and this file is where each becomes a check rather than a description:

*   **Saved plans.** It reads `terraform show -json <planfile>` output, not `plan` text and
    not a fresh plan. A guard that re-plans validates a *different* plan from the one that
    gets applied; the gap between them is where the plan-then-apply race lives.

    For any run that authorizes destruction, `--plan-file` supplies the SAVED ARTIFACT itself
    and the guard proves the JSON came from it — the artifact is verified to be a real
    Terraform saved plan, its JSON is re-derived with `terraform show -json` and compared, and
    its bytes are digested into the authorization. Binding only the JSON was review finding
    W9-04's second follow-up: `terraform apply` consumes the artifact, so a JSON-only binding
    left the applied object unbound and a destructive plan could be approved with no saved plan
    in existence at all.
*   **Change inventories.** `--inventory` writes a deterministic JSON inventory: every
    changed address with its actions, grouped counts, and the destructive set. Deterministic
    because a diffable inventory is what makes "this is the plan you reviewed" answerable at
    apply time.
*   **Bounded cost/resource estimates.** `--estimate` computes an upper bound from the PLAN's
    planned values (node ceiling, NAT gateways, control planes), and reports what it
    deliberately does not bound — see `_estimate`.
*   **Deny unexpected replacements/deletes or ownership changes without exact plan
    authorization.** `--authorize-destroy <file>` takes an authorization bound to THIS plan:
    the SHA-256 of the reviewed JSON *and* of the saved artifact that gets applied, the target
    account/region/environment/workspace **as established from the plan's own evidence**, and
    each destroyed resource with its concrete identity and its ordered actions. Details below.

## Why the authorization is bound to the PLAN and not to a list of addresses

The domain-app equivalent (`../../scripts/check_plan_safety.py`) gates destruction on a PR
label. That is right for a shared-account domain apply, where the question is *may this
revision destroy things*. It is not sufficient here, where the question is *which things* —
because a workspace plan run with the wrong `-var-file` is shaped exactly like a correct one,
and a label cannot tell an intended cluster replacement from an unintended one.

An address set is the next thing to reach for, and review finding W9-04 is that it is still not
enough: **an address list is not authorization for an exact plan.** Two materially different
plans can carry identical destroyed address sets — the same address deleted versus replaced, a
different target account or region (resource names and addresses are identical, since main.tf
builds them from the same expressions), or any non-destructive change at all. In each case the
old approval still matched and the operator approved a diff they never saw.

So the binding is the plan itself. `plan_sha256` covers every byte, which is what makes a
changed plan require fresh authorization even when its address set is unchanged; the target
and per-address action checks exist on top of it to give an actionable message rather than
just "this is not that plan". The address comparison remains symmetric, because a superset
authorization would still approve a later, different plan.

Authorizations are MACHINE-GENERATED — `--emit-authorization` writes one for the plan under
review — because nobody computes a digest by hand. The operator's act of approval is reading
the emitted document and committing it, not typing it.

## Fail-closed, specifically

Exit 0 means "safe to apply". Every other outcome — unreadable plan, malformed JSON, unknown
action vocabulary, a resource belonging to another workspace, an unknown instance type in the
estimate, an unreadable authorization file — exits 1 with the reason. The failure path and
the success path must never be indistinguishable, which is the defect PR #5283's review
reproduced in the shell guard this pattern replaces.

Nothing here reaches AWS, reads a credential, or calls a pricing API. The one subprocess it
runs is `terraform show -json` against a local saved plan file, which reads no state backend
and makes no API call — `terraform show` on a saved plan is an offline rendering of a file on
disk. That is what lets this run in the same offline lane as the rest of this module's checks.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace_ownership import (
    DESTRUCTIVE_ACTIONS,
    RELATIONSHIP_TARGET_FIELDS,
    relationship_values,
    REPLACEMENT_IS_DESTRUCTION,
    WorkspaceOwnershipError,
    leaf_type_and_name,
    validate_plan,
)
from region_version_policy import (
    HOURS_PER_MONTH as _HOURS_PER_MONTH,
    POLICY_REVIEWED_ON,
    REGION_MULTIPLIER_EVIDENCE,
    UnsupportedTargetError,
    check_cluster_version,
    check_region,
    control_plane_hourly_usd,
)

# ---------------------------------------------------------------------------
# The rate table for the bounded estimate.
#
# WHY A PINNED TABLE AND NOT THE PRICING API
#
# A guard that calls the AWS Pricing API needs a credential and a network, which would move
# this check out of the offline lane and make a pricing-endpoint outage indistinguishable from
# an expensive plan. The estimate's purpose is a reviewable upper bound and an
# order-of-magnitude sanity check on a plan — "this workspace can cost at most about $N/month"
# — not an invoice. A pinned table with a stated date and region does that, and a rate that
# has drifted produces a slightly wrong bound rather than an unavailable check.
#
# On-demand Linux rates, us-east-1, as published 2026-09. Deliberately NOT discounted for
# Savings Plans or Spot: the bound must hold when no discount applies.
#
# An instance type absent from this table is REFUSED rather than priced at zero. A silent zero
# is the fail-open shape: the lane would print a reassuringly small bound for a plan whose
# node group is an accelerated instance costing two orders of magnitude more.
# ---------------------------------------------------------------------------
RATE_TABLE_REGION = "us-east-1"
RATE_TABLE_AS_OF = "2026-09"

INSTANCE_HOURLY_USD: dict[str, float] = {
    "t3.medium": 0.0416,
    "t3.large": 0.0832,
    "m6i.large": 0.096,
    "m6i.xlarge": 0.192,
    "m6i.2xlarge": 0.384,
    "m7i.large": 0.1008,
    "m7i.xlarge": 0.2016,
    "c6i.large": 0.085,
    "c6i.xlarge": 0.17,
    "r6i.large": 0.126,
    "r6i.xlarge": 0.252,
}

# Fixed hourly charges that exist because the resource exists, independent of usage.
#
# The EKS control-plane rate is deliberately NOT here: it depends on the cluster's Kubernetes
# version support tier ($0.10/hour standard, $0.60/hour extended — six times), so it lives in
# region_version_policy.py beside the support calendar that decides the tier. Review finding
# W9-05 requires the estimate to validate "EKS support-tier pricing before calling the result an
# upper bound", and a flat 0.10 constant here would understate an extended-support cluster by
# $365/month while reading as a bound.
NAT_GATEWAY_HOURLY_USD = 0.045
KMS_KEY_MONTHLY_USD = 1.00

# gp3 storage, us-east-1, per GiB-month. Priced because the launch template added for finding
# W9-02 SETS the node root volume size (var.node_volume_size, capped at 500 GiB), which makes
# node storage a bounded cost for the first time — it used to be in UNBOUNDED_COMPONENTS on the
# grounds that the plan "does not set and therefore cannot bound" it. It does now.
EBS_GP3_MONTHLY_USD_PER_GIB = 0.08

# Re-exported, not redefined. It lives beside the hourly rate table in region_version_policy so
# that module can state a monthly consequence in its own refusal messages without importing this
# one (which imports it — the cycle is why it moved). Imported under this name here because every
# cost expectation in the test suite and every rate arithmetic below already reads it from here.
HOURS_PER_MONTH = _HOURS_PER_MONTH

# Cost components a plan document cannot bound, reported by name rather than omitted.
#
# Omitting them would make the estimate read as complete. Each of these is driven by usage a
# plan says nothing about, and inventing a number for them would put a fabricated figure next
# to the computed ones with nothing to distinguish them.
UNBOUNDED_COMPONENTS = [
    (
        "CloudWatch Logs ingestion and storage for the five control-plane log types — driven "
        "by cluster activity, not by anything in the plan. Retention is bounded "
        "(variables.tf refuses never-expire), so this is bounded in TIME but not in volume."
    ),
    "NAT gateway data processing — charged per GB, driven by the workspace's egress volume.",
    "Private STS interface endpoint data processing — charged per GB; hourly AZ charges are bounded.",
    "Inter-AZ and internet data transfer.",
    (
        "EBS snapshots and any volume a WORKLOAD creates (PersistentVolumeClaims). The node "
        "ROOT volumes are now bounded and priced — the launch template added for finding W9-02 "
        "sets their size — but a pod may request storage this plan says nothing about."
    ),
    (
        "Anything #5533 (w6-10) later schedules onto this cluster, including GPU capacity. "
        "This module creates a general-purpose group only; the bound is for an EMPTY "
        "workspace."
    ),
]


def _fail(message: str) -> int:
    print(f"::error::{message}", file=sys.stderr)
    print(f"DENIED: {message}")
    return 1


def _load_json(path: Path, what: str) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkspaceOwnershipError(
            f"could not read {what} at {path}: {exc}"
        ) from exc
    if not text.strip():
        raise WorkspaceOwnershipError(f"{what} at {path} is empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkspaceOwnershipError(
            f"{what} at {path} is not valid JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Change inventory
# ---------------------------------------------------------------------------
def build_inventory(
    plan: dict, *, environment: str, workspace_name: str, org_id: str, workspace_id: str
) -> dict:
    """A deterministic, diffable record of what this plan changes.

    Sorted by address throughout: Terraform's ordering is stable in practice but not
    guaranteed, and an inventory whose line order varies between runs cannot be diffed to
    answer "is this the plan that was reviewed?".
    """
    entries = []
    for change in plan.get("resource_changes") or []:
        address = change["address"]
        actions = list(change["change"]["actions"])
        resource_type, _ = leaf_type_and_name(address)
        entry = {
            "address": address,
            "type": resource_type,
            "actions": actions,
            "destructive": bool(DESTRUCTIVE_ACTIONS.intersection(actions)),
        }
        consequence = REPLACEMENT_IS_DESTRUCTION.get(resource_type)
        if entry["destructive"] and consequence:
            entry["consequence"] = consequence
        entries.append(entry)

    entries.sort(key=lambda item: item["address"])

    by_action: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    for entry in entries:
        by_action["+".join(entry["actions"])] += 1
        by_type[entry["type"]] += 1

    return {
        "environment": environment,
        "workspace_name": workspace_name,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "terraform_format_version": plan.get("format_version"),
        "total_changes": len(entries),
        "counts_by_action": dict(sorted(by_action.items())),
        "counts_by_type": dict(sorted(by_type.items())),
        "destructive_addresses": sorted(
            entry["address"] for entry in entries if entry["destructive"]
        ),
        "changes": entries,
        "node_image_pin": plan.get("planned_values", {})
        .get("outputs", {})
        .get("node_image_pin", {})
        .get("value"),
    }


# ---------------------------------------------------------------------------
# Bounded cost and resource estimate
# ---------------------------------------------------------------------------
def _planned(change: dict) -> dict:
    after = change.get("change", {}).get("after")
    return after if isinstance(after, dict) else {}


# ---------------------------------------------------------------------------
# WHICH ACTIONS THE ESTIMATE PRICES (review finding W9-05)
#
# ## The defect
#
# The estimate counted a resource only when `"create" in actions`, with this rationale:
#
#     # Only created resources add to the bound. An update leaves the resource in place and
#     # its charge already counted in the previous estimate; a pure delete reduces cost...
#
# The delete half is right. The rest is not, and it made the estimate silently wrong in the
# two most common cases a workspace is actually re-planned:
#
#   *   `["update"]` — the plan that RAISES a node group's ceiling from 2 to 40, or swaps
#       m6i.large for m6i.24xlarge, is an update. It priced at $0.00. The one plan whose whole
#       purpose is to change capacity was the one the estimate refused to look at, and "already
#       counted in the previous estimate" assumes a previous estimate exists and is being
#       compared, which nothing in this lane does.
#   *   `["no-op"]` — a full re-plan of an existing, unchanged workspace is all no-ops. It
#       priced at $0.00, so the answer to "what does this workspace cost" was zero for every
#       workspace that already existed.
#
# ## What it prices now
#
# The RESULTING BOUNDED CAPACITY, not the delta. After this plan applies, what can the
# workspace cost per month at its reviewed ceiling? That question has the same answer whether
# the resource is being created, updated, replaced or left alone — and it is the question a
# reviewer is actually asking. A delta needs a baseline the lane does not have.
#
#   create              -> priced from `after`.
#   update              -> priced from `after`, which is the post-apply state.
#   no-op               -> priced from `after` (Terraform populates it; it equals `before`).
#   delete+create       -> priced from `after`. A replacement ends with the resource existing.
#   delete              -> NOT priced, and that is explicit rather than incidental. The
#                          resource is gone afterwards, so it contributes nothing to the
#                          resulting capacity. Pricing a teardown as a cost is the misleading
#                          direction the original rationale correctly identified.
#   read                -> NOT priced. A data source reads something this module does not own.
# ---------------------------------------------------------------------------
PRICED_ACTIONS = frozenset({"create", "update", "no-op"})
UNPRICED_ACTIONS = frozenset({"delete", "read"})


def _priced_after_apply(actions: list[str]) -> bool:
    """Does this resource EXIST after the plan applies, and so contribute to the bound?

    An allowlist intersection rather than `"delete" not in actions`, so an action vocabulary
    this function does not recognise cannot silently fall into "priced at whatever `after`
    happens to hold". `validate_plan` has already refused anything outside Terraform's
    vocabulary by this point; the assertion below is the second lock.
    """
    unknown = set(actions) - PRICED_ACTIONS - UNPRICED_ACTIONS
    if unknown:
        raise WorkspaceOwnershipError(
            f"cannot price a change with action(s) {sorted(unknown)!r}: the estimate does not "
            f"know whether the resource exists afterwards. Refusing to treat it as free."
        )
    # A pure delete or read leaves nothing to charge for. Anything else — including a
    # replacement, which is a delete AND a create — ends with the resource in place.
    return bool(PRICED_ACTIONS.intersection(actions))


def _require(change: dict, values: dict, field: str, why: str):
    """Read a value the bound DEPENDS on, refusing when the plan leaves it unknown.

    Terraform omits an attribute from `after` when its value is not known until apply. For most
    attributes that is unremarkable, but for these it is the difference between a bound and a
    guess: review finding W9-05 requires the estimate to "refuse unknown required values", and
    the reason is that `values.get("max_size") or 0` would turn an unknown ceiling into a $0.00
    line. A number that is wrong in the reassuring direction is worse than no number.
    """
    if field not in values or values[field] is None:
        raise WorkspaceOwnershipError(
            f"{change['address']}: the plan does not give a known value for {field!r}, so "
            f"{why}. Refusing to price it at zero — an unknown required value must deny the "
            f"estimate rather than silently become a small number. If the value is unknown "
            f"until apply, the bound for this workspace cannot be computed from this plan."
        )
    return values[field]


# AWS VPC public IPv4 pricing, us-east-1, reviewed 2026-09-20:
# https://aws.amazon.com/vpc/pricing/ — in-use and idle addresses are $0.005/hour.
PUBLIC_IPV4_HOURLY_USD = 0.005
# https://aws.amazon.com/privatelink/pricing/ (us-east-1, reviewed 2026-09-24).
INTERFACE_ENDPOINT_AZ_HOURLY_USD = 0.01


def _estimate(
    plan: dict, *, aws_region: str, cluster_version: str | None = None
) -> dict:
    """An UPPER BOUND on this workspace's fixed monthly cost, from the plan's own values.

    Read from the plan rather than from the tfvars, because the plan is what gets applied.
    The bound is computed at the node group's `max_size`, never its `desired_size`: the
    question a reviewer needs answered is what this workspace can cost if it scales to its
    reviewed ceiling, and a desired-size figure understates that by the whole point of having
    a ceiling. variables.tf caps `max_size` at 100 precisely so this number is finite.

    It prices the RESULTING CAPACITY of every action class that leaves the resource in place,
    not only creations — see the note above `PRICED_ACTIONS` for the W9-05 defect that changed.

    Three things must be established before the result may be called an upper bound, and each
    raises rather than degrading:

    *   the REGION has verified service-specific rates. Currently only us-east-1 is priced.
    *   the cluster's EKS SUPPORT TIER, because extended support costs six times standard.
    *   every required value is KNOWN. An unknown ceiling is not a ceiling.
    """
    # Region availability and pricing evidence are separate requirements. Reference EC2
    # multipliers cannot establish a bound for other services, even when the factor is 1.0.
    try:
        check_region(aws_region)
        if aws_region != RATE_TABLE_REGION:
            raise UnsupportedTargetError(
                f"bounded pricing is supported only for {RATE_TABLE_REGION}; "
                f"{aws_region} needs verified per-service regional rates. "
                "An EC2 multiplier does not bound EKS, NAT, KMS and gp3 prices."
            )
        multiplier = 1.0
    except UnsupportedTargetError as exc:
        raise WorkspaceOwnershipError(
            f"the bounded estimate cannot be produced: {exc}"
        ) from exc

    lines: list[dict] = []
    resources: Counter[str] = Counter()
    skipped: list[str] = []

    def line(component: str, basis: str, us_east_1_monthly: float) -> None:
        """Record one component, converting the base-region price into this region's bound."""
        adjusted = us_east_1_monthly * multiplier
        entry = {
            "component": component,
            "basis": basis,
            "monthly_usd": round(adjusted, 2),
        }
        if multiplier != 1.0:
            entry["region_adjustment"] = (
                f"${us_east_1_monthly:.2f} at {RATE_TABLE_REGION} rates x {multiplier} "
                f"for {aws_region}"
            )
        lines.append(entry)

    for change in plan.get("resource_changes") or []:
        actions = change["change"]["actions"]
        if not _priced_after_apply(actions):
            # Recorded by name rather than silently dropped. "Why is this teardown $0.00" is a
            # question the artifact should answer on its own.
            skipped.append(f"{change['address']} ({'+'.join(actions)})")
            continue

        resource_type, _ = leaf_type_and_name(change["address"])
        resources[resource_type] += 1
        values = _planned(change)

        if resource_type == "aws_eks_cluster":
            # The support tier, and therefore the rate. Taken from the plan's own `version`
            # when it has one, because the plan is what gets applied; the caller's
            # --cluster-version is the fallback for a plan that leaves it unknown.
            version = values.get("version") or cluster_version
            if not version:
                raise WorkspaceOwnershipError(
                    f"{change['address']}: neither the plan nor --cluster-version gives the "
                    f"cluster's Kubernetes version, so its support tier is unknown. Standard "
                    f"support is $0.10/hour and extended is $0.60/hour — six times — so "
                    f"guessing the tier produces a figure that is not a bound (finding W9-05)."
                )
            try:
                support = check_cluster_version(str(version))
                hourly = control_plane_hourly_usd(support)
            except UnsupportedTargetError as exc:
                raise WorkspaceOwnershipError(
                    f"{change['address']}: {exc} A plan targeting a version that cannot be "
                    f"created has no meaningful cost bound, and the estimate must not be the "
                    f"place this is first noticed."
                ) from exc
            tier_note = (
                f" [{support.tier} support"
                + (
                    f"; standard support ended {support.standard_support_ends}"
                    if support.tier == "extended"
                    else ""
                )
                + "]"
            )
            line(
                f"EKS control plane, Kubernetes {version}{tier_note}",
                f"1 cluster x ${hourly:.2f}/hour x {HOURS_PER_MONTH} hours "
                f"({support.tier}-support rate)",
                hourly * HOURS_PER_MONTH,
            )

        elif resource_type == "aws_nat_gateway":
            line(
                "NAT gateway (hourly only; data processing is unbounded)",
                f"1 gateway x ${NAT_GATEWAY_HOURLY_USD:.3f}/hour x {HOURS_PER_MONTH} hours",
                NAT_GATEWAY_HOURLY_USD * HOURS_PER_MONTH,
            )

        elif resource_type == "aws_vpc_endpoint":
            if values.get("vpc_endpoint_type") != "Interface":
                raise WorkspaceOwnershipError(
                    "Only reviewed interface endpoint pricing is available"
                )
            subnets = values.get("subnet_ids")
            if (
                isinstance(subnets, list)
                and subnets
                and all(isinstance(value, str) and value for value in subnets)
            ):
                ceiling = len(set(subnets))
            else:
                # The ownership guard confines every endpoint subnet to this plan.
                # All resulting owned subnets give a conservative AZ bound even if
                # Terraform has not resolved the cardinality of its computed IDs.
                ceiling = len(
                    {
                        item["address"]
                        for item in plan.get("resource_changes", [])
                        if leaf_type_and_name(item["address"])[0] == "aws_subnet"
                        and _priced_after_apply(item["change"]["actions"])
                    }
                )
            if ceiling < 1:
                raise WorkspaceOwnershipError(
                    "Private STS endpoint subnet ceiling is unknown"
                )
            line(
                "Private STS interface endpoint (hourly; data processing excluded)",
                f"At most {ceiling} endpoint AZs x ${INTERFACE_ENDPOINT_AZ_HOURLY_USD:.2f}/hour x {HOURS_PER_MONTH} hours",
                ceiling * INTERFACE_ENDPOINT_AZ_HOURLY_USD * HOURS_PER_MONTH,
            )

        elif resource_type == "aws_kms_key":
            line(
                "KMS customer-managed key",
                f"1 key x ${KMS_KEY_MONTHLY_USD:.2f}/month",
                KMS_KEY_MONTHLY_USD,
            )

        elif resource_type == "aws_eks_node_group":
            instance_types = _require(
                change,
                values,
                "instance_types",
                "a node cost bound cannot be computed",
            )
            scaling = _require(
                change, values, "scaling_config", "the node ceiling is unknown"
            )
            if not isinstance(instance_types, list) or not instance_types:
                raise WorkspaceOwnershipError(
                    f"{change['address']}: instance_types is {instance_types!r}, so a node "
                    f"cost bound cannot be computed. Refusing to report an unbounded node "
                    f"group as costing nothing."
                )
            if not isinstance(scaling, list) or not scaling:
                raise WorkspaceOwnershipError(
                    f"{change['address']}: scaling_config is {scaling!r}, so the node "
                    f"ceiling is unknown. Refusing to report an unbounded node group as "
                    f"costing nothing."
                )
            max_size = (scaling[0] or {}).get("max_size")
            if (
                not isinstance(max_size, int)
                or isinstance(max_size, bool)
                or max_size < 0
            ):
                raise WorkspaceOwnershipError(
                    f"{change['address']}: scaling_config.max_size is {max_size!r}, which "
                    f"gives no finite ceiling. Design item 4's bounded estimate depends on "
                    f"this being a whole number."
                )

            # The most expensive type in the list, because the group may launch any of them
            # and this is an upper bound.
            unpriced = [t for t in instance_types if t not in INSTANCE_HOURLY_USD]
            if unpriced:
                raise WorkspaceOwnershipError(
                    f"{change['address']}: instance type(s) {unpriced!r} are not in this "
                    f"guard's rate table ({RATE_TABLE_REGION}, as of {RATE_TABLE_AS_OF}). "
                    f"Refusing to price them at zero — a silent zero would report a "
                    f"reassuringly small bound for a plan whose nodes may cost two orders "
                    f"of magnitude more. Add the type with its published on-demand rate to "
                    f"INSTANCE_HOURLY_USD in check_workspace_plan.py."
                )
            rate = max(INSTANCE_HOURLY_USD[t] for t in instance_types)
            dearest = max(instance_types, key=lambda t: INSTANCE_HOURLY_USD[t])
            line(
                f"Node group compute at its ceiling ({dearest})",
                f"{max_size} nodes (max_size) x ${rate:.4f}/hour x {HOURS_PER_MONTH} hours",
                rate * max_size * HOURS_PER_MONTH,
            )

        elif resource_type == "aws_eip":
            line(
                "NAT public IPv4 address",
                f"${PUBLIC_IPV4_HOURLY_USD:.3f}/hour x {HOURS_PER_MONTH} hours",
                PUBLIC_IPV4_HOURLY_USD * HOURS_PER_MONTH,
            )

        elif resource_type == "aws_launch_template":
            # Priced because of finding W9-02's launch template: it SETS the node root volume
            # size, so node storage became a bounded cost. The volume count is the node group's
            # ceiling, which is why this reads the plan's OTHER resource rather than its own.
            ceiling = _node_ceiling(plan)
            if ceiling is None:
                continue
            size = _node_volume_size(change, values)
            line(
                f"Node root EBS volumes at the node ceiling ({size} GiB gp3 each)",
                f"{ceiling} volumes (node max_size) x {size} GiB x "
                f"${EBS_GP3_MONTHLY_USD_PER_GIB:.2f}/GiB-month",
                EBS_GP3_MONTHLY_USD_PER_GIB * size * ceiling,
            )

    _verify_node_storage(plan)
    lines.sort(key=lambda item: item["component"])
    return {
        "rate_table_region": RATE_TABLE_REGION,
        "rate_table_as_of": RATE_TABLE_AS_OF,
        "priced_for_region": aws_region,
        "region_price_multiplier": multiplier,
        # The multiplier's evidence travels WITH the number (finding W9-05). It was derived from
        # EC2 price spread and is applied to every component, so publishing the factor alone
        # invited the reader to treat the non-compute lines as verified per-service ceilings. Only
        # relevant when an adjustment actually happened.
        **(
            {"region_multiplier_basis": REGION_MULTIPLIER_EVIDENCE}
            if multiplier != 1.0
            else {}
        ),
        "support_policy_reviewed_on": POLICY_REVIEWED_ON,
        # Qualified rather than a bare True. The compute term — which dominates a workspace at its
        # node ceiling — is a genuine bound: dearest permitted instance type at max_size. The
        # region adjustment on the non-compute lines rests on a stated assumption, named above, so
        # claiming an unqualified bound for every line would overstate the evidence.
        "is_upper_bound": True,
        "upper_bound_scope": (
            "Compute, storage and fixed per-resource charges are bounded at the reviewed ceiling "
            "in the rate table's base region "
            f"({RATE_TABLE_REGION}, {RATE_TABLE_AS_OF}), with the EKS control plane priced at its "
            "version's actual support tier. Items in 'not_bounded_by_this_estimate' are not "
            "included at all."
            + (
                " Because this workspace is not in the base region, every line has been scaled by "
                "'region_price_multiplier'; see 'region_multiplier_basis' for what that factor is "
                "and is not evidence of."
                if multiplier != 1.0
                else ""
            )
        ),
        "bound_basis": (
            "The RESULTING monthly cost of this workspace after the plan applies, at its "
            "reviewed ceiling — not the delta from its current cost, which would need a "
            "baseline this lane does not have. Every resource that exists afterwards is "
            "priced, whether the plan creates, updates, replaces or leaves it alone; a pure "
            "delete is not, and those addresses are listed in "
            "'not_priced_because_removed_or_read'. Computed at the node group's max_size, not "
            "its desired_size: the reviewable figure is what this workspace CAN cost."
        ),
        "priced_actions": sorted(PRICED_ACTIONS),
        "resource_counts_priced": dict(sorted(resources.items())),
        "not_priced_because_removed_or_read": sorted(skipped),
        "components": lines,
        "bounded_monthly_usd": round(sum(line["monthly_usd"] for line in lines), 2),
        "not_bounded_by_this_estimate": UNBOUNDED_COMPONENTS,
    }


def _node_volume_size(change: dict, values: dict) -> int:
    """The module's only launch template must define its bounded root disk."""
    mappings = values.get("block_device_mappings")
    if not isinstance(mappings, list):
        raise WorkspaceOwnershipError(
            "Node root block_device_mappings is missing or unknown"
        )
    for mapping in mappings:
        if not isinstance(mapping, dict) or mapping.get("device_name") != "/dev/xvda":
            continue
        ebs = mapping.get("ebs")
        # The provider represents this nested block as a single-element list.
        if isinstance(ebs, list):
            ebs = ebs[0] if ebs else None
        if not isinstance(ebs, dict):
            raise WorkspaceOwnershipError(
                f"{change['address']}: the /dev/xvda mapping carries no readable ebs block, "
                f"so the node root volume size cannot be bounded."
            )
        size = ebs.get("volume_size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise WorkspaceOwnershipError(
                f"{change['address']}: /dev/xvda volume_size is {size!r}, so node storage "
                f"cannot be bounded. variables.tf requires node_volume_size between 20 and "
                f"500 GiB precisely so this is a known number."
            )
        return size
    raise WorkspaceOwnershipError("Node root /dev/xvda mapping is missing")


def _verify_node_storage(plan: dict) -> None:
    """Bind resulting nodes to the reviewed template/version and encrypted root key.

    Computed IDs on creates require the exact authenticated configuration reference;
    missing data is not a zero-cost disk. Concrete IDs/versions must agree directly.
    """
    changes = {c["address"]: c for c in plan.get("resource_changes", [])}
    nodes = [
        c
        for c in changes.values()
        if leaf_type_and_name(c["address"])[0] == "aws_eks_node_group"
        and _priced_after_apply(c["change"]["actions"])
    ]
    if not nodes:
        return
    template = changes.get("aws_launch_template.node")
    if not template or not _priced_after_apply(template["change"]["actions"]):
        raise WorkspaceOwnershipError(
            "Resulting node group has no reviewed launch template"
        )
    values = _planned(template)
    _node_volume_size(template, values)
    mappings = values["block_device_mappings"]
    if len(mappings) != 1 or mappings[0].get("device_name") != "/dev/xvda":
        raise WorkspaceOwnershipError(
            "Only the single bounded /dev/xvda root mapping is supported"
        )
    blocks = mappings[0].get("ebs")
    if not isinstance(blocks, list) or len(blocks) != 1:
        raise WorkspaceOwnershipError("The node root disk needs exactly one ebs block")
    ebs = blocks[0]
    if (
        not (ebs.get("encrypted") is True or ebs.get("encrypted") == "true")
        or ebs.get("volume_type") != "gp3"
    ):
        raise WorkspaceOwnershipError("Node root disk must be encrypted gp3")
    config = {
        r["address"]: r.get("expressions", {})
        for r in plan.get("configuration", {})
        .get("root_module", {})
        .get("resources", [])
    }
    try:
        root_config = config[template["address"]]["block_device_mappings"][0]["ebs"][0]
    except (KeyError, IndexError, TypeError):
        root_config = {}
    # The pinned rate includes gp3 baseline IOPS/throughput, not purchased extras.
    for field, baseline in (
        ("iops", 3000),
        ("throughput", 125),
        ("volume_initialization_rate", 0),
    ):
        actual = ebs.get(field)
        if actual is not None:
            if type(actual) not in (int, float) or actual > baseline or actual < 0:
                raise WorkspaceOwnershipError(
                    f"Node root {field} exceeds the priced gp3 baseline"
                )
        elif field in root_config:
            raise WorkspaceOwnershipError(
                f"Unknown configured node root {field} cannot be cost-bounded"
            )
    supplied = plan.get("variables", {}).get("kms_key_arn", {}).get("value", "")
    key_change = changes.get("aws_kms_key.workspace[0]")
    if (
        not supplied
        and key_change
        and not _priced_after_apply(key_change["change"]["actions"])
    ):
        raise WorkspaceOwnershipError(
            "Resulting node storage cannot reference a removed workspace key"
        )
    key = supplied or (_planned(key_change).get("arn") if key_change else None)
    if key:
        if ebs.get("kms_key_id") != key:
            raise WorkspaceOwnershipError(
                "Node root disk key differs from the reviewed workspace key"
            )
    else:
        try:
            key_unknown = (
                template["change"]["after_unknown"]["block_device_mappings"][0]["ebs"][
                    0
                ]["kms_key_id"]
                is True
            )
            refs = config[template["address"]]["block_device_mappings"][0]["ebs"][0][
                "kms_key_id"
            ]
            owned_unknown = (
                key_change
                and key_change["change"]["actions"] == ["create"]
                and key_change["change"]["after_unknown"]["arn"] is True
            )
        except (KeyError, IndexError, TypeError):
            key_unknown = owned_unknown = False
            refs = None
        if (
            ebs.get("kms_key_id") is not None
            or not key_unknown
            or not owned_unknown
            or refs != {"references": ["local.kms_key_arn"]}
        ):
            raise WorkspaceOwnershipError(
                "Node root disk lacks a verifiable reviewed KMS key binding"
            )
    for node in nodes:
        bindings = _planned(node).get("launch_template")
        if not isinstance(bindings, list) or len(bindings) != 1:
            raise WorkspaceOwnershipError(
                "Resulting node group must reference exactly one reviewed launch template"
            )
        binding = bindings[0]
        try:
            expression = config[node["address"]]["launch_template"][0]
        except (KeyError, IndexError, TypeError):
            expression = {}
        identifier = binding.get("id")
        name = binding.get("name")
        if identifier is not None:
            if not values.get("id") or identifier != values["id"]:
                raise WorkspaceOwnershipError(
                    "Node group references a different launch template ID"
                )
        elif name is not None:
            if name != values.get("name"):
                raise WorkspaceOwnershipError(
                    "Node group references a different launch template name"
                )
        elif expression.get("id") != {
            "references": ["aws_launch_template.node.id", "aws_launch_template.node"]
        }:
            raise WorkspaceOwnershipError(
                "Unknown node launch-template ID lacks the reviewed configuration binding"
            )
        version = binding.get("version")
        if version is not None:
            if not str(version).isdigit() or str(version) != str(
                values.get("latest_version")
            ):
                raise WorkspaceOwnershipError(
                    "Node group must use the exact reviewed launch-template version"
                )
        elif expression.get("version") != {
            "references": [
                "aws_launch_template.node.latest_version",
                "aws_launch_template.node",
            ]
        }:
            raise WorkspaceOwnershipError(
                "Unknown node template version lacks the reviewed configuration binding"
            )


def _node_ceiling(plan: dict) -> int | None:
    """The node group's `max_size` from elsewhere in this plan — the EBS volume count.

    Returns None when the plan contains no priced node group, which is why the EBS line is
    skipped rather than computed: volumes are created per node, so with no node group there is
    no ceiling to multiply by. A plan containing a launch template and no node group launches
    nothing.
    """
    for change in plan.get("resource_changes") or []:
        if leaf_type_and_name(change["address"])[0] != "aws_eks_node_group":
            continue
        if not _priced_after_apply(change["change"]["actions"]):
            continue
        scaling = _planned(change).get("scaling_config")
        if isinstance(scaling, list) and scaling and isinstance(scaling[0], dict):
            max_size = scaling[0].get("max_size")
            if isinstance(max_size, int) and not isinstance(max_size, bool):
                return max_size
    return None


# ===========================================================================
# THE SAVED ARTIFACT (review finding W9-04, second follow-up)
# ===========================================================================
# ## The defect
#
# The guard's own docstring said it "reads `terraform show -json <planfile>` output, not `plan`
# text and not a fresh plan", and the whole authorization was bound to the SHA-256 of that JSON.
# But nothing established that the JSON came from a saved plan at all. `--plan-json` took any
# readable file, so a destructive plan could be authorized and approved with NO SAVED BINARY IN
# EXISTENCE — reproduced on attempt 3's head, which approved a hand-written JSON document
# describing a cluster replacement.
#
# That matters because the JSON is not what gets applied. `terraform apply` consumes the BINARY
# plan file. Digesting only the JSON binds the review artifact and leaves the applied artifact
# unbound, so the two can differ by construction: the reviewed JSON and the applied plan file
# need not be related at all.
#
# ## What is checked now
#
# For any run that authorizes (or emits an authorization for) a destructive plan, `--plan-file`
# is REQUIRED and three things are established about it:
#
#   1.  it is a real Terraform saved plan — a zip archive containing a `tfplan` member, which is
#       what `terraform apply <file>` reads. A truncated or hand-made file is refused here rather
#       than at apply time;
#   2.  the supplied JSON is DERIVED FROM IT, verified by running `terraform show -json` on the
#       artifact and comparing the resulting document to the one under review. This is the check
#       that makes "the plan I reviewed" and "the plan that gets applied" the same object;
#   3.  its bytes are digested, and that digest is bound into the authorization alongside the
#       JSON's. The apply step re-digests the file it is about to apply and must match.
#
# Comparison is on the PARSED documents, not the bytes: an operator who pipes `terraform show
# -json` through `jq` has the same plan with different whitespace, and refusing that would push
# people to bypass the check. Byte equality is asserted where it belongs — on the artifact
# itself, which is what apply consumes.
#
# A missing or unusable `terraform` binary REFUSES. It does not skip: "I could not verify the
# artifact" must never resolve to "the artifact is fine", which is the fail-open shape this whole
# file exists to remove.
# ---------------------------------------------------------------------------
SAVED_PLAN_MEMBER = "tfplan"


def _read_artifact(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise WorkspaceOwnershipError(
            f"could not read the saved plan artifact at {path}: {exc}. An unreadable artifact "
            f"denies: the file that cannot be read is the file that would be applied."
        ) from exc
    if not data:
        raise WorkspaceOwnershipError(
            f"the saved plan artifact at {path} is empty. `terraform plan -out` did not write "
            f"it, or it was truncated; either way there is nothing to apply and nothing to bind "
            f"an authorization to."
        )
    return data


def _assert_is_saved_plan(path: Path, data: bytes) -> None:
    """Refuse anything that is not a Terraform saved plan archive.

    Checked structurally rather than by extension. A saved plan is a zip containing a `tfplan`
    member (plus state and a copy of the configuration); `terraform apply <file>` reads exactly
    that. Accepting an arbitrary file would let the digest bind something no apply could consume,
    which is a binding to the wrong object rather than a binding.
    """
    import zipfile  # stdlib; imported here to keep the module's import list honest about scope

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise WorkspaceOwnershipError(
            f"{path} is not a Terraform saved plan: {exc}. A saved plan is the binary file "
            f"`terraform plan -out=<file>` writes and `terraform apply <file>` reads. A JSON "
            f"document is NOT one — `terraform show -json` output is a RENDERING of a plan, and "
            f"passing it here would bind the authorization to the review artifact while leaving "
            f"the applied artifact unbound (finding W9-04)."
        ) from exc

    if SAVED_PLAN_MEMBER not in members:
        raise WorkspaceOwnershipError(
            f"{path} is a zip archive but contains no {SAVED_PLAN_MEMBER!r} member, so it is not "
            f"a Terraform saved plan. Members found: {sorted(members)}."
        )


def _derive_plan_json(path: Path, terraform_binary: str, working_dir: Path) -> dict:
    """The plan JSON Terraform ITSELF produces from this artifact.

    This is the independent evidence. Every other fact in this guard is read from a document
    someone handed it; this one is read from the artifact that will be applied, by the tool that
    will apply it.

    Run from `working_dir`, which must be the INITIALIZED module directory — not the plan file's
    parent. Established by experiment rather than assumed: `terraform show -json` on a saved plan
    loads the provider schemas from the local `.terraform/` directory to decode it, so from an
    uninitialized directory it fails with "Failed to load plugin schemas" even though the plan
    file itself is perfectly readable. Running it in the module directory with the plan file
    passed by absolute path works, which is exactly the shape a lane has after `terraform plan
    -out`.
    """
    try:
        completed = subprocess.run(
            [terraform_binary, "show", "-json", str(path.resolve())],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(working_dir),
        )
    except OSError as exc:
        raise WorkspaceOwnershipError(
            f"could not run {terraform_binary!r} to verify the saved plan artifact: {exc}. The "
            f"artifact check is not optional and does not degrade to a skip — an unverified "
            f"artifact must not read as a verified one. Install Terraform in this lane, or pass "
            f"--terraform-binary with its path."
        ) from exc

    if completed.returncode != 0:
        raise WorkspaceOwnershipError(
            f"`{terraform_binary} show -json {path}` (run in {working_dir}) exited "
            f"{completed.returncode}, so the artifact could not be rendered and the JSON under "
            f"review cannot be shown to come from it. Note this command needs the module's "
            f"provider schemas, so it must run in the INITIALIZED module directory — pass "
            f"--module-dir if the guard is not being run from one. Terraform said: "
            f"{(completed.stderr or completed.stdout).strip()[:400]}"
        )

    try:
        derived = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WorkspaceOwnershipError(
            f"`{terraform_binary} show -json {path}` did not produce valid JSON: {exc}"
        ) from exc

    if not isinstance(derived, dict):
        raise WorkspaceOwnershipError(
            f"`{terraform_binary} show -json {path}` produced a "
            f"{type(derived).__name__}, not a plan object."
        )
    return derived


def _assert_json_derives_from_artifact(
    plan: dict, derived: dict, *, plan_json: Path, plan_file: Path
) -> None:
    """Compare the entire parsed rendering, ignoring only formatting and object-key order."""

    def canonical(value: object) -> str:
        # Python equality conflates True with 1 and False with 0. JSON encoding preserves
        # those types and also distinguishes missing keys from explicit nulls.
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    try:
        problems = [
            key
            for key in sorted(plan.keys() | derived.keys())
            if key not in plan
            or key not in derived
            or canonical(plan[key]) != canonical(derived[key])
        ]
    except (TypeError, ValueError) as exc:
        raise WorkspaceOwnershipError(
            "plan rendering contains invalid JSON values"
        ) from exc

    if problems:
        raise WorkspaceOwnershipError(
            f"{plan_json} is NOT a rendering of {plan_file}: they disagree on {problems}. "
            f"`terraform show -json` on the artifact produces a different plan from the one "
            f"under review, so the document that was reviewed and the artifact that would be "
            f"applied are different plans. Regenerate the JSON from the artifact you intend to "
            f"apply: `terraform show -json {plan_file.name} > {plan_json.name}`."
        )


# ===========================================================================
# THE TARGET, CHECKED AGAINST THE PLAN'S OWN EVIDENCE (review finding W9-04, first follow-up)
# ===========================================================================
# ## The defect
#
# The authorization recorded `account_id`, `aws_region`, `environment` and `workspace_name`, and
# `_check_authorization` compared them against... the same CLI flags that had written them. Both
# sides of that comparison came from one source, so it could not disagree with itself: a plan
# whose own `variables` declared `us-east-1` was emitted as an eu-west-1 authorization and then
# accepted by an eu-west-1 check. Reproduced on attempt 3's head, exit 0.
#
# A label supplied by the caller is not evidence about the plan. What the plan actually targets
# is recorded IN the plan:
#
#   *   `variables` — the root variable values the plan was taken with. `main.tf` builds every
#       name, the provider region and the target-account precondition from exactly these, so
#       they are the plan's own statement of its target.
#   *   resource ARNs — field 3 is the region and field 4 is the account. Independent of the
#       variables (the provider produced them), so they CORROBORATE rather than restate.
#
# ## The rule
#
# Every target field must be established from the plan's own evidence, and the CLI flag must
# AGREE with it. Three distinct refusals, because they mean different things:
#
#   *   the plan's evidence is missing → refuse. A plan that cannot say what it targets cannot
#       be authorized for a target.
#   *   the plan's evidence contradicts itself (variables say one region, the ARNs another) →
#       refuse. That is a plan assembled from more than one source.
#   *   the plan's evidence contradicts the flag → refuse, and say which is which. This is the
#       wrong-region approval, and the message names the plan's value as the authority.
# ---------------------------------------------------------------------------
TARGET_VARIABLES = (
    "account_id",
    "aws_region",
    "environment",
    "workspace_name",
    "org_id",
    "workspace_id",
)


def _plan_variable(plan: dict, name: str) -> str | None:
    variables = plan.get("variables")
    if not isinstance(variables, dict):
        return None
    entry = variables.get(name)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    return value if isinstance(value, str) and value else None


def _arn_evidence(plan: dict) -> tuple[set[str], set[str]]:
    """Regions and accounts appearing in the plan's own resource ARNs.

    Only a resource's OWN `arn` is read, for the reason `_check_account` gives: `role_arn`,
    `kms_key_arn` and similar name OTHER resources, and a workspace may legitimately use an
    operator-supplied key from another account.

    IAM and other global services leave the region field empty, so an empty field is skipped
    rather than treated as a conflicting region.
    """
    regions: set[str] = set()
    accounts: set[str] = set()
    for change in plan.get("resource_changes") or []:
        if not isinstance(change, dict):
            continue
        detail = change.get("change")
        if not isinstance(detail, dict):
            continue
        for side in ("before", "after"):
            values = detail.get(side)
            if not isinstance(values, dict):
                continue
            arn = values.get("arn")
            if not isinstance(arn, str) or not arn.startswith("arn:"):
                continue
            parts = arn.split(":", 5)
            if len(parts) < 6:
                continue
            if parts[3]:
                regions.add(parts[3])
            if parts[4]:
                accounts.add(parts[4])
    return regions, accounts


def verify_target(
    plan: dict, supplied: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Establish this plan's target from the PLAN, and require the flags to agree.

    Returns `(verified_values, evidence)` where `evidence` records, per field, what established
    it — so the authorization document says how each binding was checked rather than only what it
    was bound to. Raises on any of the three refusals described above.
    """
    verified: dict[str, str] = {}
    evidence: dict[str, str] = {}
    problems: list[str] = []

    arn_regions, arn_accounts = _arn_evidence(plan)
    corroboration: dict[str, set[str]] = {
        "aws_region": arn_regions,
        "account_id": arn_accounts,
    }

    for name in TARGET_VARIABLES:
        declared = _plan_variable(plan, name)
        sources: list[str] = []
        if declared:
            sources.append(f"plan variables.{name}")

        observed = corroboration.get(name) or set()
        if declared and observed - {declared}:
            problems.append(
                f"the plan CONTRADICTS ITSELF about {name}: its own `variables.{name}` says "
                f"{declared!r} while the ARNs of its resources say {sorted(observed)}. A plan "
                f"whose target is inconsistent internally was assembled from more than one "
                f"source, and no single value can be authorized for it."
            )
            continue
        if not declared and len(observed) == 1:
            declared = next(iter(observed))
            sources.append(f"resource ARNs ({name} field)")
        elif declared and observed:
            sources.append(f"corroborated by {len(observed)} resource ARN value(s)")

        if not declared:
            problems.append(
                f"the plan carries no evidence of its own {name}: neither `variables.{name}` nor "
                f"a resource ARN establishes it. The {name} cannot be taken from the command "
                f"line alone, because then the authorization's {name} and the value it is "
                f"checked against both come from the caller and the comparison cannot fail "
                f"(finding W9-04). Produce the plan JSON with `terraform show -json` from a plan "
                f"taken with this module's tfvars, which set {name}."
            )
            continue

        given = supplied.get(name) or ""
        if given and given != declared:
            problems.append(
                f"this run says {name}={given!r}, but the PLAN says {name}={declared!r} "
                f"({'; '.join(sources)}). The plan is the authority: it is what gets applied. An "
                f"authorization written from the command-line value would pin a target the plan "
                f"does not have, and would then be checked against that same command-line value "
                f"— so it could never disagree. That is the wrong-region approval finding W9-04 "
                f"reproduced."
            )
            continue

        verified[name] = declared
        evidence[name] = "; ".join(sources)

    if problems:
        raise WorkspaceOwnershipError(
            "the plan's target could not be independently established:\n"
            + "\n".join(f"    - {problem}" for problem in problems)
        )

    return verified, evidence


# ===========================================================================
# WHAT EXACTLY WOULD BE DESTROYED (review finding W9-04, review-evidence requirement)
# ===========================================================================
# An address is a label this module's source chooses. "aws_eks_cluster.workspace would be
# replaced" does not tell a reviewer WHICH cluster, and the review asked for the concrete
# identities and the ORDERED actions in the evidence, not just the address set.
#
# Ordered, specifically, because sorting collapses the distinction that matters:
# `sorted(["delete","create"]) == sorted(["create","delete"])`. Those two are Terraform's
# destroy-before-create and create-before-destroy, and they are not the same operation — the
# second keeps the old resource alive until the new one exists. An authorization that sorted its
# actions could not tell them apart, so approving one approved the other.
# ---------------------------------------------------------------------------
IDENTITY_FIELDS_FOR_EVIDENCE = ("id", "arn", "name", "node_group_name")


def destructive_identity(change: dict) -> dict:
    """The BEFORE side's concrete identity: what this change would actually destroy.

    The before side specifically. On a replacement the `after` describes the resource that will
    exist afterwards, which is not the one being destroyed — naming it in the destruction
    evidence would describe the wrong object.
    """
    detail = change.get("change") or {}
    before = detail.get("before")
    values = before if isinstance(before, dict) else {}

    identity = {
        field: values[field]
        for field in IDENTITY_FIELDS_FOR_EVIDENCE
        if isinstance(values.get(field), str) and values[field]
    }

    # What it points at, for the relationship-bearing types that have no identity of their own.
    # Without these, the evidence for "this attachment will be destroyed" says nothing about
    # whose permissions change.
    resource_type, _ = leaf_type_and_name(change["address"])
    for target_field in RELATIONSHIP_TARGET_FIELDS.get(resource_type, {}):
        targets = relationship_values(values, target_field)
        if all(isinstance(value, str) and value for value in targets):
            identity[target_field] = targets if "[]" in target_field else targets[0]

    if not identity:
        # Not a refusal: a create-before-destroy replacement of a resource whose id is
        # AWS-assigned can legitimately have an unpopulated before on a first-time import-free
        # plan. Saying so is better than an empty object the reader has to interpret.
        return {
            "note": (
                "the plan records no identifying value on this change's before side, so the "
                "object to be destroyed can only be named by its Terraform address"
            )
        }
    return identity


# ===========================================================================
# EXACT PLAN AUTHORIZATION (review finding W9-04)
# ===========================================================================
# ## The defect
#
# Authorization used to be a set of Terraform addresses, compared symmetrically against the
# plan's destroyed set. That is not authorization for an EXACT PLAN, because an address set is
# not a plan. Two different plans can have identical destroyed address sets and entirely
# different content:
#
#   *   `aws_eks_cluster.workspace` deleted, versus the same address REPLACED
#       (`["delete","create"]`). Same address, same "destroyed" status, and one of them keeps
#       the workspace running while the other does not.
#   *   the reviewed plan targets account 111122223333 / us-east-1 / dev / tenant-alpha; the
#       re-run plan targets a different account or environment. Names and addresses are
#       IDENTICAL — main.tf builds them from the same expressions — so the address set matches
#       and the approval transfers to a workspace nobody reviewed.
#   *   anything non-destructive changed between the two plans: an instance type, a CIDR, an
#       endpoint's public access. The destroyed set is unchanged, so the old approval still
#       matched, and the operator approved a diff they never saw.
#
# The docstring claimed "a reusable approval is not an approval of this plan" while the
# mechanism made every approval reusable across exactly those substitutions.
#
# ## What is bound now
#
# The authorization is a JSON document naming, and checked against:
#
#   1.  `plan_sha256` — the digest of the saved plan JSON BYTES. This is the binding that makes
#       the other checks belt-and-braces: any change anywhere in the plan changes the digest,
#       so a changed plan requires fresh authorization even when its address set is identical.
#   2.  `account_id`, `aws_region`, `environment`, `workspace_name` — the target identity, so an
#       approval cannot migrate to another tenant, environment or region.
#   3.  `destroy` — the intended destructive identities AND their actions, so approving a delete
#       does not silently approve a replacement of the same address.
#
# ## Why JSON here when the address list was deliberately plain text
#
# The old format's rationale was that an operator hand-writes it while reading the plan, and a
# JSON syntax error at that moment is an obstacle with no safety value. That reasoning does not
# survive the digest: nobody computes a SHA-256 by hand. So the file is now MACHINE-GENERATED —
# `--emit-authorization` writes it from the plan under review — and the operator's act of
# approval is reviewing the emitted document and committing it, not typing it. A malformed file
# denies, with the command to regenerate it in the message.
# ---------------------------------------------------------------------------
# Bumped from 1 for the W9-04 follow-ups. A version-1 document bound only the plan JSON's digest
# and recorded a target taken from the caller's own flags, so reading one now would be reading an
# approval whose scope was never independently established. `_load_authorization` refuses it by
# version rather than by missing field, so the message can say what changed.
# Version 3 additionally requires immutable org/workspace IDs. Legacy plans must be regenerated.
AUTHORIZATION_SCHEMA_VERSION = 3

# Every field an authorization must pin, with why. Used for the emitted document, the checks
# below, and the error messages, so the three cannot describe different requirements.
BOUND_TARGET_FIELDS = (
    "account_id",
    "aws_region",
    "environment",
    "workspace_name",
    "org_id",
    "workspace_id",
)


def _plan_digest(path: Path) -> str:
    """SHA-256 of the saved plan JSON as BYTES, exactly as the guard read it.

    Bytes rather than a re-serialisation of the parsed object: `json.dumps` of a loaded
    document normalises key order, whitespace and number formatting, so two materially
    different files could hash the same. The digest must identify the artifact on disk.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:  # pragma: no cover - already read successfully by this point
        raise WorkspaceOwnershipError(
            f"could not re-read {path} to digest it: {exc}"
        ) from exc


def build_authorization(
    *,
    plan_sha256: str,
    plan_file_sha256: str,
    plan_file_name: str,
    account_id: str,
    aws_region: str,
    environment: str,
    workspace_name: str,
    org_id: str,
    workspace_id: str,
    target_evidence: dict[str, str],
    destructive_actions: dict[str, list[str]],
    destructive_identities: dict[str, dict],
) -> dict:
    """The authorization document for one exact plan, ready for an operator to review.

    Emitted rather than hand-written — see the note above on why the format changed from a
    plain address list to JSON.

    Two digests, because there are two artifacts and only one of them gets applied:
    `plan_sha256` covers the JSON that was REVIEWED, `plan_file_sha256` covers the binary saved
    plan that will be APPLIED. Binding only the first was the W9-04 follow-up: the reviewed
    document and the applied artifact were unrelated objects.

    `destroy` maps each address to its actions IN PLAN ORDER — never sorted. `["delete","create"]`
    and `["create","delete"]` sort identically and are different operations (destroy-then-create
    versus create-then-destroy), so sorting would make one an authorization for the other.
    """
    return {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "what_this_authorizes": (
            "Applying EXACTLY the saved plan artifact whose SHA-256 is recorded in "
            "`plan_file_sha256`, whose rendering is the JSON recorded in `plan_sha256`, against "
            "exactly the target recorded below, destroying exactly the resources listed in "
            "`destroy` with exactly the ordered actions given. Any difference in any of those "
            "requires a freshly emitted authorization: a changed plan is a different plan even "
            "when its destroyed address set is identical."
        ),
        "plan_sha256": plan_sha256,
        "plan_file_sha256": plan_file_sha256,
        "plan_file_name": plan_file_name,
        "account_id": account_id,
        "aws_region": aws_region,
        "environment": environment,
        "workspace_name": workspace_name,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "target_established_by": dict(sorted(target_evidence.items())),
        "destroy": {
            address: list(actions)
            for address, actions in sorted(destructive_actions.items())
        },
        "destroy_identities": {
            address: destructive_identities.get(address, {})
            for address in sorted(destructive_actions)
        },
    }


def _load_authorization(path: Path) -> dict:
    """Read and shape-check an authorization document. Anything unparseable denies."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkspaceOwnershipError(
            f"could not read the destroy authorization at {path}: {exc}. An unreadable "
            f"authorization denies; it never means 'no restrictions'."
        ) from exc

    if not text.strip():
        raise WorkspaceOwnershipError(
            f"the destroy authorization at {path} is empty. An empty file authorizes nothing, "
            f"which denies any destructive plan — regenerate it with --emit-authorization."
        )

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkspaceOwnershipError(
            f"the destroy authorization at {path} is not valid JSON: {exc}. It is a "
            f"machine-generated document, not a hand-written list — regenerate it by running "
            f"this guard with --emit-authorization against the plan you intend to apply, then "
            f"review and commit the result."
        ) from exc

    if not isinstance(document, dict):
        raise WorkspaceOwnershipError(
            f"the destroy authorization at {path} is a {type(document).__name__}, not a JSON "
            f"object. The pre-W9-04 format was a plain list of addresses, which is no longer "
            f"sufficient: an address set is not a plan, and an approval bound only to addresses "
            f"transfers to any later plan that destroys the same ones. Regenerate with "
            f"--emit-authorization."
        )

    version = document.get("schema_version")
    if version != AUTHORIZATION_SCHEMA_VERSION:
        older = (
            " A version-1 document bound only the plan JSON's digest and recorded a target taken "
            "from the caller's own flags — so it did not bind the artifact that gets applied, and "
            "its target could not disagree with the value it was checked against. Neither "
            "property can be recovered by reading it more carefully; regenerate with "
            "--emit-authorization."
            if version == 1
            else " Version-2 evidence used a non-unique display name; prepare again with immutable org_id/workspace_id."
            if version == 2
            else ""
        )
        raise WorkspaceOwnershipError(
            f"the destroy authorization at {path} has schema_version {version!r}, but this "
            f"guard requires {AUTHORIZATION_SCHEMA_VERSION}. Refusing to interpret an "
            f"authorization written against a different set of guarantees.{older}"
        )

    missing = [
        name
        for name in (
            "plan_sha256",
            "plan_file_sha256",
            "destroy",
            "destroy_identities",
            *BOUND_TARGET_FIELDS,
        )
        if not document.get(name) and document.get(name) != {}
    ]
    if missing:
        raise WorkspaceOwnershipError(
            f"the destroy authorization at {path} is missing or has empty {missing}. Every one "
            f"of those is part of what the approval is bound to, and an authorization that "
            f"omits one is an approval whose scope is unknown."
        )

    if not isinstance(document["destroy"], dict):
        raise WorkspaceOwnershipError(
            f"the destroy authorization at {path} has a 'destroy' that is a "
            f"{type(document['destroy']).__name__}, not an object mapping each address to its "
            f"authorized actions."
        )

    identities = document["destroy_identities"]
    if not isinstance(identities, dict) or any(
        not isinstance(address, str)
        or not isinstance(identity, dict)
        or not identity
        or any(
            not isinstance(key, str)
            or not (
                isinstance(value, str)
                and bool(value)
                or isinstance(value, list)
                and bool(value)
                and all(isinstance(item, str) and bool(item) for item in value)
            )
            for key, value in identity.items()
        )
        for address, identity in identities.items()
    ):
        raise WorkspaceOwnershipError(
            "destroy_identities must map each destructive address to nonempty string or string-list identity fields"
        )

    return document


def _check_authorization(
    report_actions: dict[str, list[str]],
    authorization: Path,
    *,
    plan_sha256: str,
    plan_file_sha256: str,
    verified_target: dict[str, str],
    destructive_identities: dict[str, dict],
) -> tuple[bool, list[str]]:
    """Check the plan against the authorization on all four bindings.

    `verified_target` comes from `verify_target`, which establishes each value from the PLAN's own
    evidence. That is the W9-04 follow-up: previously this compared the document's target against
    the same CLI flags that had written it, so the comparison could not fail — a plan declaring
    `us-east-1` was emitted and accepted as eu-west-1.

    The two digests alone would detect every mismatch — between them they cover the reviewed JSON
    and the applied artifact byte for byte. The target and action checks are kept anyway because
    they produce an ACTIONABLE message: a digest mismatch says only "this is not that plan",
    whereas "this authorization is for environment 'prod'" tells the operator what happened. A
    guard whose refusal cannot be acted on gets bypassed.
    """
    document = _load_authorization(authorization)
    problems: list[str] = []

    # 1. The reviewed plan document's bytes. This is the binding that makes a changed plan require
    # fresh authorization even when its destroyed address set is identical — W9-04's stated test.
    if document["plan_sha256"] != plan_sha256:
        problems.append(
            f"This authorization is bound to a DIFFERENT plan document.\n"
            f"    authorized plan JSON SHA-256: {document['plan_sha256']}\n"
            f"    this plan JSON's SHA-256:     {plan_sha256}\n"
            f"    Every byte of the plan is covered, so this fires for any change — including "
            f"one that leaves the destroyed address set identical, such as a different instance "
            f"type, a changed CIDR, or a delete that became a replacement. Re-review the new "
            f"plan and regenerate the authorization with --emit-authorization."
        )

    # 2. The SAVED ARTIFACT's bytes — the file `terraform apply` actually consumes. Separate from
    # the JSON digest because they are separate objects: binding only the JSON left the applied
    # artifact unbound, which is the second W9-04 follow-up.
    if document["plan_file_sha256"] != plan_file_sha256:
        problems.append(
            f"This authorization is bound to a DIFFERENT saved plan ARTIFACT.\n"
            f"    authorized artifact SHA-256: {document['plan_file_sha256']}\n"
            f"    this artifact's SHA-256:     {plan_file_sha256}\n"
            f"    The artifact is what `terraform apply` reads; the JSON is only its rendering. "
            f"An authorization that matched the JSON but not the artifact would approve applying "
            f"a plan file nobody reviewed. Re-run `terraform plan -out` / `terraform show -json` "
            f"as a pair and regenerate the authorization."
        )

    # 3. The target identity, as ESTABLISHED FROM THE PLAN. Resource names are built from the
    # environment and workspace, so two workspaces' plans differ in their names — but the ACCOUNT
    # and REGION appear in neither a name nor an address, and the same tenant can exist in more
    # than one environment.
    for name in BOUND_TARGET_FIELDS:
        actual = verified_target.get(name)
        expected = document.get(name)
        if expected != actual:
            problems.append(
                f"This authorization is for {name}={expected!r}, but the PLAN targets "
                f"{name}={actual!r}. An approval does not transfer between tenants, "
                f"environments, accounts or regions: a plan run with the wrong -var-file is "
                f"shaped exactly like a correct one. Note the plan's own value is what this is "
                f"compared against, not the command line's — otherwise both sides of this "
                f"comparison would come from the caller and it could never fail (W9-04)."
            )

    # 4. The intended destructive identities AND actions, symmetrically. Symmetric for the
    # reason it always was: an authorization listing more than the plan destroys was written
    # against a different plan, and accepting it makes the approval reusable.
    #
    # Actions compared IN ORDER. `sorted(["delete","create"]) == sorted(["create","delete"])`,
    # and those are destroy-then-create versus create-then-destroy — different operations with
    # different availability consequences, so sorting made one authorize the other.
    authorized_destroy = document["destroy"]
    planned = {address: list(actions) for address, actions in report_actions.items()}

    unauthorized = sorted(set(planned) - set(authorized_destroy))
    if unauthorized:
        problems.append(
            "These resources would be destroyed or replaced but are NOT in the "
            "authorization:\n"
            + "\n".join(
                f"    - {address} (actions: {', '.join(planned[address])})"
                for address in unauthorized
            )
        )

    missing = sorted(set(authorized_destroy) - set(planned))
    if missing:
        problems.append(
            "These addresses are authorized but this plan does not destroy them, so the "
            "authorization was written against a DIFFERENT plan:\n"
            + "\n".join(f"    - {address}" for address in missing)
            + "\n    A superset authorization would approve a later, different plan; that is "
            "why the match is exact in both directions rather than a subset test."
        )

    # The ACTIONS on a commonly-named address. This is the substitution an address-only
    # authorization could not see: approving the deletion of a cluster is not approving its
    # replacement, and vice versa.
    for address in sorted(set(planned) & set(authorized_destroy)):
        allowed = authorized_destroy[address]
        if not isinstance(allowed, list):
            problems.append(
                f"    - {address}: the authorization records its actions as a "
                f"{type(allowed).__name__}, not a list. Regenerate with "
                f"--emit-authorization."
            )
            continue
        if list(allowed) != planned[address]:
            consequence = REPLACEMENT_IS_DESTRUCTION.get(
                leaf_type_and_name(address)[0], ""
            )
            ordering = (
                " These are the same actions in a DIFFERENT ORDER: "
                "['delete','create'] destroys the resource and then creates its replacement, "
                "while ['create','delete'] (create_before_destroy) keeps the old one alive until "
                "the new one exists. The availability consequences differ, so one is not "
                "authorization for the other — which is why this comparison is ordered and not "
                "a set test."
                if sorted(allowed) == sorted(planned[address])
                else ""
            )
            problems.append(
                f"{address} is authorized for actions {list(allowed)} but this plan performs "
                f"{planned[address]}. A delete and a replacement of the same address are "
                f"different consequences, so one is not authorization for the other."
                + ordering
                + (f" Note: {consequence}." if consequence else "")
            )

    if document["destroy_identities"] != destructive_identities:
        problems.append(
            "destroy_identities does not exactly match the authenticated plan's destroyed "
            "IDs, ARNs, names and relationship targets. Review the plan and regenerate authorization."
        )

    return (not problems), problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan-json",
        required=True,
        type=Path,
        help="output of `terraform show -json <saved planfile>`",
    )
    parser.add_argument(
        "--plan-file",
        type=Path,
        default=None,
        help=(
            "the SAVED PLAN ARTIFACT (`terraform plan -out=<file>`) that --plan-json renders. "
            "Required for every plan verification: the artifact is what "
            "`terraform apply` consumes, and an authorization bound only to the JSON leaves the "
            "applied object unbound (W9-04). The guard verifies the artifact is a real saved "
            "plan, re-derives its JSON with `terraform show -json`, and binds its digest."
        ),
    )
    parser.add_argument(
        "--terraform-binary",
        default="terraform",
        help=(
            "the Terraform executable used to re-derive the saved plan's JSON. Not optional in "
            "effect: if it cannot be run, the artifact check REFUSES rather than skipping."
        ),
    )
    parser.add_argument(
        "--module-dir",
        type=Path,
        default=None,
        help=(
            "the INITIALIZED module directory to run `terraform show -json` in. Defaults to this "
            "script's parent module. Needed because `show -json` decodes a saved plan using the "
            "provider schemas in that directory's `.terraform/`, so it fails from an "
            "uninitialized working directory even when the plan file is readable."
        ),
    )
    parser.add_argument(
        "--environment",
        required=True,
        help="the environment this plan targets; part of every resource name",
    )
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument(
        "--workspace-name",
        required=True,
        help="Display label to bind to this reviewed plan; immutable --org-id/--workspace-id identify its owner.",
    )
    parser.add_argument(
        "--account-id",
        default="",
        help="the workspace account; resources whose own ARN names another are refused",
    )
    parser.add_argument(
        "--aws-region",
        default="",
        help=(
            "the region this apply targets. Part of what a destroy authorization is bound to "
            "(W9-04): the region appears in no resource name or address, so without it an "
            "approval reviewed for one region transfers to another."
        ),
    )
    parser.add_argument(
        "--cluster-version",
        default="",
        help=(
            "the Kubernetes version this workspace targets. Used for the control plane's "
            "support-tier rate when the plan itself does not carry a known version (W9-05), "
            "and checked against the reviewed version policy (F6)."
        ),
    )
    parser.add_argument(
        "--emit-authorization",
        type=Path,
        default=None,
        help=(
            "write a destroy authorization for THIS exact plan here, for an operator to review "
            "and commit. Emitting is not approving: this flag never applies anything and "
            "exits non-zero if the plan is not otherwise safe."
        ),
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=None,
        help="write the deterministic change inventory here (design item 4)",
    )
    parser.add_argument(
        "--estimate",
        type=Path,
        default=None,
        help="write the bounded cost/resource estimate here (design item 4)",
    )
    parser.add_argument(
        "--authorize-destroy",
        type=Path,
        default=None,
        help=(
            "authorization document for THIS exact plan, as written by "
            "--emit-authorization: it binds the plan's SHA-256, the target "
            "account/region/environment/workspace, and each destroyed address with its exact "
            "actions. Required for any plan containing a delete or a replacement."
        ),
    )
    parser.add_argument(
        "--expect-destroy",
        action="store_true",
        help=(
            "this plan is a `terraform destroy` plan: a plan that deletes NOTHING is then "
            "rejected as not being the destroy that was requested (a stale plan file, or "
            "one taken without -destroy). Ownership and exact authorization still apply."
        ),
    )
    args = parser.parse_args(argv)

    # ---------------------------------------------------------------------------
    # FINDING F6: the region and version support policy, checked BEFORE anything else.
    #
    # First in main() on purpose. F6 asks that an unavailable or nonexistent target be refused
    # "before mutations", and this guard runs between `terraform plan` and `terraform apply` —
    # so this is the last place a retired version or an unreviewed region can be caught while
    # nothing has been built yet. variables.tf refuses them earlier still; this covers the lane
    # that reaches apply with a saved plan produced elsewhere.
    #
    # Missing required target/evidence inputs are refused below for every invocation.
    # ---------------------------------------------------------------------------
    for label, value, check in (
        ("--aws-region", args.aws_region, check_region),
        ("--cluster-version", args.cluster_version, check_cluster_version),
    ):
        if not value:
            continue
        try:
            check(value)
        except UnsupportedTargetError as exc:
            return _fail(
                f"{label}={value!r} is outside this platform's reviewed support policy, so "
                f"this plan must not be applied: {exc} Refused here, before the apply, because "
                f"the alternative is an opaque AWS error after the VPC and subnets already "
                f"exist. Policy reviewed {POLICY_REVIEWED_ON}; see "
                f"scripts/region_version_policy.py."
            )

    try:
        plan = _load_json(args.plan_json, "plan JSON")
        if not isinstance(plan, dict):
            raise WorkspaceOwnershipError("plan JSON is not an object")
        plan_sha256 = _plan_digest(args.plan_json)
        report = validate_plan(
            plan,
            environment=args.environment,
            workspace_name=args.workspace_name,
            org_id=args.org_id,
            workspace_id=args.workspace_id,
            account_id=args.account_id or None,
        )
    except WorkspaceOwnershipError as exc:
        return _fail(f"Plan could not be validated, so it is not safe to apply: {exc}")

    print(
        f"Validated {report.checked} resource change(s) for workspace "
        f"{args.workspace_name!r} in {args.environment!r}."
    )

    if not report.ok:
        for violation in report.violations:
            print(f"  - {violation}")
        return _fail(
            f"This plan touches resources outside workspace {args.workspace_name!r}'s ownership, or changes which party owns them. Nothing was applied."
        )

    plan_file_sha256 = ""
    verified_target: dict[str, str] = {}
    target_evidence: dict[str, str] = {}
    required = {
        "--account-id": args.account_id,
        "--aws-region": args.aws_region,
        "--plan-file": args.plan_file,
        "--inventory": args.inventory,
        "--estimate": args.estimate,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        return _fail(
            f"{', '.join(missing)} is required for every safe-to-apply plan. The saved artifact is what Terraform applies; JSON is only its rendering."
        )
    try:
        artifact = _read_artifact(args.plan_file)
        _assert_is_saved_plan(args.plan_file, artifact)
        plan_file_sha256 = hashlib.sha256(artifact).hexdigest()
        derived = _derive_plan_json(
            args.plan_file,
            args.terraform_binary,
            args.module_dir or Path(__file__).resolve().parents[1],
        )
        _assert_json_derives_from_artifact(
            plan,
            derived,
            plan_json=args.plan_json,
            plan_file=args.plan_file,
        )
        verified_target, target_evidence = verify_target(
            plan,
            {
                "account_id": args.account_id,
                "aws_region": args.aws_region,
                "environment": args.environment,
                "workspace_name": args.workspace_name,
                "org_id": args.org_id,
                "workspace_id": args.workspace_id,
            },
        )
    except WorkspaceOwnershipError as exc:
        return _fail(
            f"The plan artifact or its target could not be verified, so no authorization "
            f"applies to it: {exc}"
        )
    print(
        f"Saved plan artifact {args.plan_file} verified (SHA-256 "
        f"{plan_file_sha256[:12]}...): it is a Terraform saved plan, {args.plan_json.name} "
        f"is its own `terraform show -json` rendering, and it targets "
        f"{verified_target['environment']}/{verified_target['workspace_name']} in "
        f"{verified_target['account_id']}/{verified_target['aws_region']} on the plan's own "
        f"evidence ({'; '.join(f'{k}: {v}' for k, v in sorted(target_evidence.items()))})."
    )

    # Evidence is emitted only after artifact, target and ownership verification.
    try:
        if args.inventory:
            inventory = build_inventory(
                plan,
                environment=args.environment,
                workspace_name=args.workspace_name,
                org_id=args.org_id,
                workspace_id=args.workspace_id,
            )
            args.inventory.write_text(
                json.dumps(inventory, indent=2, sort_keys=False) + "\n",
                encoding="utf-8",
            )
            print(
                f"Change inventory written to {args.inventory} "
                f"({inventory['total_changes']} change(s): "
                f"{inventory['counts_by_action']})."
            )

        if args.estimate:
            estimate = _estimate(
                plan,
                aws_region=args.aws_region,
                cluster_version=args.cluster_version or None,
            )
            args.estimate.write_text(
                json.dumps(estimate, indent=2, sort_keys=False) + "\n", encoding="utf-8"
            )
            print(
                f"Bounded estimate written to {args.estimate}: at most "
                f"${estimate['bounded_monthly_usd']:.2f}/month in fixed and "
                f"capacity-driven charges at the reviewed ceiling for {args.aws_region} "
                f"({len(estimate['not_bounded_by_this_estimate'])} usage-driven "
                f"component(s) explicitly not bounded)."
            )
    except (WorkspaceOwnershipError, OSError) as exc:
        return _fail(
            f"The plan's change inventory or bounded estimate could not be produced: {exc}. "
            f"Design item 4 requires both as evidence, so a plan whose cost cannot be "
            f"bounded is not applied."
        )

    print("Confirmed: every changed resource belongs to this workspace.")

    if args.expect_destroy and not report.has_destructive_changes:
        return _fail(
            "A destroy was requested, but this plan contains no deletions. That makes it not "
            "the destroy that was requested — a stale plan file, or one taken without "
            "`-destroy`. Refusing to apply it."
        )

    # Emitted before the destructive-change branching, so an operator can produce the document
    # for any plan the guard considers otherwise safe. Emitting is deliberately NOT approving:
    # this writes a file for a human to read and commit, and the apply still has to be run with
    # --authorize-destroy pointing at it.
    if args.emit_authorization:
        document = build_authorization(
            plan_sha256=plan_sha256,
            plan_file_sha256=plan_file_sha256,
            plan_file_name=args.plan_file.name,
            # Every target value comes from `verify_target`, so the document records what the PLAN
            # says it targets. Taking them from the flags is what made the wrong-region approval
            # possible: the emitted document and the later check both read the same caller input.
            account_id=verified_target["account_id"],
            aws_region=verified_target["aws_region"],
            environment=verified_target["environment"],
            workspace_name=verified_target["workspace_name"],
            org_id=verified_target["org_id"],
            workspace_id=verified_target["workspace_id"],
            target_evidence=target_evidence,
            destructive_actions=report.destructive_actions,
            destructive_identities={
                change["address"]: destructive_identity(change)
                for change in (plan.get("resource_changes") or [])
                if change.get("address") in report.destructive_actions
            },
        )
        try:
            args.emit_authorization.write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            return _fail(f"Could not write the authorization document: {exc}")
        print(
            f"Authorization for THIS plan written to {args.emit_authorization} "
            f"(plan JSON SHA-256 {plan_sha256[:12]}..., saved artifact SHA-256 "
            f"{plan_file_sha256[:12]}..., {len(document['destroy'])} address(es) with their "
            f"ordered actions and destroyed identities). Review it, commit it, and pass it as "
            f"--authorize-destroy together with the SAME --plan-file. Emitting an authorization "
            f"is not approving one."
        )

    # The review evidence. Addresses alone do not say WHICH cluster or WHOSE role, and the review
    # asked for the concrete destroyed identities and the ordered actions — so each line carries
    # the before side's real ids, ARNs, names and relationship targets. The actions are printed in
    # plan order, unsorted, because destroy-then-create and create-before-destroy are different
    # operations that sort identically.
    print(f"Plan contains {len(report.destructive)} destructive change(s):")
    identities = {
        change["address"]: destructive_identity(change)
        for change in (plan.get("resource_changes") or [])
        if change.get("address") in report.destructive_actions
    }
    for entry, address in zip(
        report.destructive_detail, report.destructive, strict=False
    ):
        print(f"  - {entry}")
        identity = identities.get(address) or {}
        for key, value in sorted(identity.items()):
            print(f"      {key}: {value}")

    if not args.authorize_destroy and not report.has_destructive_changes:
        print(
            "No destructive change in plan. Verification passed; use scripts/apply_workspace_plan.py with a reviewed authorization to apply."
        )
        return 0

    if not args.authorize_destroy:
        return _fail(
            f"This plan would destroy or replace {len(report.destructive)} resource(s), and "
            f"no --authorize-destroy file was supplied. Destruction in a tenant workspace "
            f"requires an authorization bound to THIS plan — its SHA-256, its target account, "
            f"region, environment and workspace, and each destroyed address with its exact "
            f"actions. Produce one with --emit-authorization, review it, then pass it back: a "
            f"plan run with the wrong -var-file is shaped exactly like a correct one, so "
            f"'approved to destroy' is not sufficient and neither is 'approved to destroy "
            f"THESE ADDRESSES' — two different plans can destroy the same addresses."
        )

    try:
        authorized, problems = _check_authorization(
            report.destructive_actions,
            args.authorize_destroy,
            plan_sha256=plan_sha256,
            plan_file_sha256=plan_file_sha256,
            verified_target=verified_target,
            destructive_identities=identities,
        )
    except WorkspaceOwnershipError as exc:
        return _fail(f"Destroy authorization could not be verified: {exc}")

    if not authorized:
        for problem in problems:
            print(problem)
        return _fail(
            "This plan is not the plan that was authorized. Nothing was applied."
        )

    print(
        f"Authorization is bound to this exact plan (JSON SHA-256 {plan_sha256[:12]}..., "
        f"artifact SHA-256 {plan_file_sha256[:12]}..., "
        f"{verified_target['environment']}/{verified_target['workspace_name']} in "
        f"{verified_target['account_id']}/{verified_target['aws_region']} as established from the "
        f"plan itself) and matches its destroyed set and ordered actions exactly "
        f"({len(report.destructive)} address(es)). Approved."
    )
    print(
        "Apply using scripts/apply_workspace_plan.py with this reviewed authorization, "
        "plan JSON, saved plan, target, module directory, inventory and estimate paths. "
        "It re-verifies and applies the same private artifact copy without replanning."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
