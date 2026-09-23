"""The access prerequisite gate — Issue #5533 (w6-10), review finding F4.

F4 verbatim: access/security-group prerequisites were "optional and unverified
(`inventory` defaulted to `None`)".

The defect was an *argument default*, so the most important test here is not a refusal
at all — it is `test_the_prerequisite_seams_have_no_defaults_to_fall_back_to`, which
asserts the seams are mandatory, paired with
`test_the_gate_is_reached_on_the_clean_path_and_records_its_inventory`, which asserts
the entry point actually calls them. Every other refusal below is reachable only because
those two hold; if the gate went back to being optional, the refusals would still pass
while nothing called them.

Two properties are asserted repeatedly and deliberately:

**The gate runs before the first cluster mutation.** A missing access path must refuse
against an untouched cluster. The assertions are `access.created_namespaces == []` and
`access.calls == []` — what did NOT happen — because a refusal that arrives after a
namespace exists is strictly worse than the same refusal one step earlier, and the
difference is invisible in the exception message.

**Ownership is derived from the read, never claimed by a caller.** A real `aws` read
cannot see who created a security-group rule, so the default is ADOPTED and adopted
things are never removable. This is the asymmetry AC-02 protects: on a supplied cluster,
revoking a pre-existing rule is the same class of harm as deleting somebody's namespace.
"""

from __future__ import annotations

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.inventory import (
    ADOPTED,
    ADP_CREATED,
    PrerequisiteInventory,
)
from superplane_bootstrap.prerequisites import (
    ACCESS_ENTRY,
    ENDPOINT_RULE,
    MANAGEMENT_RULE,
    REQUIRED_ACCESS_SCOPE,
    REQUIRED_PREREQUISITE_KINDS,
    ExpectedPrerequisites,
    require_inventory,
    summarize,
    verify_prerequisites,
)
from superplane_bootstrap.state import BootstrapState
from superplane_bootstrap.target import verify_target
from superplane_bootstrap.workspace import bootstrap_workspace
from superplane_contracts.secrets import assert_no_secret_material

from .conftest import (
    ACCOUNT_ID,
    CLUSTER_SG_ID,
    CNI_ROLE_ARN,
    CREDENTIAL_ID,
    ENDPOINT_RULE_ID,
    ENFORCE_VERSION,
    MANAGEMENT_RULE_ID,
    MANAGEMENT_SG_ID,
    NAMESPACE,
    PRINCIPAL_ARN,
    VPC_ID,
    WORKSPACE_ID,
    FakeClusterAccess,
    FakePrerequisiteAccess,
    FakeRegistrationStore,
    FakeStateStore,
)


@pytest.fixture
def target(binding, provider_identity, observed_cluster, expected_target):
    return verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
    )


def _expected(**overrides) -> ExpectedPrerequisites:
    """Terraform's published outputs, as the authoritative expectation.

    Overridden per test rather than mutated, because `ExpectedPrerequisites` is frozen
    for exactly this reason: an expectation a gate could edit mid-verification is an
    expectation that proves nothing.
    """
    return ExpectedPrerequisites(
        **{
            "account_id": ACCOUNT_ID,
            "vpc_id": VPC_ID,
            "cluster_security_group_id": CLUSTER_SG_ID,
            "management_security_group_id": MANAGEMENT_SG_ID,
            "node_security_group_id": "sg-synthetic-nodes",
            "sts_endpoint_security_group_id": "sg-synthetic-sts",
            "sts_endpoint_vpc_id": VPC_ID,
            **overrides,
        }
    )


def _verify(target, access=None, *, store=None, **overrides):
    arguments = {
        "access": access if access is not None else FakePrerequisiteAccess(),
        "target": target,
        "expected": _expected(),
        "principal_arn": PRINCIPAL_ARN,
        "namespace": NAMESPACE,
        "store": store if store is not None else FakeStateStore(),
        "state": BootstrapState(
            workspace_id=target.workspace_id, cluster_arn=target.cluster_arn
        ),
        "provider_account_id": ACCOUNT_ID,
        **overrides,
    }
    return verify_prerequisites(**arguments)


def _run(
    access,
    store,
    binding,
    provider_identity,
    observed_cluster,
    expected_target,
    **overrides,
):
    """The real entry point. Used here to prove the gate is wired, not just correct."""
    return bootstrap_workspace(
        **{
            "binding": binding,
            "provider": provider_identity,
            "access": access,
            "prerequisite_access": FakePrerequisiteAccess(),
            "store": store,
            "state_store": FakeStateStore(),
            "observed_cluster": observed_cluster,
            "expected_account_id": expected_target["expected_account_id"],
            "expected_region": expected_target["expected_region"],
            "expected_cluster_name": expected_target["expected_cluster_name"],
            "expected_cluster_arn": expected_target["expected_cluster_arn"],
            "expected_certificate_authority_data": expected_target[
                "expected_certificate_authority_data"
            ],
            "expected_cni_role_arn": CNI_ROLE_ARN,
            "expected_prerequisites": _expected(),
            "cluster_ownership": "adp-created",
            "namespace": NAMESPACE,
            "enforce_version": ENFORCE_VERSION,
            "credential_reference_id": CREDENTIAL_ID,
            "contract_version": "v1",
            "screen": assert_no_secret_material,
            **overrides,
        }
    )


# --- The gate is mandatory (the F4 defect itself) --------------------------------


def test_the_prerequisite_seams_have_no_defaults_to_fall_back_to():
    """The F4 defect restated as a signature property.

    `inventory` used to be an optional argument nothing read, so a bootstrap could
    register a ready workspace having verified no access path at all. The repair is the
    ABSENCE of a default: omitting any of these is a TypeError raised by Python before
    any code runs, not a refusal the implementation has to remember to make. Asserted
    against the signature because "has no default" is the property, and a test that
    merely called the function could be satisfied by a default of `None` plus a check.
    """
    import inspect

    parameters = inspect.signature(bootstrap_workspace).parameters

    for name in ("expected_prerequisites", "prerequisite_access", "state_store"):
        assert parameters[name].default is inspect.Parameter.empty, (
            f"{name} has a default again, which is exactly the shape of the F4 defect: "
            "a caller can obtain a registered workspace without supplying it"
        )


def test_an_explicitly_absent_expectation_refuses_rather_than_crashing(
    binding, provider_identity, observed_cluster, expected_target
):
    """A required argument can still be passed `None`.

    That is not hypothetical: the operator CLI reads these ids from Terraform outputs,
    and an output that is absent arrives as `None`. Before this check it produced an
    `AttributeError` several comparisons into the gate — which reads as a bug in the
    gate rather than as the missing input it is, and would send an operator to the wrong
    file. It must also still refuse against an untouched cluster.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore()

    outcome = _run(
        access,
        store,
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        expected_prerequisites=None,
    )

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert "outputs.tf" in str(outcome.refusal)
    assert outcome.ready is False
    assert access.calls == []


def test_the_gate_is_reached_on_the_clean_path_and_records_its_inventory(
    binding, provider_identity, observed_cluster, expected_target
):
    """Wired, not merely present. A gate the entry point never calls is F1's defect
    applied to F4's code, and the two would be indistinguishable from the outside."""
    prerequisite_access = FakePrerequisiteAccess()
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore()

    outcome = _run(
        access,
        store,
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        prerequisite_access=prerequisite_access,
    )

    assert outcome.ready is True
    assert outcome.inventory is not None
    assert {item.kind for item in outcome.inventory.prerequisites} == set(
        REQUIRED_PREREQUISITE_KINDS
    )
    assert prerequisite_access.calls, "the prerequisite reads never happened"


def test_a_missing_access_entry_refuses_before_the_cluster_is_touched(
    binding, provider_identity, observed_cluster, expected_target
):
    """The ordering half of F4, asserted as what did NOT happen.

    The refusal message alone cannot distinguish "refused before any mutation" from
    "refused after creating a namespace"; only the untouched cluster can.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore()

    outcome = _run(
        access,
        store,
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        prerequisite_access=FakePrerequisiteAccess(entry_exists=False),
    )

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert outcome.ready is False
    assert outcome.inventory is None
    assert outcome.installation is None
    assert access.created_namespaces == []
    assert access.calls == [], (
        "the cluster was read or mutated before the access path was verified"
    )
    assert store.finalized == []
    assert store.reservations == {}, (
        "a registration was reserved before the prerequisite gate ran; the reservation "
        "must not be taken for a workspace whose access path does not exist"
    )


# --- Control ---------------------------------------------------------------------


def test_a_complete_access_path_verifies_and_records_every_kind(target):
    """The positive control, without which every negative below could pass vacuously."""
    inventory, state = _verify(target)

    assert inventory.workspace_id == WORKSPACE_ID
    assert {item.kind for item in inventory.prerequisites} == set(
        REQUIRED_PREREQUISITE_KINDS
    )
    assert state.prerequisites_recorded is True


def test_the_inventory_is_recorded_durably_before_the_caller_sees_it(target):
    """F6's rule applied here: the record is written during the gate, not by the caller.

    A caller responsible for persisting the inventory is a caller that can be killed
    before doing so — and an unrecorded access path is one nothing will revoke.
    """
    store = FakeStateStore()

    inventory, _ = _verify(target, store=store)

    assert store.history, "no durable write happened during the gate"
    assert store.history[-1]["prerequisites_recorded"] is True
    from superplane_bootstrap.state import state_from_mapping

    restored = state_from_mapping(store.history[-1])
    assert restored.prerequisite_inventory == inventory
    assert {
        item.identifier for item in restored.prerequisite_inventory.prerequisites
    } == {item.identifier for item in inventory.prerequisites}


def test_each_rule_is_recorded_with_the_id_retirement_revokes_by(target):
    """A rule matched only by shape could revoke one somebody else created, so the id
    is the identifier rather than the source/port tuple."""
    inventory, _ = _verify(target)
    identifiers = {item.kind: item.identifier for item in inventory.prerequisites}

    assert identifiers[ENDPOINT_RULE] == ENDPOINT_RULE_ID
    assert identifiers[MANAGEMENT_RULE] == MANAGEMENT_RULE_ID


def test_every_recorded_prerequisite_carries_an_attribution_reason(target):
    """An unattributed prerequisite is one nobody will know to remove — the accidental
    permanence F4 describes. `OwnedPrerequisite` requires a reason; this asserts the
    gate supplies a meaningful one rather than a placeholder."""
    inventory, _ = _verify(target)

    for item in inventory.prerequisites:
        assert item.reason.strip()
        assert len(item.reason) > 20, (
            f"{item.kind} carries a reason too short to tell an operator anything: "
            f"{item.reason!r}"
        )


# --- The scoped access entry -----------------------------------------------------


def test_a_cluster_scoped_access_entry_is_refused(target):
    """Cluster-admin by another name. It would make every namespace boundary this
    package establishes advisory, and it is the single most consequential misread here
    because everything downstream would still pass."""
    with pytest.raises(BootstrapRefused, match="cluster-admin by another name"):
        _verify(target, FakePrerequisiteAccess(entry_scope="cluster"))


def test_an_access_entry_covering_extra_namespaces_is_refused(target):
    """ "Includes the workspace namespace" is not "is confined to it"."""
    with pytest.raises(BootstrapRefused, match="beyond the workspace"):
        _verify(
            target,
            FakePrerequisiteAccess(entry_namespaces=(NAMESPACE, "someone-elses-work")),
        )


def test_an_access_entry_confined_to_a_different_namespace_is_refused(target):
    with pytest.raises(BootstrapRefused, match="not exactly"):
        _verify(target, FakePrerequisiteAccess(entry_namespaces=("other-namespace",)))


def test_an_access_entry_reporting_no_namespaces_at_all_is_refused(target):
    """An entry confined to nothing is an answer, and the answer is wrong.

    Distinct from the unanswered case below: `()` was reported, so the refusal is the
    confinement mismatch rather than the missing-observation one. Worth its own test
    because an empty scope is what a half-applied Terraform change produces, and a
    laxer comparison ("contains the namespace" rather than "equals exactly") would
    accept it.
    """
    with pytest.raises(BootstrapRefused, match="not exactly"):
        _verify(target, FakePrerequisiteAccess(entry_namespaces=()))


def test_an_access_entry_with_an_unreported_namespace_scope_is_refused(target):
    """The genuinely unanswered case: no `namespaces` key in the observation.

    An unbounded scope cannot be distinguished from a correctly confined one, so this
    refuses rather than assuming either — the package-wide rule that an unanswered
    question is not a negative answer.
    """
    access = FakePrerequisiteAccess()
    access.access_entry = lambda cluster_arn, principal_arn: {  # type: ignore[method-assign]
        "exists": True,
        "scope": REQUIRED_ACCESS_SCOPE,
        "policy": "arn:aws:eks::aws:cluster-access-policy/SyntheticNamespaceAdmin",
    }

    with pytest.raises(BootstrapRefused, match="did not report which namespaces"):
        _verify(target, access)


def test_a_string_namespace_scope_is_not_read_as_a_one_element_sequence(target):
    """`"superplane-workspace"` iterates as characters, which would compare unequal and
    refuse for a confusing reason — or, with a laxer comparison, pass wrongly. The
    explicit `isinstance(..., str)` rejection is what this pins."""
    access = FakePrerequisiteAccess()
    access.entry_namespaces = NAMESPACE  # type: ignore[assignment]

    with pytest.raises(BootstrapRefused, match="did not report which namespaces"):
        _verify(target, access)


def test_an_access_entry_with_no_policy_is_refused(target):
    """An entry whose policy is unknown has unknown authority."""
    with pytest.raises(BootstrapRefused, match="did not report an access policy"):
        _verify(target, FakePrerequisiteAccess(entry_policy="   "))


def test_the_access_entry_is_read_for_the_bound_principal(target):
    """Read for the principal the operation is bound to, not a caller parameter.

    An entry verified for some other principal says nothing about whether THIS
    credential can reach the cluster.
    """
    access = FakePrerequisiteAccess()

    _verify(target, access)

    assert any(target.cluster_arn in call for call in access.calls)


def test_a_missing_principal_arn_is_refused(target):
    with pytest.raises(BootstrapRefused, match="principal ARN is required"):
        _verify(target, principal_arn="  ")


def test_a_missing_namespace_is_refused(target):
    """Without a namespace the entry's confinement cannot be checked against anything,
    which would turn the most important check in this module into a no-op."""
    with pytest.raises(BootstrapRefused, match="namespace is required"):
        _verify(target, namespace="   ")


# --- The security-group rules ----------------------------------------------------


def test_a_missing_endpoint_rule_is_refused(target):
    """The management plane reaches the workspace API server through it. Without it,
    later gates fail confusingly or pass against something else."""
    with pytest.raises(BootstrapRefused, match="does not exist"):
        _verify(target, FakePrerequisiteAccess(rules_exist=False))


def test_a_rule_in_another_account_is_refused(target):
    """Either it does not affect this cluster or it opens a path into unrelated
    infrastructure. Both are refusals, and the message names the account because the
    operator action is a credential problem."""
    with pytest.raises(BootstrapRefused, match="exists in account"):
        _verify(target, FakePrerequisiteAccess(rule_account_id="999999999999"))


def test_a_rule_in_another_vpc_is_refused(target):
    """A wrong VPC is a wrong-environment problem — typically a dev expectation checked
    against a prod cluster."""
    with pytest.raises(BootstrapRefused, match="attached to VPC"):
        _verify(target, FakePrerequisiteAccess(rule_vpc_id="vpc-11111111111111111"))


def test_a_rule_on_the_wrong_port_is_refused(target):
    with pytest.raises(BootstrapRefused, match="permits port"):
        _verify(target, FakePrerequisiteAccess(rule_port=22))


def test_a_rule_with_the_wrong_protocol_is_refused(target):
    with pytest.raises(BootstrapRefused, match="permits protocol"):
        _verify(target, FakePrerequisiteAccess(rule_protocol="udp"))


@pytest.mark.parametrize(
    "field_name",
    ["rule_id", "group_id", "source", "vpc_id", "account_id", "port", "protocol"],
)
def test_each_omitted_rule_field_is_refused_by_name(target, field_name):
    """Parametrized per field so a shrinking required set is a failure.

    Naming the field matters: a generic "malformed rule" gives the operator nothing,
    and the action differs per field — a missing `rule_id` means retirement could not
    revoke precisely, a missing `account_id` means attribution is unverifiable.
    """
    with pytest.raises(BootstrapRefused, match=field_name):
        _verify(target, FakePrerequisiteAccess(omit_rule_fields=(field_name,)))


def test_an_omitted_rule_field_refusal_says_why_it_matters(target):
    with pytest.raises(BootstrapRefused, match="cannot be revoked precisely"):
        _verify(target, FakePrerequisiteAccess(omit_rule_fields=("rule_id",)))


def test_a_blank_rule_id_is_refused(target):
    """Present but empty. Retirement revokes by id, and a blank one matched by shape
    could revoke a rule somebody else created."""
    access = FakePrerequisiteAccess()
    original = access.security_group_rule

    def blank_id(group_id, source, port, protocol):
        return {**dict(original(group_id, source, port, protocol)), "rule_id": "   "}

    access.security_group_rule = blank_id  # type: ignore[method-assign]

    with pytest.raises(BootstrapRefused, match="exact security-group rule ID"):
        _verify(target, access)


# --- Unanswered reads are not negative answers -----------------------------------


def test_an_access_entry_read_that_does_not_answer_is_refused(target):
    """The package-wide rule: a read with no `exists` key refuses.

    The natural implementation returns `{}` on an API error and `{}.get("exists")` is
    falsy — so "the API call failed" would silently become "the entry does not exist",
    which is at least a comprehensible refusal. The real hazard is the same defaulting
    applied to `created_by_bootstrap`, where falsy is the SAFE direction and a future
    edit could easily invert it.
    """
    access = FakePrerequisiteAccess()
    access.access_entry = lambda cluster_arn, principal_arn: {}  # type: ignore[method-assign]

    with pytest.raises(BootstrapRefused, match="did not report whether it exists"):
        _verify(target, access)


def test_a_rule_read_that_does_not_answer_is_refused(target):
    access = FakePrerequisiteAccess()
    access.security_group_rule = lambda group_id, source, port, protocol: {}  # type: ignore[method-assign]

    with pytest.raises(BootstrapRefused, match="did not report whether it exists"):
        _verify(target, access)


def test_a_read_returning_no_observation_at_all_is_refused(target):
    """`None` rather than a Mapping. Refusing to treat an unreadable answer as
    'absent' is the explicit wording, and this is the path that reaches it."""
    access = FakePrerequisiteAccess()
    access.access_entry = lambda cluster_arn, principal_arn: None  # type: ignore[method-assign,return-value]

    with pytest.raises(BootstrapRefused, match="did not return an observation"):
        _verify(target, access)


# --- Ownership is derived, never claimed (AC-02) ---------------------------------


def test_an_unattributable_prerequisite_defaults_to_adopted_and_is_never_removable(
    target,
):
    """The safe default, asserted as a default.

    A real `aws` read cannot observe who created a rule. So absent an explicit
    `created_by_bootstrap`, the rule predates this bootstrap as far as anything can
    tell — and ADP deletes only what it created. Getting this backwards means cleanup
    revoking a pre-existing rule on a supplied cluster, which is AC-02's named harm.
    """
    inventory, _ = _verify(target)

    assert all(item.ownership == ADOPTED for item in inventory.prerequisites)
    assert inventory.removable == ()
    assert len(inventory.preserved) == len(REQUIRED_PREREQUISITE_KINDS)


def test_a_prerequisite_the_read_attributes_to_bootstrap_is_removable(target):
    """The other direction. An object ADP created and did not record becomes permanent
    by accident, so a genuine `adp-created` read must produce a removable entry."""
    inventory, _ = _verify(
        target,
        FakePrerequisiteAccess(
            entry_created_by_bootstrap=True, rule_created_by_bootstrap=True
        ),
    )

    assert all(item.ownership == ADP_CREATED for item in inventory.prerequisites)
    assert len(inventory.removable) == len(REQUIRED_PREREQUISITE_KINDS)
    assert inventory.preserved == ()


def test_an_explicit_false_attribution_is_adopted(target):
    """Distinct from absent, and the same verdict. Both mean "not ours to delete"."""
    inventory, _ = _verify(
        target,
        FakePrerequisiteAccess(
            entry_created_by_bootstrap=False, rule_created_by_bootstrap=False
        ),
    )

    assert all(item.ownership == ADOPTED for item in inventory.prerequisites)


def test_a_truthy_non_true_attribution_does_not_confer_ownership(target):
    """`created_by_bootstrap: "yes"` is not `True`.

    The check is `is True` rather than truthiness, because a string, a `1` or a
    non-empty dict arriving from a JSON read must not be enough to authorize a delete.
    """
    access = FakePrerequisiteAccess()
    access.entry_created_by_bootstrap = "yes"  # type: ignore[assignment]
    access.rule_created_by_bootstrap = 1  # type: ignore[assignment]

    inventory, _ = _verify(target, access)

    assert all(item.ownership == ADOPTED for item in inventory.prerequisites)
    assert inventory.removable == ()


def test_a_caller_cannot_declare_ownership_through_the_gate(target):
    """There is no parameter for it. Ownership comes from the read, so the gate's
    signature offers no way to claim `adp-created` — which is why this is asserted
    against the signature rather than by attempting a call."""
    import inspect

    parameters = set(inspect.signature(verify_prerequisites).parameters)

    assert "ownership" not in parameters
    assert "inventory" not in parameters, (
        "the gate accepts a caller-supplied inventory, which would let the caller "
        "assert the very facts this gate exists to verify"
    )


# --- Attribution against Terraform's outputs, not caller parameters -------------


def test_an_expectation_naming_an_account_the_credential_is_not_in_is_refused(target):
    """The account comes from the provider's answer to "who am I".

    A caller-supplied account would let a rule in the wrong account verify against
    itself — the expectation and the observation would agree and both be wrong.
    """
    with pytest.raises(BootstrapRefused, match="refusing to attribute"):
        _verify(target, provider_account_id="999999999999")


def test_an_expectation_for_a_different_workspace_is_refused(target):
    """Terraform outputs from another workspace describe another cluster. Verifying
    against them would attribute this workspace's access to that one's VPC."""
    with pytest.raises(BootstrapRefused, match="different workspace"):
        _verify(
            target,
            expected=_expected(account_id="999999999999"),
            provider_account_id="999999999999",
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "account_id",
        "vpc_id",
        "cluster_security_group_id",
        "management_security_group_id",
        "protocol",
    ],
)
def test_no_field_of_the_published_expectation_may_be_blank(field_name):
    """A blank expectation compares equal to anything and would verify nothing — the
    same failure shape as the optional argument F4 found, one level down."""
    with pytest.raises(BootstrapRefused, match=f"ExpectedPrerequisites.{field_name}"):
        _expected(**{field_name: "  "})


def test_a_non_integer_port_expectation_is_refused():
    with pytest.raises(BootstrapRefused, match="must be an int"):
        _expected(api_server_port="443")  # type: ignore[arg-type]


def test_a_boolean_port_expectation_is_refused():
    """`True` is an `int` in Python and would pass an `isinstance` check, then compare
    equal to port 1. Explicitly excluded rather than left to chance."""
    with pytest.raises(BootstrapRefused, match="must be an int"):
        _expected(api_server_port=True)  # type: ignore[arg-type]


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_an_out_of_range_port_expectation_is_refused(port):
    with pytest.raises(BootstrapRefused, match="not a valid port"):
        _expected(api_server_port=port)


# --- The inventory is required downstream, too (registration and retirement) ----


def test_require_inventory_refuses_an_absent_inventory():
    """The F4 gate in one place, for the two callers that consume it. Registration must
    not publish a usable workspace whose access path was never attributed."""
    with pytest.raises(BootstrapRefused, match="prerequisite inventory is required"):
        require_inventory(None, workspace_id=WORKSPACE_ID, what="registration")


def test_require_inventory_refuses_another_workspaces_inventory(target):
    """Using it would act on another workspace's access — the same harm the mixed
    inventory constructor refuses, at the point of use."""
    inventory, _ = _verify(target)
    foreign = PrerequisiteInventory(
        workspace_id="another-workspace",
        prerequisites=tuple(
            type(item)(
                kind=item.kind,
                identifier=item.identifier,
                workspace_id="another-workspace",
                ownership=item.ownership,
                reason=item.reason,
            )
            for item in inventory.prerequisites
        ),
    )

    with pytest.raises(BootstrapRefused, match="belongs to workspace"):
        require_inventory(foreign, workspace_id=WORKSPACE_ID, what="retirement")


def test_require_inventory_names_the_operation_it_refused_for():
    """Two callers refuse through this one function, so the message has to say which —
    "registration" and "retirement" send an operator to different places."""
    with pytest.raises(BootstrapRefused, match="for retirement"):
        require_inventory(None, workspace_id=WORKSPACE_ID, what="retirement")


def test_require_inventory_accepts_a_matching_inventory(target):
    """The positive control for the gate above: it must not refuse everything."""
    inventory, _ = _verify(target)

    assert (
        require_inventory(inventory, workspace_id=WORKSPACE_ID, what="registration")
        is inventory
    )


# --- The operator-facing summary -------------------------------------------------


def test_the_summary_reports_preserved_entries_as_well_as_removable_ones(target):
    """An adopted rule an operator does not know about is the same accidental-permanence
    problem in a different place, so both lists are always reported."""
    inventory, _ = _verify(target)

    summary = summarize(inventory)

    assert summary["removable"] == ()
    assert len(summary["preserved"]) == len(REQUIRED_PREREQUISITE_KINDS)
    assert any(ACCESS_ENTRY in entry for entry in summary["preserved"])


def test_the_summary_carries_no_secret_material(target):
    """It is quoted in issue comments and completion reports, so it goes through the
    real contract screen — the same discipline the registration record follows."""
    inventory, _ = _verify(
        target,
        FakePrerequisiteAccess(
            entry_created_by_bootstrap=True, rule_created_by_bootstrap=True
        ),
    )

    summary = summarize(inventory)

    for group, entries in summary.items():
        for entry in entries:
            # The access entry identifier embeds the cluster ARN, which the contract
            # treats as secret-shaped and `registration.py` exempts by explicit field
            # name. Skipped here for the same reason and named for the same reason:
            # so widening the exemption stays a visible decision.
            if ACCESS_ENTRY in entry or "arn:" in entry:
                continue
            assert_no_secret_material(entry, what=f"summary.{group}")
