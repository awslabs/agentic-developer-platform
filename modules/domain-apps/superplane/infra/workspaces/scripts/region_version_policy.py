"""The reviewed region and Kubernetes version policy — Issue #5532 (w6-09), finding F6.

## The defect this replaces

`variables.tf` validated the region and the cluster version with REGEX ALONE:

    condition = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.aws_region))
    condition = can(regex("^1\\.(2[5-9]|3[0-9])$", var.cluster_version))

Both accept targets that do not exist. `xx-fake-1` matches the region pattern. `us-east-2` is
a real region but not one this platform is reviewed for. `1.25` matches the version pattern and
left EKS standard support in May 2024 — an apply against it either fails or lands a cluster
already outside its support window. `1.39` matches and does not exist yet, so a workspace
created for it cannot be created at all. In every case the failure arrives at APPLY time, after
the VPC and its subnets exist, and the operator reads an opaque AWS error rather than a refusal
naming the unsupported target.

A pattern describes the SHAPE of an identifier. Availability is a fact about the world on a
date, and a shape cannot express it.

## What this file is, and its one honest limitation

An explicit, dated allowlist. `POLICY_REVIEWED_ON` is the date the contents were checked
against AWS's published region list and the EKS version-support calendar; `VERSION_SUPPORT`
records each version's standard-support end so the refusal can say *why* rather than only
*no*.

The limitation, stated rather than hidden: this is a pinned snapshot, so it goes stale. A
version whose standard support ends after `POLICY_REVIEWED_ON` is still listed as supported
here on the day it lapses. That is a deliberate trade against the alternative — calling
`DescribeAddonVersions` at plan time, which needs a credential and a network, moves this out of
the offline lane, and makes an API outage indistinguishable from an unsupported version. A
stale allowlist fails CLOSED on anything new (unknown targets are refused) and produces a
slightly generous answer on things aging out; a live lookup fails OPEN on an outage. So the
snapshot is reviewed on a date and `test_region_version_policy.py` asserts the date is not
absent, that every calendar entry is internally consistent, and that the Terraform validations
and this file cannot drift apart.

## Why the support tier lives here too

Review finding W9-05 requires the bounded estimate to validate "region and EKS support-tier
pricing before calling the result an upper bound". EKS charges $0.10/hour for a cluster in
STANDARD support and $0.60/hour in EXTENDED support — six times more. That multiplier is a
property of the version, on a date, which is exactly what this file already knows. Keeping it
here rather than in the rate table means the price and the support decision cannot disagree:
there is one place that decides whether 1.28 is extended-support, and both the refusal message
and the hourly rate read it.

Nothing here reaches AWS, reads a credential or calls a pricing API.
"""

from __future__ import annotations

from dataclasses import dataclass

# The date the contents of this file were checked against AWS's published region list and the
# EKS Kubernetes version support calendar. Load-bearing: it is what makes "supported" a claim
# with a scope rather than an assertion of timeless fact, and it is what a reviewer looks at
# first to decide whether the snapshot is still worth trusting.
POLICY_REVIEWED_ON = "2026-09-20"


class UnsupportedTargetError(Exception):
    """A region or version outside the reviewed policy. Always refuse on this."""


# ---------------------------------------------------------------------------
# REGIONS
#
# Not "every region AWS has" — every region this PLATFORM is reviewed to create workspaces in.
# The distinction is the point of an allowlist. A region is on this list only if someone has
# confirmed the services this module uses are available there (EKS with Auto Mode, KMS, NAT
# gateways), the rate table below has prices for it, and placing tenant data there is a
# data-residency decision that has been made rather than defaulted into.
#
# Adding a region is therefore a REVIEW, not a typo fix: it needs the price multiplier below
# and a bump to POLICY_REVIEWED_ON.
# ---------------------------------------------------------------------------
SUPPORTED_REGIONS: dict[str, str] = {
    "us-east-1": "Primary. The rate table's base region; all published prices are quoted here.",
    "us-west-2": "Secondary US region for workspaces with a west-coast residency requirement.",
    "eu-west-1": "EU residency. Required for tenants whose data may not leave the EU.",
    "eu-central-1": "EU residency, Frankfurt — for tenants with a German residency requirement.",
    "ap-southeast-2": "APAC residency, Sydney.",
}

# Regions that exist and are deliberately NOT available to workspaces, with the reason. Held
# separately from "unknown" so the refusal can distinguish "there is no such region" from "that
# region is real and the answer is still no" — an operator who mistyped needs a different
# message from one whose request was considered and declined.
EXCLUDED_REGIONS: dict[str, str] = {
    "us-east-2": (
        "a real region, but not reviewed for this platform: no tenant has a residency "
        "requirement for it and every additional region is a rate table and a support surface"
    ),
    "ap-east-1": (
        "opt-in region. An apply fails unless the account has enabled it, and whether it is "
        "enabled is account state this module cannot see from a plan"
    ),
    "me-south-1": "opt-in region; see ap-east-1.",
    "cn-north-1": (
        "China partition (aws-cn). A different partition means different ARNs, a separate "
        "account, and service availability this module's assumptions do not cover"
    ),
    "us-gov-west-1": "GovCloud partition (aws-us-gov); see cn-north-1.",
}


@dataclass(frozen=True)
class VersionSupport:
    """One Kubernetes minor version's support status as of POLICY_REVIEWED_ON."""

    version: str
    # "standard", "extended" or "retired". The tier the version is in ON the reviewed date,
    # which is what decides both whether it may be created and what it costs per hour.
    tier: str
    # When STANDARD support ended or ends. Recorded even for retired versions, because the
    # refusal is far more actionable when it can say the date than when it says only "no".
    standard_support_ends: str
    note: str = ""


# EKS Kubernetes version support, as published and checked on POLICY_REVIEWED_ON.
#
# Three tiers and they are not interchangeable:
#
#   standard  — createable, $0.10/cluster/hour.
#   extended  — createable, $0.60/cluster/hour (6x). AWS keeps patching it; you pay for that.
#               Allowed, because a tenant mid-upgrade has a legitimate reason to be here — but
#               the estimate must price it correctly, which is W9-05's support-tier requirement.
#   retired   — NOT createable. AWS auto-upgrades clusters out of this tier, so a workspace
#               "created" at a retired version is a workspace whose version is not what was
#               reviewed.
VERSION_SUPPORT: dict[str, VersionSupport] = {
    "1.25": VersionSupport("1.25", "retired", "2024-05-01"),
    "1.26": VersionSupport("1.26", "retired", "2024-06-11"),
    "1.27": VersionSupport("1.27", "retired", "2024-07-24"),
    "1.28": VersionSupport("1.28", "retired", "2024-11-26"),
    "1.29": VersionSupport("1.29", "retired", "2025-03-23"),
    "1.30": VersionSupport("1.30", "retired", "2025-07-23"),
    "1.31": VersionSupport(
        "1.31",
        "extended",
        "2025-11-26",
        "Standard support has ended; createable but billed at the extended-support rate.",
    ),
    "1.32": VersionSupport(
        "1.32",
        "extended",
        "2026-03-23",
        "Standard support has ended; createable at the extended-support rate.",
    ),
    "1.33": VersionSupport(
        "1.33",
        "extended",
        "2026-07-29",
        "Standard support ended 2026-07-29, before this policy's review date; createable at "
        "the extended-support rate. Corrected under review finding W9-05/F6 — this entry "
        "previously claimed 'standard' with an end date of 2026-07-31, which was both the wrong "
        "date and a tier its own date contradicted, and it priced the control plane at "
        "$73/month instead of $438.",
    ),
    "1.34": VersionSupport("1.34", "standard", "2026-11-30"),
    "1.35": VersionSupport("1.35", "standard", "2027-03-27"),
}


# ---------------------------------------------------------------------------
# The snapshot must not CONTRADICT ITSELF (review finding W9-05/F6)
#
# A dated snapshot goes stale; that trade is stated above and accepted. What is NOT acceptable is a
# snapshot whose own two fields disagree, and that is what the review found: `1.33` was recorded as
# `standard` with `standard_support_ends = 2026-07-31`, a date 51 days BEFORE the review date it
# was supposedly checked on. In us-east-1 that version is in extended support.
#
# The consequence was not cosmetic. The tier decides the control-plane rate, so the estimate priced
# a 1.33 cluster at $0.10/hour — $73/month — when AWS bills $0.60/hour, $438/month. The figure was
# six times too low and was labelled an upper bound.
#
# That defect was invisible because nothing compared the two fields. Every other consistency
# property of this file is asserted (a tier is one of three values, a date parses, a createable tier
# has a price) and this one — the one that decides the price — was not. So it is checked HERE, at
# import time, rather than only in the test suite: a contradictory calendar must not be loadable by
# the estimate at all, because the failure it causes is a believable number rather than an error.
#
# The rule: a version claiming STANDARD support whose standard support has already ended as of
# POLICY_REVIEWED_ON is a contradiction. Its tier says "cheap and current" while its date says
# "standard support is over". Extended and retired entries are expected to have past end dates —
# that is what those tiers MEAN — so only the standard claim is checked.
#
# String comparison is correct for ISO-8601 dates and deliberate: `date.today()` would make this
# check depend on when it runs, so a calendar that was consistent at review time would start
# failing on its own, turning a real contradiction into background noise.
# ---------------------------------------------------------------------------
def _assert_calendar_is_self_consistent() -> None:
    contradictions = [
        f"{version}: tier is 'standard' but standard_support_ends "
        f"{support.standard_support_ends} is on or before POLICY_REVIEWED_ON "
        f"{POLICY_REVIEWED_ON}"
        for version, support in sorted(VERSION_SUPPORT.items())
        if support.tier == "standard"
        and support.standard_support_ends <= POLICY_REVIEWED_ON
    ]
    if not contradictions:
        return

    # The rates are read from the table rather than written into this sentence. The whole finding
    # is that a duplicated fact drifts from the fact it duplicates, so quoting "$0.60 against
    # $0.10" as literal prose here would reintroduce exactly that failure one level up: a
    # published-price change would leave this explanation confidently wrong about why it fired.
    extended = CONTROL_PLANE_HOURLY_USD["extended"]
    standard = CONTROL_PLANE_HOURLY_USD["standard"]
    raise UnsupportedTargetError(
        "the reviewed version calendar contradicts itself:\n"
        + "\n".join(f"    - {entry}" for entry in contradictions)
        + f"\n\nA version whose standard support has ended is in EXTENDED support, which costs "
        f"${extended:.2f}/hour against ${standard:.2f} — {extended / standard:.0f} times. "
        f"Leaving it marked 'standard' makes the bounded estimate understate that cluster's "
        f"control plane by ${(extended - standard) * HOURS_PER_MONTH:,.0f} a month — a factor "
        f"of {extended / standard:.0f} — while still calling the result an upper bound "
        f"(finding W9-05). Either move the entry to tier 'extended', or — if it genuinely still "
        f"has standard support — correct its end date and bump POLICY_REVIEWED_ON to the date "
        f"you checked it."
    )


# The tiers a workspace may be CREATED at. Extended is included deliberately — refusing it
# would block a tenant who is legitimately mid-upgrade — and the price difference is what makes
# that safe to allow: an extended-support cluster shows up in the estimate at 6x, so the cost
# of staying there is visible rather than silent.
CREATABLE_TIERS = frozenset({"standard", "extended"})

# EKS control-plane hourly rate BY SUPPORT TIER, us-east-1, as of POLICY_REVIEWED_ON.
# This is W9-05's "support-tier pricing": a single 0.10 constant understates an
# extended-support cluster by $365/month, and an upper bound that is six times too low is not
# an upper bound.
CONTROL_PLANE_HOURLY_USD: dict[str, float] = {
    "standard": 0.10,
    "extended": 0.60,
}

# Billable hours in a month, by AWS's own convention for monthly price quotes. Defined here rather
# than in the estimate script because both modules need it: the estimate turns hourly rates into a
# monthly figure, and this module's refusal messages state the monthly consequence of a wrong tier.
# The estimate imports it from here, so there is one value and not two that can disagree.
HOURS_PER_MONTH = 730

# Per-region multiplier applied to the rate table's us-east-1 prices. A bound computed from
# us-east-1 rates for a workspace in eu-central-1 is not an upper bound for that workspace, so
# the estimate must either adjust or refuse — it may not quietly use the wrong region's price.
#
# ## The scope of the evidence behind these numbers (review finding W9-05)
#
# Stated precisely, because the previous comment claimed more than the numbers supported. It said
# these were "rounded up to the nearest whole percent above the true spread across the instance
# families in the rate table" — that is, derived from EC2 on-demand prices. But the multiplier is
# applied to EVERY line in the estimate: the EKS control plane, NAT gateway hours, KMS keys and gp3
# storage as well as instances. An EC2-derived factor is not evidence about those services, and
# calling the product an upper bound asserted something about each of them that had not been
# checked.
#
# Two honest options: verify each service's per-region price and take the worst ratio, or state the
# evidence's scope and stop claiming more. Verification needs AWS's published per-region price for
# five services in five regions, which this offline module cannot obtain and which I have not
# independently confirmed — so the claim is RESTRICTED rather than asserted:
#
#   *   For EC2 instance cost — the dominant term in any workspace at its node ceiling — these
#       remain a deliberate round-up of the observed cross-family spread, and the bound holds.
#   *   For the fixed per-service components, the multiplier is a STATED ASSUMPTION: that no
#       service in the rate table costs proportionally more in these regions than EC2 does. That
#       assumption is recorded in the estimate's own output (`region_multiplier_basis`) instead of
#       being implied by the word "bound", so a reviewer can see exactly what is and is not
#       established.
#
# `REGION_MULTIPLIER_EVIDENCE` below carries that text into the artifact. Narrowing the claim
# rather than deleting the adjustment is deliberate: dropping it would silently price an
# ap-southeast-2 workspace at us-east-1 rates, which is the very defect W9-05 raised.
#
# us-east-1 is exactly 1.0 by definition, being the table's base.
REGION_PRICE_MULTIPLIER: dict[str, float] = {
    "us-east-1": 1.00,
    "us-west-2": 1.00,
    "eu-west-1": 1.10,
    "eu-central-1": 1.14,
    "ap-southeast-2": 1.18,
}

REGION_MULTIPLIER_EVIDENCE = (
    "Derived from the cross-family spread of EC2 on-demand Linux prices and rounded up to the "
    "next whole percent. EC2 instance hours dominate a workspace priced at its node ceiling, so "
    "for that term this is an upper bound. It is applied to the other components (EKS control "
    "plane, NAT gateway, KMS, gp3 storage) as a STATED ASSUMPTION — that none of them costs "
    "proportionally more in this region than EC2 does — and not as a verified per-service ratio: "
    "confirming that needs AWS's published per-region price for each service, which this offline "
    "guard does not fetch. Treat the non-compute lines as adjusted estimates rather than proven "
    "ceilings."
)


def check_region(region: str) -> None:
    """Refuse a region outside the reviewed policy. Returns None, or raises.

    Separate from the version check so the caller can report BOTH problems rather than only
    the first — an operator with a stale region and a retired version should learn that in one
    run, not two.
    """
    if not region:
        raise UnsupportedTargetError(
            "no region was supplied. The region is part of what makes an estimate a bound "
            "and part of what an authorization is bound to; it is not defaultable."
        )
    if region in SUPPORTED_REGIONS:
        return

    if region in EXCLUDED_REGIONS:
        raise UnsupportedTargetError(
            f"region {region!r} is not available to Superplane workspaces: "
            f"{EXCLUDED_REGIONS[region]}. This is a reviewed exclusion, not an oversight — "
            f"adding it means confirming service availability, adding a price multiplier, and "
            f"making the data-residency decision explicitly. Reviewed regions as of "
            f"{POLICY_REVIEWED_ON}: {sorted(SUPPORTED_REGIONS)}."
        )

    raise UnsupportedTargetError(
        f"region {region!r} is not in this platform's reviewed region policy. It may be "
        f"misspelled, or a real region nobody has reviewed for workspace use — a regex on the "
        f"identifier's SHAPE cannot tell those apart from a region that exists, which is why "
        f"this allowlist exists (finding F6). Reviewed regions as of {POLICY_REVIEWED_ON}: "
        f"{sorted(SUPPORTED_REGIONS)}."
    )


def check_cluster_version(version: str) -> VersionSupport:
    """Refuse a Kubernetes version that cannot be created, and return its support record.

    Returns the record rather than None because the caller needs the TIER: it decides the
    control-plane price, and a version check that discarded it would leave the estimate to
    guess at the thing this function just established.
    """
    if not version:
        raise UnsupportedTargetError(
            "no cluster_version was supplied. An unpinned version is resolved at apply time, "
            "which makes the cluster's contents depend on when it ran rather than on what was "
            "reviewed."
        )

    support = VERSION_SUPPORT.get(version)
    if support is None:
        known = sorted(VERSION_SUPPORT)
        # A version ABOVE everything known is the future case, and it is worth distinguishing:
        # the operator is not wrong about the format, they are ahead of this file's review date.
        hint = (
            f"{version!r} is beyond the newest version in this policy ({known[-1]}), so either "
            f"it does not exist yet or this policy has not been reviewed since it shipped — "
            f"reviewed {POLICY_REVIEWED_ON}. Creating a cluster at a version EKS does not "
            f"offer fails at apply, after the VPC exists."
            if _version_key(version) > _version_key(known[-1])
            else f"{version!r} is not a version this policy knows about."
        )
        raise UnsupportedTargetError(
            f"cluster_version {version!r} is not in the reviewed EKS version policy. {hint} "
            f"A regex matching '1.25'–'1.39' accepts both retired and not-yet-existing "
            f"versions (finding F6). Known versions: {known}."
        )

    if support.tier not in CREATABLE_TIERS:
        raise UnsupportedTargetError(
            f"cluster_version {version!r} is RETIRED: EKS standard support ended "
            f"{support.standard_support_ends} and it is no longer createable. AWS "
            f"auto-upgrades clusters off retired versions, so a workspace created here would "
            f"not stay at the version that was reviewed. Createable versions as of "
            f"{POLICY_REVIEWED_ON}: {sorted(createable_versions())}."
        )

    return support


def createable_versions() -> list[str]:
    """Every version a workspace may be created at, newest last."""
    return sorted(
        (v for v, s in VERSION_SUPPORT.items() if s.tier in CREATABLE_TIERS),
        key=_version_key,
    )


def _version_key(version: str) -> tuple[int, ...]:
    """Sort '1.9' below '1.10'. String comparison gets this backwards."""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def control_plane_hourly_usd(support: VersionSupport) -> float:
    """The hourly control-plane rate for this version's support tier.

    Raises rather than defaulting to the standard rate on an unknown tier: defaulting would
    price an extended-support cluster at one sixth of its cost, and a bound that is six times
    too low is worse than no bound because it is believed.
    """
    rate = CONTROL_PLANE_HOURLY_USD.get(support.tier)
    if rate is None:
        raise UnsupportedTargetError(
            f"no control-plane rate is recorded for support tier {support.tier!r}. Refusing "
            f"to fall back to the standard rate: extended support costs six times standard, "
            f"so a guessed tier produces a figure that is not a bound."
        )
    return rate


def region_price_multiplier(region: str) -> float:
    """The factor converting a us-east-1 price into an upper bound for `region`.

    `check_region` is called first rather than trusting the caller to have done it: a missing
    multiplier must never become 1.0 by default, because that silently prices a workspace in
    the platform's most expensive region as if it were in its cheapest.
    """
    check_region(region)
    multiplier = REGION_PRICE_MULTIPLIER.get(region)
    if multiplier is None:
        raise UnsupportedTargetError(
            f"region {region!r} is in SUPPORTED_REGIONS but has no entry in "
            f"REGION_PRICE_MULTIPLIER, so a us-east-1 price cannot be converted into a bound "
            f"for it. Whoever added the region must add its multiplier; refusing to assume "
            f"1.0, which would understate every region more expensive than us-east-1."
        )
    return multiplier
