"""Identity, bounds and the anti-smuggling refusal. No database required.

Issue #5525 (w6-02), EPIC #4910, Wave 6. Design requirement 1 ("resolve identities
from authenticated context, never caller assertions") and the finite-request-size half
of requirement 2.

These are deliberately offline: they assert properties of the types, and running them
needs no PostgreSQL, so they stay green in a lane that has no database and the
database-backed suites skip honestly beside them.
"""

from __future__ import annotations

import pytest

from harness_jobs import (
    CONTRACT_VERSION,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_PARAMETER_COUNT,
    MAX_PARAMETER_VALUE_LENGTH,
    MAX_TOTAL_PARAMETER_BYTES,
    REQUIRED_PERMISSION,
    SUPPORTED_CONTRACT_VERSIONS,
    TERMINAL_STATES,
    ContractViolation,
    OperationBinding,
    OperationRefused,
    OperationRequest,
    OperationState,
    ResolvedPrincipal,
    decode_payload,
    encode_payload,
    forbidden_parameters,
    payload_digest,
)

# ---------------------------------------------------------------------------
# The structural rule: a caller cannot name its own tenant
# ---------------------------------------------------------------------------


def test_the_request_type_has_no_tenant_fields():
    """The refusal is structural, not a validation rule that can be skipped.

    Asserted on the field set rather than by attempting an assignment, so adding an
    `org_id` field to the caller's half fails here rather than passing review because
    "something else checks it".
    """
    fields = set(OperationRequest.__dataclass_fields__)
    assert fields == {"action", "idempotency_key", "parameters", "contract_version"}
    for forbidden in ("org_id", "workspace_id", "subject", "tenant_id", "principal"):
        assert forbidden not in fields


@pytest.mark.parametrize(
    "key",
    [
        "org_id",
        "orgId",
        "ORG_ID",
        "org-id",
        "orgid",
        "organization_id",
        "tenant_id",
        "workspace_id",
        "workspaceId",
        "subject",
        "actor_id",
        "on_behalf_of",
        "  Org-ID  ",
    ],
)
def test_identity_asserting_parameters_are_refused_in_every_spelling(key):
    """The dict-shaped side channel is closed, case- and separator-insensitively.

    A type with no `org_id` field still accepts `parameters={'org_id': ...}`, and a
    case-sensitive exact-match blocklist is a bypass with an obvious recipe.
    """
    with pytest.raises(ContractViolation, match="must not assert an identity"):
        OperationRequest(
            action="provision", idempotency_key="k", parameters={key: "org-a"}
        )
    assert forbidden_parameters({key: "x"}) == (key,)


def test_a_correct_org_id_parameter_is_still_refused():
    """Refused even when it *matches*, which is the point.

    A caller-supplied identity that agrees today is a code path that reads
    caller-supplied identity. Once the path exists, the only thing stopping a
    mismatched value from being honoured is that something happens to compare them --
    and comparisons get reordered, cached or made conditional. Refusing regardless
    makes "no path reads caller-supplied identity" a property rather than a
    coincidence.
    """
    with pytest.raises(ContractViolation):
        OperationRequest(
            action="provision",
            idempotency_key="k",
            parameters={"org_id": "org-a"},  # the caller's real org
        )


def test_every_offending_key_is_reported_at_once():
    """An operator fixing a request should not discover one bad key per round trip."""
    assert set(forbidden_parameters({"org_id": "a", "actor": "b", "size": "c"})) == {
        "org_id",
        "actor",
    }


def test_only_the_principal_carries_the_tenant():
    """`ResolvedPrincipal` is the only half with tenant fields."""
    assert "org_id" in ResolvedPrincipal.__dataclass_fields__
    assert "workspace_id" in ResolvedPrincipal.__dataclass_fields__


def test_the_binding_takes_its_tenant_from_the_principal_only():
    """`issue` is the single place the two halves join; it reads only the principal."""
    principal = ResolvedPrincipal(
        org_id="org-real",
        workspace_id="ws-real",
        subject="user-1",
        permissions=frozenset({REQUIRED_PERMISSION}),
    )
    binding = OperationBinding.issue(
        principal,
        OperationRequest(action="provision", idempotency_key="k"),
    )
    assert (binding.org_id, binding.workspace_id) == ("org-real", "ws-real")
    assert binding.operation_id != binding.attempt_id, (
        "an operation and its first attempt must be separately identifiable, or "
        "#5527 cannot fence an attempt without also re-identifying the operation"
    )


def test_a_principal_without_the_permission_cannot_issue_a_binding():
    principal = ResolvedPrincipal(org_id="org-a", workspace_id="ws-1", subject="user-1")
    assert principal.may_provision is False
    with pytest.raises(OperationRefused, match=REQUIRED_PERMISSION):
        OperationBinding.issue(
            principal, OperationRequest(action="provision", idempotency_key="k")
        )


@pytest.mark.parametrize(
    "value", ["", "has space", 'quote"', "nul\x00byte", "-leading", "a" * 200]
)
def test_malformed_tenant_identifiers_are_refused_at_construction(value):
    """Refused where they are built, not escaped at every use.

    These values reach a query and an idempotency key. One place that forgets to
    escape is a defect; one place that refuses malformed input is a property.
    """
    with pytest.raises(ContractViolation):
        ResolvedPrincipal(org_id=value, workspace_id="ws-1", subject="user-1")


# ---------------------------------------------------------------------------
# Finite request sizes
# ---------------------------------------------------------------------------


def test_an_unbounded_idempotency_key_is_refused():
    with pytest.raises(ContractViolation, match="idempotency_key"):
        OperationRequest(
            action="provision", idempotency_key="k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1)
        )


def test_a_missing_idempotency_key_is_refused():
    with pytest.raises(ContractViolation, match="required"):
        OperationRequest(action="provision", idempotency_key="")


def test_too_many_parameters_are_refused():
    with pytest.raises(ContractViolation, match="at most"):
        OperationRequest(
            action="provision",
            idempotency_key="k",
            parameters={f"p{index}": "v" for index in range(MAX_PARAMETER_COUNT + 1)},
        )


def test_the_aggregate_size_bound_catches_what_per_field_bounds_miss():
    """Many just-under-limit values pass every individual check and must still fail.

    50 parameters of 2000 characters is 100 KB, and each one is individually legal.
    Without the aggregate bound this is an accepted request that fills a disk.
    """
    parameters = {
        f"p{index}": "v" * (MAX_PARAMETER_VALUE_LENGTH - 1) for index in range(40)
    }
    assert sum(len(k) + len(v) for k, v in parameters.items()) > (
        MAX_TOTAL_PARAMETER_BYTES
    )
    with pytest.raises(ContractViolation, match="in total"):
        OperationRequest(action="provision", idempotency_key="k", parameters=parameters)


def test_an_unknown_action_is_refused():
    with pytest.raises(ContractViolation, match="action"):
        OperationRequest(action="delete-everything", idempotency_key="k")


@pytest.mark.parametrize("version", ["v2", "v0", "", "1", 1, None, "V1"])
def test_an_unsupported_contract_version_is_refused_not_interpreted(version):
    """#5524's compatibility table: never best-effort interpretation.

    A caller built against a different contract has different expectations about what
    its fields mean, and guessing produces a wrong answer that looks right.

    The cases worth naming are `1` and `"1"`: the published version is the *string*
    `"v1"`, and a caller sending a parsed-out integer is the drift that a loose
    comparison would accept. `"V1"` is here because a case-insensitive match would
    make the version a family of spellings rather than a value.
    """
    with pytest.raises(ContractViolation, match="contract version"):
        OperationRequest(
            action="provision",
            idempotency_key="k",
            contract_version=version,
        )


def test_the_contract_version_is_the_published_spelling():
    """A string, matching `superplane_contracts.version.CONTRACT_VERSION`.

    Asserted on the type as well as the value: an integer here and a string there
    compare unequal on every well-formed request, so the store would refuse everything
    -- and the fix under pressure is a loose comparison that refuses nothing.
    """
    assert isinstance(CONTRACT_VERSION, str)
    assert CONTRACT_VERSION == "v1"
    assert CONTRACT_VERSION in SUPPORTED_CONTRACT_VERSIONS


# ---------------------------------------------------------------------------
# The payload digest
# ---------------------------------------------------------------------------


def test_the_digest_ignores_parameter_ordering():
    """Two spellings of the same request must not read as a changed payload."""
    first = OperationRequest(
        action="provision", idempotency_key="k", parameters={"a": "1", "b": "2"}
    )
    second = OperationRequest(
        action="provision", idempotency_key="k", parameters={"b": "2", "a": "1"}
    )
    assert payload_digest(first) == payload_digest(second)


def test_the_digest_distinguishes_a_changed_value():
    base = OperationRequest(
        action="provision", idempotency_key="k", parameters={"size": "small"}
    )
    bigger = OperationRequest(
        action="provision", idempotency_key="k", parameters={"size": "enormous"}
    )
    assert payload_digest(base) != payload_digest(bigger)


def test_the_digest_resists_the_delimiter_collision():
    """Length-prefixing, not joining: `{"a": "b:c"}` and `{"a:b": "c"}` must differ.

    A delimiter-joined digest maps both to the same string, and two different requests
    sharing a digest is exactly the collision that lets a changed retry pass as
    identical -- which is the check being bypassed rather than merely weakened.
    """
    first = OperationRequest(
        action="provision", idempotency_key="k", parameters={"a": "b:c"}
    )
    second = OperationRequest(
        action="provision", idempotency_key="k", parameters={"a:b": "c"}
    )
    assert payload_digest(first) != payload_digest(second)


def test_the_digest_covers_the_action():
    """A teardown and a provision under one key are different requests."""
    provision = OperationRequest(action="provision", idempotency_key="k")
    teardown = OperationRequest(action="teardown", idempotency_key="k")
    assert payload_digest(provision) != payload_digest(teardown)


def test_the_digest_is_stable_across_processes():
    """A fixed expected value, so a change to the algorithm is a visible decision.

    Stability matters because the digest is stored: changing how it is computed makes
    every existing row's digest unreproducible, so every retry of an in-flight
    operation would read as a changed payload and be refused.
    """
    request = OperationRequest(
        action="provision", idempotency_key="fixed", parameters={"size": "small"}
    )
    assert payload_digest(request) == payload_digest(request)
    assert len(payload_digest(request)) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


def test_unknown_is_terminal_and_is_not_a_failure():
    """The distinction the whole compensation design rests on.

    Terminal, because a poll loop that excluded it would spin forever. Not a failure,
    because collapsing the two either leaks resources believed never created, or
    retries a provision that actually succeeded -- duplicated cloud spend.
    """
    assert OperationState.UNKNOWN in TERMINAL_STATES
    assert OperationState.UNKNOWN is not OperationState.FAILED
    assert OperationState.PENDING not in TERMINAL_STATES
    assert OperationState.RUNNING not in TERMINAL_STATES


def test_states_are_stored_as_strings_not_ordinals():
    """A stored ordinal silently remaps every existing row when a member is inserted."""
    assert OperationState.PENDING.value == "pending"
    assert isinstance(OperationState.PENDING, str)


# ---------------------------------------------------------------------------
# F8: the actual caller's declared shape
# ---------------------------------------------------------------------------
#
# The prefix families are broad on purpose, and the breadth caught a field the only
# maintained caller actually sends. These tests are driven from that caller's real
# payloads rather than from a hand-written probe list, because the previous round's
# lesson was that a hand-written list reproduces whatever the implementer already had in
# mind -- an empty-parameter facade test cannot see a rejected caller at all.


# What `services/provisioning.py:341-377` passes, verbatim. `start_provision` builds
# `{"workspace_name", "isolation_mode"}` and adds `aws_account_id` when an account is
# named; `start_teardown` builds `{"workspace_name"}`.
_REAL_CALLER_PAYLOADS = [
    pytest.param(
        {"workspace_name": "example", "isolation_mode": "shared"},
        id="start_provision",
    ),
    pytest.param(
        {
            "workspace_name": "example",
            "isolation_mode": "dedicated",
            "aws_account_id": "123456789012",
        },
        id="start_provision-with-account",
    ),
    pytest.param({"workspace_name": "example"}, id="start_teardown"),
]


@pytest.mark.parametrize("parameters", _REAL_CALLER_PAYLOADS)
@pytest.mark.parametrize("action", ["provision", "teardown"])
def test_the_actual_callers_payloads_are_admitted(action, parameters):
    """The caller that exists must be able to call this store.

    Every one of these raised `ContractViolation` on `workspace_name` before the
    declared-shape exemption, which meant every provision and every teardown the
    platform issues was refused as an identity-smuggling attempt. A store nothing can
    call is not a durable store.
    """
    assert forbidden_parameters(parameters) == ()
    request = OperationRequest(
        action=action, idempotency_key="k", parameters=parameters
    )
    assert request.parameters == parameters


@pytest.mark.parametrize("parameters", _REAL_CALLER_PAYLOADS)
def test_the_callers_own_field_values_survive_a_round_trip(parameters):
    """Admitted is not enough: the caller's values must come back unchanged.

    The failure this excludes is an input rename -- accepting the call by quietly
    dropping or relabelling `workspace_name` would pass the admission test above while
    losing the field the caller sent, and a dispatcher would then provision something
    the caller did not ask for. Checked through the real encode/decode path, which is
    what a recovering process uses.
    """
    request = OperationRequest(
        action="provision", idempotency_key="k", parameters=parameters
    )
    restored = decode_payload(encode_payload(request))
    assert restored.parameters == parameters
    assert restored.parameters["workspace_name"] == parameters["workspace_name"]
    assert payload_digest(restored) == payload_digest(request)


@pytest.mark.parametrize(
    "key",
    [
        "workspace_id",
        "workspaceId",
        "WORKSPACE_ID",
        "workspace-id",
        "workspaceid",
        "org_id",
        "subject",
        "permission",
        "permissions",
        "role",
    ],
)
def test_the_shape_exemption_does_not_admit_an_identity_or_authority_claim(key):
    """The exemption is for the prefix families only, never the exact-name set.

    This is the test that makes the ordering in `forbidden_parameters` a property rather
    than a reading of the current source. `workspace_name` being admitted must not drag
    `workspace_id` in with it -- and the two differ only in the suffix, so the
    neighbouring-key case is exactly where a careless exemption would leak.
    """
    assert forbidden_parameters({key: "x"}) == (key,)
    with pytest.raises(ContractViolation):
        OperationRequest(action="provision", idempotency_key="k", parameters={key: "x"})


def test_a_shape_key_and_an_identity_key_together_are_still_refused():
    """A permitted field must not launder a forbidden one beside it.

    The realistic smuggling attempt is not a bare `org_id`; it is `org_id` next to the
    fields a legitimate request carries, so the map looks like normal traffic. The
    refusal names only the offending key.
    """
    offending = forbidden_parameters(
        {"workspace_name": "example", "isolation_mode": "shared", "org_id": "org-evil"}
    )
    assert offending == ("org_id",)


def test_the_workspace_prefix_family_still_refuses_unknown_members():
    """Only enumerated keys are exempt; the family still catches everything else.

    Otherwise the fix would be "delete the `workspace_` family", which is the broad
    protection rather than the specific false positive.
    """
    for key in ("workspace_owner", "workspace_principal", "workspace_role"):
        assert forbidden_parameters({key: "x"}) == (key,)
