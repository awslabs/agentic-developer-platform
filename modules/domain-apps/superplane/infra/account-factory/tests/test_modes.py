"""Unsupported and unauthorized requests fail before mutation — Issue #5530 (w6-07).

Covers two of the four negative cases AC-02 names: **unknown mode**, and **wrong management
account / organization**. (Unpinned dependencies are `test_dependencies.py`; cleanup outside
owned resources is `test_cleanup.py`.)

## What these tests establish

Validation is the only thing standing between a request and a mutation, so the properties
worth locking down are:

* a refusal happens BEFORE anything is produced — not during, and not reported afterwards;
* an authorization comparison that was not made is reported as unchecked, never as a pass;
* each mode requires exactly its own fields and REFUSES the other modes' fields, so a
  request cannot half-describe two modes at once.

## What they deliberately do NOT establish

Nothing here contacts AWS or Kubernetes, so none of it is evidence that a real organization,
account or cluster exists, is reachable, or would accept these values. Shape validation
rejects obviously-unusable input early; it does not confirm a target. The live target is
unresolved (see `dependencies.lock.yaml`'s `target: status: unresolved`) and remains the
EPIC A supervisor's to settle.
"""

from __future__ import annotations

import pytest
from account_factory.modes import (
    LEGACY_FORBIDDEN_VALUES,
    AccountFactoryRequest,
    ClusterOwnership,
    ModeError,
    OwnershipMode,
    ValidationAuthorization,
    ensure_valid,
    from_mapping,
    validate,
)

from .conftest import (
    BUILDERS,
    FIXTURE_MANAGEMENT_ACCOUNT,
    FIXTURE_ORG_ID,
    bring_existing_cluster_request,
    existing_account_request,
    matching_authorization,
    new_account_request,
    resolved_binding,
)

# ── The three modes exist and are distinguishable ────────────────────────────────────


def test_exactly_three_modes_are_supported():
    """The issue names three modes. A fourth appearing silently would change the contract."""
    assert {mode.value for mode in OwnershipMode} == {
        "new-account-managed",
        "existing-account-managed",
        "bring-existing-cluster",
    }


def test_only_new_account_managed_creates_an_account():
    """`creates_account` is what gates account closure in `cleanup.py`."""
    creating = {mode for mode in OwnershipMode if mode.creates_account}
    assert creating == {OwnershipMode.NEW_ACCOUNT_MANAGED}


def test_only_bring_existing_cluster_adopts_its_cluster():
    """Cluster ownership is per-mode, and drives the delete boundary."""
    for mode, build in BUILDERS.items():
        request = build()
        expected = (
            ClusterOwnership.ADOPTED
            if mode is OwnershipMode.BRING_EXISTING_CLUSTER
            else ClusterOwnership.ADP_CREATED
        )
        assert request.cluster_ownership is expected, mode


def test_each_valid_fixture_passes_validation():
    """The positive control. Without it, the negative tests could pass vacuously."""
    for mode, build in BUILDERS.items():
        problems, _ = validate(build())
        assert problems == [], f"{mode.value}: {problems}"


# ── AC-02: unknown mode ──────────────────────────────────────────────────────────────


def test_unknown_mode_from_config_is_refused_by_name():
    """A config file naming a mode this module does not implement is refused."""
    with pytest.raises(ModeError) as raised:
        from_mapping(
            {
                "mode": "delete-everything",
                "organization_id": FIXTURE_ORG_ID,
                "management_account_id": FIXTURE_MANAGEMENT_ACCOUNT,
                "management_cluster": "c",
                "region": "us-west-2",
                "workspace_id": "ws-x",
            }
        )
    message = str(raised.value)
    assert "delete-everything" in message
    # The supported set is named, so the operator learns what to use instead.
    for mode in OwnershipMode:
        assert mode.value in message


def test_unknown_mode_is_refused_before_any_object_is_produced():
    """The refusal must precede production, which is the "before mutation" requirement.

    `from_mapping` raises rather than returning a partially-populated request, so there is
    no object for a caller to accidentally proceed with.
    """
    with pytest.raises(ModeError):
        from_mapping({"mode": "new-account-manged"})  # transposed characters


def test_a_raw_string_mode_bypassing_from_mapping_is_still_refused():
    """Defence in depth: `validate` does not assume its caller used `from_mapping`."""
    request = AccountFactoryRequest(
        mode="new-account-managed",  # a string, not the enum
        organization_id=FIXTURE_ORG_ID,
        management_account_id=FIXTURE_MANAGEMENT_ACCOUNT,
        management_cluster="c",
        region="us-west-2",
        workspace_id="ws-x",
    )
    problems, unchecked = validate(request)
    assert any("not a supported ownership mode" in problem for problem in problems)
    # No authorization comparison is claimed for a request whose mode is unusable.
    assert unchecked == []


def test_unknown_config_field_is_refused_rather_than_ignored():
    """A misspelled field silently dropped changes what gets created."""
    with pytest.raises(ModeError) as raised:
        from_mapping(
            {
                "mode": "bring-existing-cluster",
                "organization_id": FIXTURE_ORG_ID,
                "management_account_id": FIXTURE_MANAGEMENT_ACCOUNT,
                "management_cluster": "c",
                "region": "us-west-2",
                "workspace_id": "ws-x",
                "existing_cluster_nmae": "typo-cluster",  # would silently create a cluster
            }
        )
    assert "existing_cluster_nmae" in str(raised.value)


def test_no_required_identity_is_defaulted():
    """Every identity must be supplied. A defaulted target is the legacy defect."""
    for omitted in (
        "organization_id",
        "management_account_id",
        "management_cluster",
        "region",
        "workspace_id",
    ):
        data = {
            "mode": "new-account-managed",
            "organization_id": FIXTURE_ORG_ID,
            "management_account_id": FIXTURE_MANAGEMENT_ACCOUNT,
            "management_cluster": "c",
            "region": "us-west-2",
            "workspace_id": "ws-x",
        }
        del data[omitted]
        with pytest.raises(ModeError) as raised:
            from_mapping(data)
        assert omitted in str(raised.value)


def test_comma_joined_availability_zones_are_refused():
    """The legacy `config.env` held `AVAILABILITY_ZONES="us-east-1a,us-east-1b"`.

    Read as a list of one, that string would become a single nonexistent zone name.
    """
    with pytest.raises(ModeError) as raised:
        from_mapping(
            {
                "mode": "new-account-managed",
                "organization_id": FIXTURE_ORG_ID,
                "management_account_id": FIXTURE_MANAGEMENT_ACCOUNT,
                "management_cluster": "c",
                "region": "us-west-2",
                "workspace_id": "ws-x",
                "availability_zones": "us-west-2a,us-west-2b",
            }
        )
    assert "must be a list" in str(raised.value)


# ── AC-02: wrong management account / organization ───────────────────────────────────


def test_wrong_organization_is_refused():
    """Acting in an organization this run was not authorized for."""
    request = new_account_request()
    authorization = matching_authorization(request, organization_id="o-otherorg999")
    problems, _ = validate(request, authorization)
    assert any("not the organization this run is authorized for" in p for p in problems)


def test_wrong_management_account_is_refused():
    """The comparison the legacy flow could not make.

    Its check confirmed that the ambient credentials resolved to the `AWS_ACCOUNT_ID` in
    its own config file — i.e. that the config matched itself. It could not detect that the
    config named the wrong account.
    """
    request = new_account_request()
    authorization = matching_authorization(
        request, management_account_id="999999999999"
    )
    problems, _ = validate(request, authorization)
    assert any(
        "not the management account this run is authorized for" in p for p in problems
    )


def test_wrong_management_cluster_is_refused():
    request = new_account_request()
    authorization = matching_authorization(
        request, management_cluster="some-other-cluster"
    )
    problems, _ = validate(request, authorization)
    assert any(
        "not the cluster this run is authorized to act from" in p for p in problems
    )


def test_a_mode_outside_the_permitted_set_is_refused():
    """Authorization can permit a subset of modes — e.g. adopt-only, never vend."""
    request = new_account_request()
    authorization = matching_authorization(
        request,
        permitted_modes=frozenset(
            {
                OwnershipMode.EXISTING_ACCOUNT_MANAGED,
                OwnershipMode.BRING_EXISTING_CLUSTER,
            }
        ),
    )
    problems, _ = validate(request, authorization)
    assert any("is not permitted for this run" in p for p in problems)


def test_a_matching_authorization_passes_and_leaves_nothing_unchecked():
    """The positive control for the four tests above."""
    request = new_account_request()
    problems, unchecked = validate(request, matching_authorization(request))
    assert problems == []
    assert unchecked == []


# ── AF-003: the workspace and target account are compared, against authority values ──
#
# These lock a specific regression. Before the repair, `ValidationAuthorization` had no
# workspace or target-account field, so neither was compared against anything: a request
# naming ANOTHER tenant's workspace, accompanied by an authorization supplying every field
# the type then supported, validated as `([], [])` — no problems AND nothing unchecked, i.e.
# reported as fully verified. That is worse than an unchecked value, because a report could
# not tell the two apart.


def test_a_valid_but_different_workspace_is_refused():
    """The AF-003 probe. Every other authorization field matches; only the workspace differs.

    `ws-other-tenant` is a perfectly valid workspace id — that is the point. The refusal must
    come from it not being THIS run's workspace, not from it being malformed.
    """
    request = existing_account_request()
    authorization = matching_authorization(request, workspace_id="ws-other-tenant")
    problems, unchecked = validate(request, authorization)
    assert any("not the workspace this run is authorized for" in p for p in problems), (
        problems
    )
    # And specifically NOT the pre-repair outcome:
    assert (problems, unchecked) != ([], [])


def test_a_valid_but_unauthorized_target_account_is_refused():
    """A well-formed account id that this run was not authorized to act in.

    Adoption reaches into an existing account, so an uncompared target account id is a route
    into an account nobody chose — the same class of defect as the legacy defaulted target,
    arriving by a different door.
    """
    request = existing_account_request()
    authorization = matching_authorization(
        request, permitted_target_accounts=frozenset({"000000000777"})
    )
    problems, _ = validate(request, authorization)
    assert any(
        "not an account this run is authorized to act in" in p for p in problems
    ), problems


def test_an_empty_permitted_account_set_authorizes_no_account():
    """Empty is a decision, not an absence.

    `None` means "no value was supplied, so the comparison was not made"; `frozenset()` means
    "this run may act in no account". Collapsing the two would make the safest possible
    authorization behave like the most permissive one.
    """
    request = existing_account_request()
    authorization = matching_authorization(
        request, permitted_target_accounts=frozenset()
    )
    problems, unchecked = validate(request, authorization)
    assert any("permitted: none" in p for p in problems), problems
    assert "target_account_id" not in unchecked


def test_a_workspace_mismatch_is_refused_in_every_mode():
    """Whichever mode is requested, the workspace boundary holds.

    Parametrised over all three because AF-003 was a property of the validator, not of one
    mode, and a single-mode test would not have caught it in the others.
    """
    for build in BUILDERS.values():
        request = build()
        authorization = matching_authorization(request, workspace_id="ws-other-tenant")
        problems, _ = validate(request, authorization)
        assert any(
            "not the workspace this run is authorized for" in p for p in problems
        ), f"{request.mode.value}: {problems}"


def test_authorization_built_from_a_binding_takes_the_workspace_from_the_principal():
    """The workspace comes from the authority, not from the request.

    This is the rule the shared provisioning contract states for the same reason:
    `ProvisioningIntent` carries no workspace, because the workspace an operation acts on is
    resolved server-side from the binding's principal. A caller cannot name the tenant it
    provisions for, so it cannot widen its own authorization by editing its request.
    """
    request = existing_account_request()
    authorization = ValidationAuthorization.from_operation_binding(
        resolved_binding(workspace_id=request.workspace_id),
        management_account_id=request.management_account_id,
        management_cluster=request.management_cluster,
        permitted_modes=frozenset(OwnershipMode),
        permitted_target_accounts=frozenset({request.target_account_id}),
    )
    assert authorization.workspace_id == request.workspace_id
    assert authorization.organization_id == request.organization_id
    assert validate(request, authorization) == ([], [])


def test_a_binding_for_another_workspace_refuses_this_request():
    """The negative half of the test above: a real binding cannot be pointed elsewhere."""
    request = existing_account_request()
    authorization = ValidationAuthorization.from_operation_binding(
        resolved_binding(workspace_id="ws-other-tenant"),
        permitted_target_accounts=frozenset({request.target_account_id}),
    )
    problems, _ = validate(request, authorization)
    assert any("not the workspace this run is authorized for" in p for p in problems)


def test_a_binding_without_a_resolved_principal_is_refused():
    """No resolved principal means no authority-side workspace, so there is nothing to compare.

    Refused rather than falling back to the request's own workspace: that fallback would
    reinstate AF-003 exactly, while looking like a check.
    """

    class _Unresolved:
        principal = None

    with pytest.raises(ModeError) as raised:
        ValidationAuthorization.from_operation_binding(_Unresolved())
    assert "resolved principal" in str(raised.value)


def test_every_mismatch_is_reported_not_just_the_first():
    """Accumulating problems means one fix per run is not required to find the next."""
    request = new_account_request()
    authorization = ValidationAuthorization(
        organization_id="o-otherorg999",
        management_account_id="999999999999",
        management_cluster="other-cluster",
        permitted_modes=frozenset({OwnershipMode.BRING_EXISTING_CLUSTER}),
        workspace_id="ws-someone-elses",
        permitted_organizational_units=frozenset({"ou-other-999999zz"}),
    )
    problems, unchecked = validate(request, authorization)
    assert len(problems) == 6, problems
    assert unchecked == []


# ── Unchecked is not the same as passed ──────────────────────────────────────────────


def test_absent_authorization_reports_every_comparison_as_unchecked():
    """With no authorization supplied, nothing was verified — and it says so.

    This is the distinction the report depends on: "the management account was not
    verified" must not be indistinguishable from "the management account matched".
    """
    problems, unchecked = validate(new_account_request(), None)
    assert problems == []
    assert set(unchecked) == {
        "organization_id",
        "management_account_id",
        "management_cluster",
        "mode",
        "workspace_id",
        # This mode places the account in the organization tree (#5531), so the placement
        # is one more comparison that was not made — and an unverified placement is the one
        # that decides which service control policies the new account is born under.
        "organizational_unit_id",
    }


def test_absent_authorization_reports_the_target_account_as_unchecked_too():
    """In a mode that names a target account, that comparison is one more thing unverified.

    Separate from the test above because the two modes differ: new-account-managed has no
    target account to compare, and reporting a check it could never make would overstate
    what is missing.
    """
    _, unchecked = validate(existing_account_request(), None)
    assert "target_account_id" in unchecked


def test_a_mode_that_creates_the_account_reports_no_target_account_comparison():
    """`new-account-managed` forbids a target account id, so none is pending verification."""
    _, unchecked = validate(new_account_request(), None)
    assert "target_account_id" not in unchecked


def test_partial_authorization_reports_exactly_the_missing_comparisons():
    request = new_account_request()
    authorization = ValidationAuthorization(organization_id=request.organization_id)
    problems, unchecked = validate(request, authorization)
    assert problems == []
    assert set(unchecked) == {
        "management_account_id",
        "management_cluster",
        "mode",
        "workspace_id",
        "organizational_unit_id",
    }


def test_ensure_valid_returns_the_unchecked_list_for_a_valid_request():
    unchecked = ensure_valid(new_account_request())
    assert "management_account_id" in unchecked


def test_ensure_valid_raises_with_every_problem_listed():
    request = new_account_request(workspace_id="ADP-Gateway")  # invalid shape
    with pytest.raises(ModeError) as raised:
        ensure_valid(request)
    assert "refused before any mutation" in str(raised.value)


# ── Per-mode field requirements, both directions ─────────────────────────────────────


def test_new_account_managed_refuses_a_target_account_id():
    """Supplying an id for an account that does not exist yet names a DIFFERENT account."""
    problems, _ = validate(new_account_request(target_account_id="000000000009"))
    assert any("target_account_id must be absent" in p for p in problems)


def test_new_account_managed_requires_an_account_email():
    problems, _ = validate(new_account_request(account_email=None))
    assert any("account_email is required" in p for p in problems)


def test_existing_account_managed_requires_the_account_to_be_named():
    problems, _ = validate(existing_account_request(target_account_id=None))
    assert any("target_account_id is required" in p for p in problems)


def test_existing_account_managed_refuses_an_account_email():
    """The account exists; its address is not this request's to set."""
    problems, _ = validate(existing_account_request(account_email="x@example.invalid"))
    assert any("account_email must be absent" in p for p in problems)


def test_bring_existing_cluster_requires_the_cluster_to_be_named():
    problems, _ = validate(bring_existing_cluster_request(existing_cluster_name=None))
    assert any("existing_cluster_name is required" in p for p in problems)


@pytest.mark.parametrize(
    "field,value",
    [
        ("vpc_cidr", "10.70.0.0/16"),
        ("cluster_version", "1.31"),
        ("node_instance_type", "m6i.large"),
        ("availability_zones", ("us-west-2a",)),
    ],
)
def test_bring_existing_cluster_refuses_fields_it_would_ignore(field, value):
    """A value that cannot take effect is refused rather than silently dropped.

    Accepting `cluster_version` here would imply this request upgrades the adopted cluster,
    which it does not.
    """
    problems, _ = validate(bring_existing_cluster_request(**{field: value}))
    assert any(field in p and "must be absent" in p for p in problems)


def test_cluster_creating_modes_require_the_cluster_inputs():
    for build in (new_account_request, existing_account_request):
        problems, _ = validate(build(vpc_cidr=None, cluster_version=None))
        assert any("vpc_cidr is required" in p for p in problems)
        assert any("cluster_version is required" in p for p in problems)


def test_availability_zone_outside_the_region_is_refused():
    problems, _ = validate(
        new_account_request(availability_zones=("us-east-1a", "us-west-2b"))
    )
    assert any("us-east-1a" in p and "not in region" in p for p in problems)


# ── Workspace boundary ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "namespace",
    ["adp", "adp-gateway", "kube-system", "default", "kro-system", "ack-system"],
)
def test_a_core_namespace_cannot_be_used_as_a_workspace_id(namespace):
    """The workspace id becomes a namespace, so an unchecked id is a route into core ADP."""
    problems, _ = validate(new_account_request(workspace_id=namespace))
    assert any("core ADP or Kubernetes namespace" in p for p in problems)


@pytest.mark.parametrize(
    "workspace_id",
    ["Ws-Upper", "ws_underscore", "-leading", "trailing-", "ws/slash", ""],
)
def test_a_workspace_id_that_is_not_a_kubernetes_name_is_refused(workspace_id):
    problems, _ = validate(new_account_request(workspace_id=workspace_id))
    assert any("not a valid Kubernetes object name" in p for p in problems)


# ── Legacy fixed targets are refused by value ────────────────────────────────────────


def test_every_legacy_value_is_refused_wherever_it_appears():
    """Un-defaulting the legacy targets is not enough; they are refused if passed in.

    Each legacy value is tried in the field it originally occupied in `config.env`.
    """
    placements = {
        "605440105851": ("management_account_id", existing_account_request),
        "github-arc-runner-eks": ("management_cluster", new_account_request),
        "prsaws+aisuperplane@amazon.com": ("account_email", new_account_request),
        "superplane-test": ("workspace_id", new_account_request),
    }
    assert set(placements) == set(LEGACY_FORBIDDEN_VALUES), (
        "a legacy value was added to LEGACY_FORBIDDEN_VALUES without a test placing it"
    )
    for value, (field, build) in placements.items():
        problems, _ = validate(build(**{field: value}))
        assert any(
            value in p and "Legacy fixed targets are refused" in p for p in problems
        ), f"{value!r} in {field} was not refused"


def test_legacy_values_are_refused_case_insensitively():
    problems, _ = validate(
        new_account_request(management_cluster="GitHub-ARC-Runner-EKS")
    )
    assert any("Legacy fixed targets are refused" in p for p in problems)
