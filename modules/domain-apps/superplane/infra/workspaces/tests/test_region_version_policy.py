"""The region and version support policy is real, dated, and singular — #5532 (w6-09), F6.

## What finding F6 was

`variables.tf` validated the region and cluster version with REGEX ALONE. A pattern describes
the SHAPE of an identifier; availability is a fact about the world on a date. So the module
accepted, and failed at apply on:

*   `xx-fake-1` — matches `^[a-z]{2}(-[a-z]+)+-[0-9]$` and is not a region.
*   `us-east-2` — entirely real, and not a region this platform has reviewed.
*   `1.25` — matches `^1\\.(2[5-9]|3[0-9])$` and left EKS standard support in May 2024.
*   `1.39` — matches, and does not exist.

Every one of those surfaced as an opaque AWS error AFTER the VPC and subnets were built.

## The hazard this file exists to police

The policy now lives in two places, and it has to: `scripts/region_version_policy.py` holds the
reviewed lists, the support calendar and the prices, but a Terraform `validation` block cannot
call Python — and variables.tf is the only place that refuses a bad target *before any resource
is created*, which is what F6 asks for.

Duplication that nobody compares is how two policies come to disagree, so the tests below parse
the literals out of `variables.tf` and assert they equal the Python policy's. Adding a region to
one file and not the other fails here rather than at apply.

## Measured against the reviewed head

Most of this file tests code that did not exist at `2f75e700`, so "it fails pre-repair" is
trivially true for those. The one test that is a genuine regression control over the OLD source
is `test_the_terraform_validation_enforces_the_same_lists_it_declares`: restoring the regex-only
validations into a copy of the module and running this file gives 18 passed, 1 failed, and the
failure names BOTH the region and the version list as unenforced. The Terraform half's
reproductions (seven of twelve runs) are recorded in `region_version_policy.tftest.hcl`.

## Why this is offline and what it therefore cannot prove

Nothing here calls AWS. That is the point — F6 asks that retired, future and unavailable
combinations be exercised OFFLINE. The limitation is honest and stated in the policy module
itself: the allowlist is a dated snapshot, so a version whose support lapses after
`POLICY_REVIEWED_ON` is still listed as supported on the day it lapses. These tests assert the
date exists and the calendar is internally consistent; they cannot assert the snapshot matches
what AWS published this morning. The trade is deliberate — a live lookup needs a credential and
fails OPEN on an outage, where a stale allowlist fails CLOSED on anything it does not know.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

WORKSPACES = Path(__file__).resolve().parents[1]
VARIABLES_TF = WORKSPACES / "variables.tf"

# The rate table's base region, named here so the multiplier control below cannot drift from it.
RATE_TABLE_BASE = "us-east-1"

sys.path.insert(0, str(WORKSPACES / "scripts"))

from region_version_policy import (  # noqa: E402
    CONTROL_PLANE_HOURLY_USD,
    CREATABLE_TIERS,
    EXCLUDED_REGIONS,
    HOURS_PER_MONTH,
    POLICY_REVIEWED_ON,
    REGION_MULTIPLIER_EVIDENCE,
    REGION_PRICE_MULTIPLIER,
    SUPPORTED_REGIONS,
    VERSION_SUPPORT,
    UnsupportedTargetError,
    VersionSupport,
    _assert_calendar_is_self_consistent,
    check_cluster_version,
    check_region,
    control_plane_hourly_usd,
    createable_versions,
    region_price_multiplier,
)


# ---------------------------------------------------------------------------
# The policy is dated and internally consistent
# ---------------------------------------------------------------------------
def test_the_policy_records_when_it_was_reviewed() -> None:
    """A support claim with no date is an assertion of timeless fact, which this cannot be.

    The date is what tells a reader whether the snapshot is still worth trusting, and it is
    quoted in every refusal message so an operator who disagrees knows what to re-check.
    """
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", POLICY_REVIEWED_ON), (
        f"POLICY_REVIEWED_ON is {POLICY_REVIEWED_ON!r}, which is not an ISO date. Every "
        f"refusal quotes it; 'supported as of <no date>' is not a reviewable claim."
    )


def test_every_version_has_a_known_tier_and_a_support_date() -> None:
    """A tier outside the three known values would decide creatability AND price by accident."""
    for version, support in VERSION_SUPPORT.items():
        assert support.version == version, (
            f"VERSION_SUPPORT key {version!r} disagrees with its record's version "
            f"{support.version!r} — one of them is what a lookup will return and the other is "
            f"what a reader believes."
        )
        assert support.tier in {"standard", "extended", "retired"}, (
            f"{version} has tier {support.tier!r}. An unrecognised tier is not refused by "
            f"CREATABLE_TIERS membership and has no price, so it would fail in two places for "
            f"reasons neither names."
        )
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", support.standard_support_ends), (
            f"{version} has standard_support_ends={support.standard_support_ends!r}. The "
            f"retirement refusal quotes this date; without it the message is just 'no'."
        )


def test_every_createable_tier_has_a_price() -> None:
    """A tier a workspace may be created at, with no rate, prices that workspace at nothing.

    This is the check that keeps W9-05's support-tier pricing and F6's support policy from
    drifting: they read the same two dicts, and a tier in one and not the other is the shape
    where an allowed version silently costs $0.00.
    """
    for tier in sorted(CREATABLE_TIERS):
        assert tier in CONTROL_PLANE_HOURLY_USD, (
            f"tier {tier!r} is createable but has no entry in CONTROL_PLANE_HOURLY_USD, so a "
            f"cluster on it cannot be priced."
        )
        assert CONTROL_PLANE_HOURLY_USD[tier] > 0, (
            f"tier {tier!r} is priced at {CONTROL_PLANE_HOURLY_USD[tier]}. A free control "
            f"plane is not a thing AWS offers, and a zero here understates every bound."
        )


def test_extended_support_costs_more_than_standard() -> None:
    """The whole reason the tier is priced separately.

    If these were equal, every mechanism in this file would still work and the estimate would
    understate an extended-support cluster by six times — the W9-05 defect, reintroduced by a
    table edit rather than a code change.
    """
    assert (
        CONTROL_PLANE_HOURLY_USD["extended"] > CONTROL_PLANE_HOURLY_USD["standard"]
    ), (
        "extended support must cost more than standard; AWS bills $0.60/hour against $0.10. "
        "Equal rates make the tier distinction free to get wrong."
    )


def test_every_supported_region_can_be_priced() -> None:
    """A region on the allowlist with no multiplier cannot be given a bound.

    `region_price_multiplier` refuses rather than defaulting to 1.0 — which would price the
    platform's most expensive region as if it were its cheapest — so a region added without a
    multiplier breaks the estimate. Better to fail here.
    """
    for region in sorted(SUPPORTED_REGIONS):
        assert region in REGION_PRICE_MULTIPLIER, (
            f"{region} is supported but has no price multiplier, so no bound can be computed "
            f"for a workspace in it. Whoever added the region must add the multiplier."
        )
        assert REGION_PRICE_MULTIPLIER[region] >= 1.0, (
            f"{region}'s multiplier is {REGION_PRICE_MULTIPLIER[region]}, below the "
            f"us-east-1 base. These feed an UPPER bound and are deliberately rounded UP; a "
            f"multiplier under 1.0 means a cheaper region, which must be proven rather than "
            f"assumed or the result stops being a bound."
        )
    assert REGION_PRICE_MULTIPLIER["us-east-1"] == 1.0, (
        "us-east-1 is the rate table's base region, so its multiplier is 1.0 by definition."
    )


def test_a_region_is_not_both_supported_and_excluded() -> None:
    """The two lists answer the same question, so an overlap makes the answer order-dependent."""
    overlap = sorted(set(SUPPORTED_REGIONS) & set(EXCLUDED_REGIONS))
    assert not overlap, (
        f"{overlap} appear in both SUPPORTED_REGIONS and EXCLUDED_REGIONS. `check_region` "
        f"tests supported first, so the exclusion would silently never apply."
    )


# ---------------------------------------------------------------------------
# The offline exercises finding F6 asks for: retired, future, unavailable
# ---------------------------------------------------------------------------
def test_a_retired_version_is_refused_with_its_retirement_date() -> None:
    """F6's "retired" case. The pattern this replaced accepted 1.25, retired in May 2024.

    AWS auto-upgrades clusters off retired versions, so a workspace "created" at one is a
    workspace whose version is not the reviewed one — and the refusal must say WHEN support
    ended, because "unsupported" without a date gives the operator nothing to plan against.
    """
    with pytest.raises(UnsupportedTargetError) as caught:
        check_cluster_version("1.25")
    message = str(caught.value)
    assert "RETIRED" in message
    assert VERSION_SUPPORT["1.25"].standard_support_ends in message, (
        f"the refusal must name the retirement date. Got: {message}"
    )


def test_a_future_version_is_refused_and_says_so_distinctly() -> None:
    """F6's "future" case, and it needs a DIFFERENT message from the retired one.

    `1.99` is not a mistake about the format — the operator is ahead of this file's review date,
    or the version does not exist. Telling them "retired" would send them to look at a
    retirement calendar for a version that was never released.
    """
    with pytest.raises(UnsupportedTargetError) as caught:
        check_cluster_version("1.99")
    message = str(caught.value)
    assert "beyond the newest version in this policy" in message, (
        f"a future version must be distinguished from a retired one. Got: {message}"
    )
    assert POLICY_REVIEWED_ON in message, (
        "the refusal must give the review date, because 'not offered yet' is a claim that "
        "expires and the operator needs to know how stale it is."
    )


def test_the_version_ordering_is_numeric_not_lexicographic() -> None:
    """`"1.9" > "1.10"` as strings, which would call a current version "beyond the newest".

    Asserted directly because the future-version message depends on this comparison, and a
    lexicographic sort would produce a confidently wrong refusal rather than an error.
    """
    versions = createable_versions()
    assert versions == sorted(versions, key=lambda v: [int(p) for p in v.split(".")]), (
        f"createable_versions() is not in numeric order: {versions}. String ordering places "
        f"1.9 above 1.10 and would misclassify versions as future."
    )


def test_an_unreviewed_but_real_region_is_refused_as_a_decision() -> None:
    """F6's "unavailable" case, and the distinction that makes the refusal useful.

    `us-east-2` exists. Someone who asked for it did not mistype — they asked for something the
    platform has considered and declined, and the message must say which, or they will
    reasonably assume a bug and retry.
    """
    with pytest.raises(UnsupportedTargetError) as caught:
        check_region("us-east-2")
    message = str(caught.value)
    assert "reviewed exclusion, not an oversight" in message, (
        f"a real-but-excluded region must be distinguished from a typo. Got: {message}"
    )
    assert EXCLUDED_REGIONS["us-east-2"].split(":")[0] in message


def test_a_nonexistent_region_that_matches_the_regex_is_refused() -> None:
    """The exact value the pattern accepted. `xx-fake-1` matches every shape rule for a region."""
    with pytest.raises(UnsupportedTargetError) as caught:
        check_region("xx-fake-1")
    assert "reviewed region policy" in str(caught.value)


def test_an_opt_in_region_is_refused_for_a_reason_a_plan_cannot_check() -> None:
    """Opt-in regions fail on ACCOUNT STATE, which no plan document can see.

    The strongest case for an allowlist over a pattern: whether ap-east-1 is enabled is a
    property of the account, not of the identifier, so no amount of validating the string can
    establish it. Refusing the region is the only offline answer.
    """
    with pytest.raises(UnsupportedTargetError) as caught:
        check_region("ap-east-1")
    assert "opt-in" in str(caught.value).lower()


def test_another_partition_is_refused() -> None:
    """aws-cn and aws-us-gov change every ARN this module builds."""
    for region in ("cn-north-1", "us-gov-west-1"):
        with pytest.raises(UnsupportedTargetError):
            check_region(region)


def test_an_empty_region_or_version_is_refused_rather_than_skipped() -> None:
    """Absent must not read as "no constraint" — the fail-open shape for every check here."""
    with pytest.raises(UnsupportedTargetError):
        check_region("")
    with pytest.raises(UnsupportedTargetError):
        check_cluster_version("")


def test_every_reviewed_region_and_createable_version_is_accepted() -> None:
    """The positive half. A policy that refused everything would pass every test above.

    This is the anti-vacuous control for the whole file: `check_region` and
    `check_cluster_version` must ACCEPT the targets the platform supports, or the module cannot
    be deployed anywhere and the policy gets deleted rather than corrected.
    """
    assert SUPPORTED_REGIONS, "an empty region allowlist makes the module undeployable"
    assert createable_versions(), (
        "no createable version means no workspace can be created"
    )

    for region in SUPPORTED_REGIONS:
        check_region(region)  # must not raise
        assert region_price_multiplier(region) > 0

    for version in createable_versions():
        support = check_cluster_version(version)
        assert control_plane_hourly_usd(support) > 0


def test_extended_support_versions_are_createable_not_refused() -> None:
    """Deliberate, and worth pinning: a tenant mid-upgrade has a legitimate reason to be here.

    Refusing extended support would block a real migration. What makes allowing it safe is the
    6x price showing up in the estimate, so the cost of staying is visible rather than silent —
    which is why this test and `test_extended_support_costs_more_than_standard` belong together.
    """
    extended = [v for v, s in VERSION_SUPPORT.items() if s.tier == "extended"]
    assert extended, (
        "no version is in extended support, so this test proves nothing. If the calendar has "
        "moved on, re-point it at whatever is now in extended support rather than deleting it."
    )
    for version in extended:
        support = check_cluster_version(version)  # must not raise
        assert control_plane_hourly_usd(support) == CONTROL_PLANE_HOURLY_USD["extended"]


# ---------------------------------------------------------------------------
# The Terraform half cannot drift from the Python half
# ---------------------------------------------------------------------------
def _hcl_list(name: str) -> list[str]:
    """Extract a `name = [ "a", "b" ]` list literal from variables.tf.

    Parsed from the SOURCE rather than duplicated here, because a hardcoded expected list in a
    test is the assumption the test is supposed to be checking.
    """
    source = VARIABLES_TF.read_text(encoding="utf-8")
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*\[(.*?)\]", source, re.S | re.M)
    assert match, (
        f"could not find a `{name} = [...]` list in variables.tf. If it was renamed, this "
        f"cross-check stops comparing anything — update the name here rather than deleting the "
        f"test, or the two halves of the policy can drift silently."
    )
    return re.findall(r'"([^"]+)"', match.group(1))


def test_the_terraform_region_allowlist_equals_the_python_one() -> None:
    """One policy, two files, because a validation block cannot call Python.

    variables.tf is the only place that refuses a bad target before any resource exists, which
    is what F6 asks for; region_version_policy.py is where the reasoning and the prices live. A
    region added to one and not the other is accepted by the apply and unpriceable by the
    estimate, or vice versa.
    """
    assert set(_hcl_list("supported_regions")) == set(SUPPORTED_REGIONS), (
        f"variables.tf's local.supported_regions and region_version_policy.py's "
        f"SUPPORTED_REGIONS disagree.\n"
        f"  only in variables.tf: {sorted(set(_hcl_list('supported_regions')) - set(SUPPORTED_REGIONS))}\n"
        f"  only in Python:       {sorted(set(SUPPORTED_REGIONS) - set(_hcl_list('supported_regions')))}"
    )


def test_the_terraform_version_allowlist_equals_the_createable_set() -> None:
    """Same cross-check for versions, against the DERIVED createable set.

    Compared to `createable_versions()` rather than to a second literal, so the calendar stays
    the single source of truth: retiring a version in the Python policy must force a
    variables.tf edit, and this is what makes it.
    """
    assert set(_hcl_list("createable_cluster_versions")) == set(
        createable_versions()
    ), (
        f"variables.tf's local.createable_cluster_versions does not match the versions the "
        f"support calendar says are createable.\n"
        f"  variables.tf: {sorted(_hcl_list('createable_cluster_versions'))}\n"
        f"  calendar:     {createable_versions()}\n"
        f"A version retired in the calendar but still listed in variables.tf is one Terraform "
        f"will accept and EKS will refuse, at apply."
    )


def test_the_terraform_validation_enforces_the_same_lists_it_declares() -> None:
    """The locals are documentation; the `contains(...)` calls are the enforcement.

    A validation block cannot reference a local (Terraform forbids it), so the lists appear
    twice inside variables.tf itself — once as a readable local and once inline in the
    condition. This asserts the inline copies are the enforcing ones and that they match, which
    is the drift that would leave a correct-looking local next to a validation accepting
    something else.
    """
    source = VARIABLES_TF.read_text(encoding="utf-8")
    # Every `contains([...])` list in the file, whatever it validates.
    enforced = [
        set(re.findall(r'"([^"]+)"', body))
        for body in re.findall(r"contains\(\s*\[(.*?)\]", source, re.S)
    ]

    # Both labels are collected before asserting. Failing on the first would hide that the
    # OTHER one is also unenforced — and when this was measured against the reviewed head,
    # both were.
    missing = [
        (label, expected)
        for label, expected in (
            ("region", set(SUPPORTED_REGIONS)),
            ("version", set(createable_versions())),
        )
        if expected not in enforced
    ]

    assert not missing, (
        "no `contains([...])` validation in variables.tf enforces the reviewed "
        + " or ".join(label for label, _ in missing)
        + " list. The local declaring it is a comment unless a validation checks it — and F6 "
        "is specifically that the check must happen before any resource is created.\n"
        + "".join(f"  expected {label}: {sorted(v)}\n" for label, v in missing)
        + f"  contains() lists found: {[sorted(item) for item in enforced]}"
    )


# ===========================================================================
# W9-05 / F6: the support data itself was WRONG, and nothing compared its own fields
# ===========================================================================
# The review found the snapshot stating a fact contradicted by its own other field: `1.33` was
# recorded as tier `standard` with `standard_support_ends = 2026-07-31` — a date BEFORE the
# `POLICY_REVIEWED_ON` it was supposedly checked on. In us-east-1, EKS 1.33 left standard support
# on 2026-07-29 and is in extended support.
#
# This was not a documentation slip. The tier decides the control-plane rate, so `_estimate` priced
# a 1.33 cluster at $0.10/hour — $73/month — against AWS's actual $0.60/hour, $438/month. The
# estimate was six times too low and labelled an upper bound, which is exactly what W9-05 is about.
#
# Every OTHER internal-consistency property of the calendar was already asserted above (a tier is
# one of three known values, a date parses, a createable tier has a price). The one property that
# decides the price — that a tier and its own date agree — was not, which is why a wrong entry sat
# there through a review. The controls below fix that in both directions: the corrected values are
# pinned, and the contradiction is made unrepresentable rather than merely absent.
# ---------------------------------------------------------------------------
def test_a_standard_tier_claim_contradicted_by_its_own_date_is_rejected() -> None:
    """The guard, driven with the EXACT entry the reviewed head shipped.

    This is the regression control: it reconstructs `1.33 / standard / 2026-07-31` and requires the
    consistency check to refuse it. Without the repair this calendar imports cleanly and prices a
    1.33 control plane at one sixth of its cost.

    Driven through the real function against a patched table rather than by asserting on the
    shipped values alone, because the shipped values could be corrected while the RULE stayed
    absent — and then the next version to age out of standard support reintroduces the defect
    silently.
    """
    import region_version_policy as policy

    original = dict(policy.VERSION_SUPPORT)
    try:
        policy.VERSION_SUPPORT["1.33"] = VersionSupport(
            "1.33", "standard", "2026-07-31"
        )
        with pytest.raises(UnsupportedTargetError) as refusal:
            _assert_calendar_is_self_consistent()
    finally:
        policy.VERSION_SUPPORT.clear()
        policy.VERSION_SUPPORT.update(original)

    message = str(refusal.value)
    assert "1.33" in message and "2026-07-31" in message, (
        f"the refusal must name the offending version AND its date, or a maintainer with eleven "
        f"calendar entries cannot tell which one is wrong.\n{message}"
    )
    ratio = CONTROL_PLANE_HOURLY_USD["extended"] / CONTROL_PLANE_HOURLY_USD["standard"]
    shortfall = (
        CONTROL_PLANE_HOURLY_USD["extended"] - CONTROL_PLANE_HOURLY_USD["standard"]
    ) * HOURS_PER_MONTH
    assert f"{ratio:.0f} times" in message and f"${shortfall:,.0f}" in message, (
        f"the refusal must say what the contradiction COSTS. 'Inconsistent tier' reads as "
        f"pedantry; a {ratio:.0f}-fold understated bound — ${shortfall:,.0f} a month on the "
        f"control plane alone — is the actual consequence and is what makes fixing it urgent "
        f"rather than optional.\n{message}"
    )


def test_the_contradiction_refusal_derives_its_figures_from_the_rate_table() -> None:
    """The refusal must READ the rates, not quote them — checked by changing them.

    This whole finding is one duplicated fact drifting from the fact it duplicated. Writing
    "$0.60 against $0.10" as literal prose inside the message that explains the drift would repeat
    the defect one level up: a published-price change would leave the explanation confidently wrong
    about why it fired, in the exact file whose job is to be the single source of those rates.

    Verified by driving the function against a patched rate table rather than by scanning its
    source for the string "0.60" — source scanning cannot tell a hardcoded rate from a comment
    explaining why not to hardcode it, and a control that fires on its own documentation gets
    deleted rather than fixed.
    """
    import region_version_policy as policy

    original_support = dict(policy.VERSION_SUPPORT)
    original_rates = dict(policy.CONTROL_PLANE_HOURLY_USD)
    try:
        policy.VERSION_SUPPORT["1.33"] = VersionSupport(
            "1.33", "standard", "2026-07-31"
        )
        # Deliberately not the real ratio: 0.50/0.02 is 25x, so any figure copied from today's
        # published prices rather than read from the table shows up as a mismatch.
        policy.CONTROL_PLANE_HOURLY_USD.update({"standard": 0.02, "extended": 0.50})
        with pytest.raises(UnsupportedTargetError) as refusal:
            _assert_calendar_is_self_consistent()
    finally:
        policy.VERSION_SUPPORT.clear()
        policy.VERSION_SUPPORT.update(original_support)
        policy.CONTROL_PLANE_HOURLY_USD.clear()
        policy.CONTROL_PLANE_HOURLY_USD.update(original_rates)

    message = str(refusal.value)
    assert "25 times" in message and "$0.50" in message and "$0.02" in message, (
        f"with the table patched to $0.02 standard and $0.50 extended, the refusal must say 25 "
        f"times and quote those rates. Naming the shipped prices instead means they are written "
        f"into the message as prose and will drift from the table.\n{message}"
    )
    assert "0.60" not in message and "0.10" not in message, (
        f"the refusal still contains a real published rate that the patched table does not "
        f"hold, so at least one figure is hardcoded.\n{message}"
    )


def test_the_shipped_calendar_is_self_consistent() -> None:
    """The rule applied to the real table — the assertion the reviewed head would fail.

    Called directly rather than relying on the import-time invocation: a future refactor that
    dropped the call at module scope would leave this file's other tests passing, since they never
    exercise the contradiction.
    """
    _assert_calendar_is_self_consistent()


def test_every_version_past_its_standard_support_is_priced_as_extended() -> None:
    """The pricing consequence, asserted independently of the tier field's spelling.

    The consistency guard checks that a `standard` claim is not contradicted by its date. This
    checks the thing that MATTERS: that every version whose standard support has ended is actually
    billed at the extended rate. The two are separable — a version could be mislabelled `retired`
    and escape the first check while still being createable at the wrong price.
    """
    for version, support in sorted(VERSION_SUPPORT.items()):
        if support.tier not in CREATABLE_TIERS:
            continue
        if support.standard_support_ends > POLICY_REVIEWED_ON:
            continue
        rate = control_plane_hourly_usd(support)
        assert rate == CONTROL_PLANE_HOURLY_USD["extended"], (
            f"{version}'s standard support ended {support.standard_support_ends}, on or before "
            f"the review date {POLICY_REVIEWED_ON}, but it is priced at ${rate}/hour rather than "
            f"the extended-support ${CONTROL_PLANE_HOURLY_USD['extended']}/hour. A cluster past "
            f"standard support costs six times more; pricing it at the standard rate understates "
            f"its control plane by ${(CONTROL_PLANE_HOURLY_USD['extended'] - rate) * 730:.0f} a "
            f"month while the result is still called an upper bound (finding W9-05)."
        )


def test_eks_1_33_is_extended_support_at_the_reviewed_rate() -> None:
    """The specific correction the review asked for, pinned as a value.

    The review named the numbers: 1.33 in us-east-1 is EXTENDED support, standard support ended
    2026-07-29, and the control plane costs $438/month rather than $73. Asserting the derived
    monthly figure rather than only the tier because the figure is what a reviewer reads in the
    estimate, and it is what was wrong.
    """
    support = VERSION_SUPPORT["1.33"]
    assert support.tier == "extended", (
        f"EKS 1.33 is recorded as {support.tier!r}. Standard support ended 2026-07-29 — before "
        f"this policy's review date — so it is in extended support."
    )
    assert support.standard_support_ends == "2026-07-29", (
        f"1.33's standard support ended 2026-07-29; this says "
        f"{support.standard_support_ends!r}. The previous value 2026-07-31 was wrong on the date "
        f"as well as on the tier."
    )
    monthly = control_plane_hourly_usd(support) * 730
    assert round(monthly) == 438, (
        f"a 1.33 control plane prices at ${monthly:.0f}/month; the extended-support rate makes it "
        f"$438. The reviewed head produced $73, which is the six-fold understatement W9-05 found."
    )


def test_the_1_35_standard_support_end_is_the_published_date() -> None:
    """1.35's end date was also wrong: 2027-03-31 against the actual 2027-03-27.

    Minor next to the tier error — 1.35 is in standard support either way, so no price changed —
    but a support calendar whose dates are approximations is not a reviewed snapshot, and the four
    days matter on the day a workspace is planned against them.
    """
    assert VERSION_SUPPORT["1.35"].standard_support_ends == "2027-03-27", (
        f"1.35's standard support ends 2027-03-27, not "
        f"{VERSION_SUPPORT['1.35'].standard_support_ends!r}."
    )


def test_the_region_multiplier_states_the_scope_of_its_evidence() -> None:
    """The multiplier is EC2-derived and applied to every service, which must be said out loud.

    The review asked that the multipliers either be verified against the cited service-specific
    prices or that the pricing claim be explicitly restricted to supported evidence. Verifying
    five services across five regions needs AWS's published per-region prices, which this offline
    module does not fetch — so the claim is restricted, and this control keeps that restriction
    from being quietly dropped later by someone who reads the factor as fully verified.
    """
    for phrase, why in (
        (
            "EC2",
            "the evidence's actual source must be named, or the factor reads as a verified "
            "all-services ratio",
        ),
        (
            "STATED ASSUMPTION",
            "the unverified part must be labelled as an assumption rather than implied by the "
            "word 'bound'",
        ),
        (
            "does not fetch",
            "the reason it is unverified — an offline guard — must be given, so a reader can "
            "judge whether to trust it",
        ),
    ):
        assert phrase in REGION_MULTIPLIER_EVIDENCE, (
            f"REGION_MULTIPLIER_EVIDENCE does not mention {phrase!r}: {why}.\n"
            f"Current text: {REGION_MULTIPLIER_EVIDENCE}"
        )


def test_us_east_1_needs_no_multiplier_caveat() -> None:
    """The base region is exactly 1.0, so no assumption is being made about it.

    Pinned because the caveat above is attached only when an adjustment happens. If us-east-1 ever
    stopped being exactly 1.0, an estimate in the base region would be silently scaled by an
    EC2-derived factor with no caveat attached to it.
    """
    assert REGION_PRICE_MULTIPLIER[RATE_TABLE_BASE] == 1.00, (
        f"{RATE_TABLE_BASE} is the rate table's base region and must be exactly 1.0; it is "
        f"{REGION_PRICE_MULTIPLIER[RATE_TABLE_BASE]}."
    )


def test_terraform_version_diagnostic_matches_reviewed_support_tiers():
    text = VARIABLES_TF.read_text()
    diagnostic = re.search(
        r'error_message = "cluster_version must be a version EKS can create today: ([^\n]+)',
        text,
    ).group(1)
    groups = re.findall(r"((?:1\.\d+[, ]*)+) \((extended|standard) support", diagnostic)
    observed = {
        version: tier
        for versions, tier in groups
        for version in re.findall(r"1\.\d+", versions)
    }
    expected = {
        version: support.tier
        for version, support in VERSION_SUPPORT.items()
        if support.tier in CREATABLE_TIERS
    }
    assert observed == expected
