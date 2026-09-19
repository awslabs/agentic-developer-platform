"""Acceptance suite for the provider-executor credential delivery path.

Issue #5048 (U10), EPIC #4910. R8, A half — the offline acceptances (2-7).

## What this suite is for, and what it deliberately cannot establish

Every test here runs offline against a **mocked** trusted-delivery contract, because
B's contract does not exist in ADP. So this suite establishes that the executor
*behaves* correctly — refuses what it must refuse, materializes where it must, cleans
up, isolates per tenant, and reports the provider's reading rather than its own claim.

It does **not** establish that credential delivery works. R8 acceptance 1 — a real
credential-dependent read-only provider call made by the bound executor — needs a
named AWS account, a credential label, spend authorization and a named cleanup owner,
none of which the approved delivery plan supplies. `test_mock_is_recorded_as_a_mock`
and `test_live_acceptance_is_not_claimed_by_this_suite` exist so a green run here
cannot be mistaken for that. Per `acceptance-split.md` rule 2, mock success closes no
live criterion.

The negative tests are the point of the suite. Each one is a specific failure from the
story's blast-radius table, and each is written to fail if the guard is removed —
which is why several assert on *how* a refusal happened (no management call was made,
no DB session was touched) rather than only that an exception was raised. An exception
can be raised for the wrong reason and still turn a test green.
"""

from __future__ import annotations

import ast
import pathlib
import pickle
import shutil

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from superplane_contracts.connections import (
    CredentialReference,
    ValidationReport,
)
from superplane_contracts.delivery import (
    CREDENTIAL_MANAGEMENT_PERMISSION,
    DELIVERY_PERMISSION,
    EXTERNAL_SECRETS_REPLICATION_RETIRED,
    REVOCATION_LIMITATION,
    TRUSTED_DELIVERY_IS_MOCKED,
    DeliveryLease,
    DeliveryRefused,
    ExecutorIdentity,
    IsolationRoot,
    RevocationState,
    RunBinding,
    SecretMaterial,
    TrustedDeliveryChannel,
    assert_workload_environment,
    file_is_executor_only,
    management_credentials_in,
    refuse_external_secret_replication,
    restricted_materialization,
)
from superplane_contracts.delivery_executor import (
    DeliveryOutcome,
    DeliveryRequest,
    ProviderExecutor,
    forbidden_request_parameters,
    summarize,
)
from superplane_contracts.health import ContractViolation
from superplane_contracts.provisioning import ResolvedPrincipal

from _delivery_fixtures import (
    EXECUTOR_ID,
    FAKE_KEY_VALUE,
    LATER,
    NOW,
    OTHER_EXECUTOR_ID,
    OTHER_WORKSPACE,
    PROVIDER_ACCOUNT_ID,
    WORKSPACE,
    FailingOperation,
    RecordingChannel,
    RecordingOperation,
    SpyDatabaseSession,
    clock,
    expired_clock,
    make_binding,
    make_lease,
    make_reference,
    make_request,
)

_MODULE_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DELIVERY_SOURCE = _MODULE_ROOT / "contracts/superplane_contracts/delivery.py"
_EXECUTOR_SOURCE = _MODULE_ROOT / "contracts/superplane_contracts/delivery_executor.py"


# ---------------------------------------------------------------------------
# Fixtures. The doubles themselves live in `_delivery_fixtures.py` — see that
# module's docstring for why they are hand-written and why the channel offers the
# forbidden management surface rather than omitting it.
# ---------------------------------------------------------------------------


@pytest.fixture
def channel() -> RecordingChannel:
    """B's scoped trusted-delivery contract, mocked and recording every call."""
    return RecordingChannel()


@pytest.fixture
def operation() -> RecordingOperation:
    """A permitted read-only provider operation that inspects the credential file."""
    return RecordingOperation()


@pytest.fixture
def failing_operation() -> FailingOperation:
    """A provider operation that raises, for the cleanup-on-failure branch."""
    return FailingOperation()


@pytest.fixture
def arn_leaking_operation() -> RecordingOperation:
    """A provider whose own response embeds an ARN, as `sts get-caller-identity` does."""
    return RecordingOperation(
        observation={"arn": "arn:aws:iam::123456789012:user/delivery-test"}
    )


@pytest.fixture
def lease() -> DeliveryLease:
    """A lease issued to `EXECUTOR_ID`, in `WORKSPACE`, not yet expired."""
    return make_lease()


@pytest.fixture
def executor(channel: RecordingChannel, tmp_path: pathlib.Path) -> ProviderExecutor:
    """The executor the lease was issued to. `tmp_path` is the isolation base."""
    return ProviderExecutor(
        identity=ExecutorIdentity(executor_id=EXECUTOR_ID),
        channel=channel,
        isolation_base=tmp_path,
        clock=clock,
    )


@pytest.fixture
def unbound_executor(
    channel: RecordingChannel, tmp_path: pathlib.Path
) -> ProviderExecutor:
    """A *different* executor, holding a lease that was not issued to it."""
    return ProviderExecutor(
        identity=ExecutorIdentity(executor_id=OTHER_EXECUTOR_ID),
        channel=channel,
        isolation_base=tmp_path,
        clock=clock,
    )


@pytest.fixture
def expired_executor(
    channel: RecordingChannel, tmp_path: pathlib.Path
) -> ProviderExecutor:
    """The bound executor, running after every lease's expiry."""
    return ProviderExecutor(
        identity=ExecutorIdentity(executor_id=EXECUTOR_ID),
        channel=channel,
        isolation_base=tmp_path,
        clock=expired_clock,
    )


@pytest.fixture
def spy_db_session() -> SpyDatabaseSession:
    """A domain DB session that records any touch. Must stay untouched."""
    return SpyDatabaseSession()


# ---------------------------------------------------------------------------
# Acceptance: delivery is consumed from B's scoped contract, and from nothing else
# ---------------------------------------------------------------------------


def test_delivery_comes_from_the_lease_and_calls_no_management_endpoint(
    executor, channel, operation, lease
):
    """The credential arrives through the lease; no vault management call is made.

    This is the story's central assertion and it is deliberately not "an exception was
    not raised". `RecordingChannel` records every method invoked on it and offers the
    management surface (`register_credential`, `delete_credential`, `read_secret_value`)
    that a real vault client would — so if the executor reached for any of them, the
    call would be *recorded* rather than failing with AttributeError. A test whose
    double lacks the forbidden methods proves only that the double lacks them.
    """
    outcome = executor.run(lease, make_request(), operation)

    assert channel.calls == ["revocation_state", "fetch_material", "record_delivered"]
    assert channel.management_calls == []
    assert channel.raw_reads == []
    assert isinstance(outcome, DeliveryOutcome)


def test_executor_source_imports_no_http_or_secrets_client():
    """Structurally: the management endpoints are unreachable from the executor.

    The absence is asserted against the source's import graph rather than trusted from
    a docstring, following the precedent in `tools/superplane-mcp/tests/test_vault_client.py`.
    "We chose not to import it" stays true in prose while a later edit makes it false.

    `boto3` and `urllib.request` are forbidden because that is how a module acquires a
    raw secret read or a management call without anyone deciding to grant it.
    `sqlalchemy` is forbidden because it is how a domain-DB write arrives.
    """
    forbidden = (
        "urllib",
        "urllib.request",
        "http",
        "http.client",
        "requests",
        "httpx",
        "boto3",
        "botocore",
        "sqlalchemy",
        "vault_client",
        "superplane_mcp.vault_client",
        "vault_service",
        "credential_resolver",
        "vault_routes",
    )
    for source in (_DELIVERY_SOURCE, _EXECUTOR_SOURCE):
        imported = _imported_names(source)
        for name in imported:
            for bad in forbidden:
                assert not (name == bad or name.startswith(f"{bad}.")), (
                    f"{source.name} imports {name!r}: the executor must take its "
                    "credential from B's scoped delivery contract, not from an HTTP "
                    "call, a raw secret read or a database session"
                )


def test_management_permission_does_not_authorize_delivery():
    """A binding carrying `renew_credential` is refused at construction.

    Authority to register or rotate a credential is not authority to use it inside a
    run. Refusing at construction rather than at the authority check means such a
    binding cannot be assembled and passed around as if it authorized a delivery.
    """
    with pytest.raises(ContractViolation, match="does not authorize delivery"):
        RunBinding(
            operation_id="op-1",
            provider="aws",
            provider_account_id=PROVIDER_ACCOUNT_ID,
            operation="describe_regions",
            principal=ResolvedPrincipal(
                subject="s", org_id="o", workspace_id=WORKSPACE
            ),
            recipient=ExecutorIdentity(executor_id="exec-1"),
            permission=CREDENTIAL_MANAGEMENT_PERMISSION,
            expires_at=LATER,
        )


def test_delivery_permission_matches_the_domain_authorization_model():
    """The pinned permission strings are the ones U9's enum actually defines.

    Pinned as strings rather than imported (the two packages ship separately), so this
    test is what keeps them attached to the model: a rename upstream fails here instead
    of silently detaching the check. Same technique `test_connection_contract.py` uses
    for `RENEW_CREDENTIAL_PERMISSION`.
    """
    policy = _MODULE_ROOT / "auth/superplane_auth/policy.py"
    source = policy.read_text()
    assert f'PROVISION = "{DELIVERY_PERMISSION}"' in source
    assert f'RENEW_CREDENTIAL = "{CREDENTIAL_MANAGEMENT_PERMISSION}"' in source


def test_trusted_delivery_channel_offers_no_credential_read_by_id():
    """B's contract, as consumed here, cannot be asked for an arbitrary credential.

    A `read_credential(credential_id)` on this protocol would be a raw-read endpoint
    with extra steps: any caller naming an id would get a value. `fetch_material` takes
    the whole lease instead, so the scope travels with the request.

    Read from the protocol's own annotations and methods rather than from
    `__protocol_attrs__`, which is a CPython implementation detail: this suite must keep
    asserting the surface on whatever interpreter CI pins, and a test that vanishes into
    an AttributeError on a version bump is a check nobody notices losing.
    """
    surface = {
        name
        for name in vars(TrustedDeliveryChannel)
        if not name.startswith("_") and callable(getattr(TrustedDeliveryChannel, name))
    }
    assert surface == {"revocation_state", "fetch_material", "record_delivered"}


# ---------------------------------------------------------------------------
# Acceptance: binding and isolation refusals
# ---------------------------------------------------------------------------


def test_executor_not_bound_to_the_operation_is_refused(
    unbound_executor, operation, lease, channel
):
    """A valid lease issued to a *different* executor is refused.

    "Recipient-bound" is the property; a lease any executor could redeem is a bearer
    token for the credential. The assertion on `channel.calls` matters as much as the
    exception: the refusal must happen before any material is fetched, so a rejected
    executor never held the credential even briefly.
    """
    with pytest.raises(DeliveryRefused, match="not issued to this executor"):
        unbound_executor.run(lease, make_request(), operation)
    assert channel.calls == []
    assert operation.performed == []


def test_credential_bound_to_another_workspace_is_refused(executor, operation, channel):
    """A lease whose workspace is not the bound principal's is unconstructable.

    ADP's own handlers filter on `Workspace.org_id == org_id`, so possession inside an
    org is not a binding — the hole `connections.py` documents at length. Here the
    mismatch is refused at construction, so a cross-workspace lease cannot be built
    and handed to the executor at all.
    """
    with pytest.raises(ContractViolation, match="does not match the bound principal"):
        make_lease(workspace_id=OTHER_WORKSPACE, principal_workspace=WORKSPACE)
    assert channel.calls == []


@pytest.mark.parametrize(
    "candidate_request",
    [
        DeliveryRequest(provider="gcp", operation="describe_regions"),
        DeliveryRequest(provider="aws", operation="delete_project"),
    ],
)
def test_request_must_match_the_bound_provider_and_action(
    executor, operation, lease, channel, candidate_request
):
    """A lease is authority for one provider action, not a bearer credential."""
    with pytest.raises(DeliveryRefused, match="bound operation"):
        executor.run(lease, candidate_request, operation)
    assert channel.calls == []
    assert operation.performed == []


@pytest.mark.parametrize("key", ["account_id", "project-id", "subscription"])
def test_request_cannot_select_the_bound_provider_account(
    executor, operation, lease, channel, key
):
    """Provider account scope comes from B's binding, never request parameters."""
    request = make_request(parameters=((key, PROVIDER_ACCOUNT_ID),))
    with pytest.raises(DeliveryRefused, match="may not assert an identity"):
        executor.run(lease, request, operation)
    assert channel.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "gcp"),
        ("provider_account_id", "210987654321"),
        ("operation", "delete_project"),
    ],
)
def test_executable_operation_must_match_the_binding(
    executor, operation, lease, channel, field, value
):
    """A truthful request cannot smuggle a different executable adapter."""
    setattr(operation, field, value)
    with pytest.raises(DeliveryRefused, match="does not match the bound"):
        executor.run(lease, make_request(), operation)
    assert channel.calls == []
    assert operation.performed == []


def test_credential_provider_must_match_the_bound_provider():
    """An AWS binding cannot be paired with a GCP credential reference."""
    with pytest.raises(ContractViolation, match="credential provider"):
        DeliveryLease(
            lease_id="lease-cross-provider",
            reference=CredentialReference(
                credential_id="cred-gcp", service="gcp", label="delivery-test"
            ),
            workspace_id=WORKSPACE,
            binding=make_binding(),
        )


def test_body_supplied_identity_is_not_authority_even_when_it_matches(
    executor, operation, lease, channel
):
    """An `org_id` parameter that AGREES with the binding is still refused.

    The matching case is the important one. "It matched, so no harm done" is the
    argument that deletes this check, and once a path reads the caller's value the only
    thing preventing a mismatched value being honoured is that something else compares
    them — and comparisons get reordered, cached or made conditional.
    """
    matching = make_request(parameters=(("org_id", lease.binding.principal.org_id),))
    with pytest.raises(DeliveryRefused, match="may not assert an identity"):
        executor.run(lease, matching, operation)
    assert channel.calls == []


@pytest.mark.parametrize(
    "key",
    [
        "user_id",
        "org_id",
        "workspace_id",
        "ORG_ID",
        "org-id",
        "tenant",
        "on_behalf_of",
        "x-user-id",
        "caller_org",
    ],
)
def test_identity_smuggling_keys_are_refused(executor, operation, lease, key):
    """Identity keys are refused across the family, not just the ones spelled exactly.

    Parameterized over case and separator variants because a case-sensitive or
    exact-match-only check is a bypass with an obvious recipe.
    """
    request = make_request(parameters=((key, "anything"),))
    assert forbidden_request_parameters(request) == (key,)
    with pytest.raises(DeliveryRefused, match="may not assert an identity"):
        executor.run(lease, request, operation)


def test_expired_lease_is_refused_and_states_the_limitation(
    expired_executor, operation, lease, channel
):
    """An expired lease is refused, and the refusal says what expiry does not do.

    An operator reading "expired" without that sentence concludes the credential is
    contained, and therefore skips the provider-side revocation that would contain it.
    """
    with pytest.raises(DeliveryRefused) as raised:
        expired_executor.run(lease, make_request(), operation)
    assert "expired" in str(raised.value)
    assert "revoked at the provider" in str(raised.value)
    assert channel.calls == []


def test_a_duck_typed_lease_is_refused(executor, operation, lease):
    """A local object with the right attribute names is not a lease.

    This is the shape a caller reaches for to skip B's contract entirely: a small
    stand-in carrying a lease_id and a reference. Refusing it is what makes "authority
    comes from B's contract" enforceable rather than conventional.
    """

    class LookalikeLease:
        lease_id = "l-fake"
        reference = CredentialReference(credential_id="c-1", service="aws", label="l")
        workspace_id = WORKSPACE
        binding = lease.binding
        recipient = lease.recipient
        provenance = {"trusted_delivery": "live"}

    with pytest.raises(DeliveryRefused, match="must be a DeliveryLease"):
        executor.run(LookalikeLease(), make_request(), operation)  # type: ignore[arg-type]


def test_a_lease_requires_an_expiry():
    """Delivery authority with no expiry is unexpressible.

    U17a's `OperationBinding` leaves expiry optional because B published no expiry
    semantics for a provisioning operation. This diverges deliberately: R8 asks for a
    *short-lived* channel, and an unbounded credential delivery is the failure being
    fixed rather than a case to tolerate.
    """
    with pytest.raises(TypeError):
        RunBinding(  # type: ignore[call-arg]
            operation_id="op-1",
            provider="aws",
            provider_account_id=PROVIDER_ACCOUNT_ID,
            operation="describe_regions",
            principal=ResolvedPrincipal(
                subject="s", org_id="o", workspace_id=WORKSPACE
            ),
            recipient=ExecutorIdentity(executor_id="exec-1"),
            permission=DELIVERY_PERMISSION,
        )


# ---------------------------------------------------------------------------
# Acceptance: executor-only ephemeral filesystem, absent from every tool result
# ---------------------------------------------------------------------------


def test_credential_lands_in_an_executor_only_file(executor, operation, lease):
    """The provider reads the real credential from a file only its owner can read.

    Permissions are checked against the filesystem rather than against the mode
    argument passed to `os.open`: a mode is a request, and umask, ACLs and a
    pre-existing file can each make the result differ from the ask.
    """
    executor.run(lease, make_request(), operation)

    performed = operation.performed[0]
    assert performed["content"] == FAKE_KEY_VALUE
    assert performed["executor_only"] is True
    assert performed["parent_executor_only"] is True


def test_credential_file_is_removed_after_use(executor, operation, lease):
    """The file and its scratch directory are gone once the operation returns.

    A credential left behind is readable by the next tenant scheduled on the host —
    the "left on a shared filesystem" row of the story's blast-radius table.
    """
    executor.run(lease, make_request(), operation)

    path = operation.performed[0]["path"]
    assert not path.exists()
    assert not path.parent.exists()


def test_credential_is_removed_even_when_the_provider_raises(
    executor, lease, failing_operation
):
    """Cleanup does not depend on the happy path.

    A provider call that raises is the case where a naive implementation leaks the
    file, because the removal sat after the call rather than in a `finally`.
    """
    with pytest.raises(RuntimeError, match="provider exploded"):
        executor.run(lease, make_request(), failing_operation)

    assert failing_operation.path is not None
    assert not failing_operation.path.exists()
    assert not failing_operation.path.parent.exists()


def test_provider_sibling_files_are_removed_without_masking_success(executor, lease):
    """SDK cache siblings cannot strand the credential scratch directory."""

    class SiblingWritingOperation:
        path = None
        provider = "aws"
        provider_account_id = PROVIDER_ACCOUNT_ID
        operation = "describe_regions"

        def perform(self, credential_path, *, lease):
            self.path = credential_path
            (credential_path.parent / "sdk-cache").write_text("non-secret state")
            return {"regions": ["us-east-1"]}

    operation = SiblingWritingOperation()
    executor.run(lease, make_request(), operation)
    assert operation.path is not None
    assert not operation.path.parent.exists()


@pytest.mark.parametrize("raises", [False, True])
def test_provider_directory_mode_cannot_strand_the_credential_or_mask_result(
    executor, lease, raises
):
    """An SDK making its config directory read-only cannot defeat cleanup."""

    class ReadOnlyScratchOperation:
        path = None
        provider = "aws"
        provider_account_id = PROVIDER_ACCOUNT_ID
        operation = "describe_regions"

        def perform(self, credential_path, *, lease):
            self.path = credential_path
            credential_path.parent.chmod(0o500)
            if raises:
                raise RuntimeError("provider exploded after changing directory mode")
            return {"regions": ["us-east-1"]}

    operation = ReadOnlyScratchOperation()
    if raises:
        with pytest.raises(RuntimeError, match="provider exploded after changing"):
            executor.run(lease, make_request(), operation)
    else:
        outcome = executor.run(lease, make_request(), operation)
        assert outcome.provider_observation == {"regions": ["us-east-1"]}

    assert operation.path is not None
    assert not operation.path.exists()
    assert not operation.path.parent.exists()


@pytest.mark.parametrize("raises", [False, True])
def test_provider_removing_scratch_cannot_mask_result(executor, lease, raises):
    """A provider may consume the material by removing its whole scratch dir."""

    class ScratchRemovingOperation:
        path = None
        provider = "aws"
        provider_account_id = PROVIDER_ACCOUNT_ID
        operation = "describe_regions"

        def perform(self, credential_path, *, lease):
            self.path = credential_path
            shutil.rmtree(credential_path.parent)
            if raises:
                raise RuntimeError("provider exploded after consuming scratch")
            return {"regions": ["us-east-1"]}

    operation = ScratchRemovingOperation()
    if raises:
        with pytest.raises(RuntimeError, match="provider exploded after consuming"):
            executor.run(lease, make_request(), operation)
    else:
        outcome = executor.run(lease, make_request(), operation)
        assert outcome.provider_observation == {"regions": ["us-east-1"]}

    assert operation.path is not None
    assert not operation.path.exists()
    assert not operation.path.parent.exists()


def test_credential_appears_in_no_tool_result(executor, operation, lease):
    """Nothing a model can see carries the credential.

    Checked against the rendered tool result and the outcome's `repr`, not against the
    fields the test author remembered to look at — `repr` is how a value leaks into a
    log line or an exception message three frames up.
    """
    outcome = executor.run(lease, make_request(), operation)

    rendered = repr(outcome.tool_result()) + repr(outcome) + summarize(outcome)
    assert FAKE_KEY_VALUE not in rendered
    assert outcome.credential_present_in_result is False


def test_exact_credential_is_refused_even_under_an_innocuous_result_key(
    executor, operation, lease, channel
):
    """Leak prevention checks the delivered value, not only secret-shaped fields."""
    operation.observation = {"message": f"provider echoed {FAKE_KEY_VALUE}"}
    with pytest.raises(ContractViolation, match="contains the delivered credential"):
        executor.run(lease, make_request(), operation)
    assert channel.calls == ["revocation_state", "fetch_material"]
    assert not operation.performed[0]["path"].parent.exists()


def test_secret_material_refuses_to_render_itself():
    """`SecretMaterial` cannot be printed, formatted or interpolated into a string.

    The realistic leak is not `print(secret)`; it is an f-string in a log line or an
    exception that interpolates its context. A `str` cannot defend itself in either.
    """
    material = SecretMaterial(FAKE_KEY_VALUE)

    assert FAKE_KEY_VALUE not in repr(material)
    assert FAKE_KEY_VALUE not in str(material)
    assert FAKE_KEY_VALUE not in f"{material}"
    assert FAKE_KEY_VALUE not in f"{material:>40}"
    assert FAKE_KEY_VALUE not in "%s" % (material,)
    assert material.reveal() == FAKE_KEY_VALUE


def test_secret_material_cannot_be_serialized():
    """Pickling is refused: that is how a value reaches a queue, cache or transcript."""
    with pytest.raises(ContractViolation, match="cannot be serialized"):
        pickle.dumps(SecretMaterial(FAKE_KEY_VALUE))


def test_secret_material_is_not_hashable():
    """A hashable secret can be a dict key, and dict keys get logged when the dict does."""
    with pytest.raises(TypeError):
        {SecretMaterial(FAKE_KEY_VALUE): "value"}  # noqa: B018


def test_a_bare_string_from_the_channel_is_refused(executor, operation, lease, channel):
    """A channel handing back a plain `str` instead of `SecretMaterial` is refused.

    Not pedantry: accepting one would silently give up the non-rendering property, and
    the give-up would be invisible at every later call site.
    """
    channel.material = FAKE_KEY_VALUE  # type: ignore[assignment]
    with pytest.raises(ContractViolation, match="not a bare value"):
        executor.run(lease, make_request(), operation)


def test_provider_observation_is_scrubbed_before_it_reaches_the_outcome(
    executor, lease, arn_leaking_operation
):
    """An ARN in the provider's own response does not survive into a tool result.

    A provider response is untrusted input for this purpose — `sts get-caller-identity`
    returns an ARN as a matter of course. `secrets.py` treats an ARN as secret material
    because an ARN plus any over-broad IAM policy completes the read, and it survives
    rotation.
    """
    outcome = executor.run(lease, make_request(), arn_leaking_operation)

    assert "arn:aws:iam::" not in repr(outcome.tool_result())
    assert "arn:aws:iam::" not in repr(outcome)


# ---------------------------------------------------------------------------
# Acceptance: rotation and revocation, with the limitation surfaced
# ---------------------------------------------------------------------------


def test_rotated_credential_is_picked_up_through_the_reference(
    executor, operation, channel
):
    """The executor holds no standing credential, so a rotation takes effect.

    Two runs under leases naming different credential ids, with the channel returning
    different material for each. The second run must see the new value: a cached
    credential is one no rotation reaches, which is why the executor is frozen and
    stores nothing.
    """
    first = make_lease(credential_id="cred-old")
    channel.material_by_credential = {
        "cred-old": SecretMaterial("OLD-KEY-VALUE"),
        "cred-new": SecretMaterial("NEW-KEY-VALUE"),
    }
    executor.run(first, make_request(), operation)
    assert operation.performed[-1]["content"] == "OLD-KEY-VALUE"

    rotated = make_lease(credential_id="cred-new")
    executor.run(rotated, make_request(), operation)
    assert operation.performed[-1]["content"] == "NEW-KEY-VALUE"


def test_executor_caches_no_credential_between_runs(
    executor, operation, lease, channel
):
    """Material is re-fetched on every run, so revocation is re-checked at the operation.

    Asserted by counting fetches rather than by inspecting attributes: an executor that
    cached would satisfy an attribute check by storing the value somewhere unexpected.
    """
    executor.run(lease, make_request(), operation)
    executor.run(lease, make_request(), operation)

    assert channel.calls.count("fetch_material") == 2
    assert channel.calls.count("revocation_state") == 2


def test_disabled_credential_blocks_admission_and_states_the_limitation(
    executor, operation, lease, channel
):
    """A disabled credential is refused AND the response carries the limitation.

    Both halves are the acceptance. An operator who believes disablement was
    containment will not perform the provider-side revocation that actually is.
    """
    channel.revocation = RevocationState(
        admits_work=False, limitation=REVOCATION_LIMITATION
    )

    with pytest.raises(DeliveryRefused) as raised:
        executor.run(lease, make_request(), operation)

    message = str(raised.value)
    assert "no longer admits work" in message
    assert "revoked at the provider" in message
    assert channel.calls == ["revocation_state"]


def test_revocation_state_cannot_deny_without_stating_the_limitation():
    """A denial with no limitation is unconstructable, so the message cannot be dropped."""
    with pytest.raises(
        ContractViolation, match="must surface its revocation limitation"
    ):
        RevocationState(admits_work=False)


def test_a_successful_outcome_also_carries_the_limitation(executor, operation, lease):
    """The limitation travels with successes too.

    An operator reading a success is exactly the operator about to believe that
    expiring the lease contained the key.
    """
    outcome = executor.run(lease, make_request(), operation)
    assert "revoked at the provider" in outcome.limitation


def test_revocation_is_read_from_the_channel_not_from_the_lease(
    executor, operation, lease, channel
):
    """Authority is re-checked at the operation, against B's record.

    A lease is a value the caller holds; a credential disabled after the lease was
    issued still looks fine on it. So the check must be a call, and it must come first.
    """
    channel.revocation = RevocationState(
        admits_work=False, limitation=REVOCATION_LIMITATION
    )
    with pytest.raises(DeliveryRefused):
        executor.run(lease, make_request(), operation)
    assert channel.calls[0] == "revocation_state"


def test_a_malformed_revocation_report_raises_rather_than_authorizing(
    executor, operation, lease, channel
):
    """A channel returning the wrong type is a contract breach, not an authorization.

    Read as `.admits_work` off an arbitrary object, a missing attribute raises but a
    truthy one silently authorizes — so the type is checked instead.
    """
    channel.revocation = {"admits_work": True}  # type: ignore[assignment]
    with pytest.raises(ContractViolation, match="not a RevocationState"):
        executor.run(lease, make_request(), operation)


# ---------------------------------------------------------------------------
# Acceptance: no domain-DB write, asserted on the session
# ---------------------------------------------------------------------------


def test_no_direct_domain_db_write_occurs(executor, operation, lease, spy_db_session):
    """An assertion on the DB session, not a comment.

    The session is a spy that records every attribute touched and raises on use. A run
    that reached for it would show up here; the executor has no field to hold it, so
    nothing does. Domain records are the upstream API's to write.
    """
    executor.run(lease, make_request(), operation)

    assert spy_db_session.touched == []
    assert spy_db_session.commits == 0


def test_executor_has_no_database_handle(executor):
    """Structurally: there is no attribute on the executor a session could occupy."""
    fields = set(type(executor).__dataclass_fields__)
    assert fields == {"identity", "channel", "isolation_base", "clock"}
    for suspicious in ("session", "db", "engine", "connection", "cursor"):
        assert not hasattr(executor, suspicious)


# ---------------------------------------------------------------------------
# Acceptance: per-tenant/workspace/provider isolation
# ---------------------------------------------------------------------------


def test_two_tenants_share_no_sdk_state(tmp_path):
    """No shared SkyPilot home, cache or backend state across tenants.

    A shared `~/.sky` is the concrete failure: it holds cluster state and credentials,
    so one tenant's `sky status` enumerates the other's clusters. Every derived path is
    compared, not just the credential directory.
    """
    left = IsolationRoot(
        base=tmp_path,
        org_id="org-a",
        workspace_id="ws-a",
        provider="aws",
        provider_account_id=PROVIDER_ACCOUNT_ID,
    )
    right = IsolationRoot(
        base=tmp_path,
        org_id="org-b",
        workspace_id="ws-b",
        provider="aws",
        provider_account_id=PROVIDER_ACCOUNT_ID,
    )

    assert not left.shares_state_with(right)
    for attribute in ("sdk_home", "credentials_dir", "cache_dir", "backend_state_dir"):
        assert getattr(left, attribute) != getattr(right, attribute)


@pytest.mark.parametrize(
    ("org", "workspace", "provider", "provider_account_id"),
    [
        ("org-b", "ws-a", "aws", PROVIDER_ACCOUNT_ID),
        ("org-a", "ws-b", "aws", PROVIDER_ACCOUNT_ID),
        ("org-a", "ws-a", "gcp", PROVIDER_ACCOUNT_ID),
        ("org-a", "ws-a", "aws", "210987654321"),
    ],
)
def test_every_scope_component_separates_state(
    tmp_path, org, workspace, provider, provider_account_id
):
    """Changing any one of tenant, workspace or provider yields a distinct scope.

    Parameterized so isolation is a property of the full scope rather than of the two
    roots a test happened to construct. Provider and provider-account changes each
    matter on their own: two accounts under one workspace must not share state.
    """
    base = IsolationRoot(
        base=tmp_path,
        org_id="org-a",
        workspace_id="ws-a",
        provider="aws",
        provider_account_id=PROVIDER_ACCOUNT_ID,
    )
    other = IsolationRoot(
        base=tmp_path,
        org_id=org,
        workspace_id=workspace,
        provider=provider,
        provider_account_id=provider_account_id,
    )

    assert not base.shares_state_with(other)


def test_sdk_environment_points_every_state_path_into_the_scope(tmp_path):
    """`HOME` is set alongside `SKYPILOT_DIR`, because SkyPilot resolves `~/.sky` from HOME.

    Setting only the explicit SkyPilot variable leaves a code path that writes to a
    shared home, which is the failure with the friendliest-looking config.
    """
    root = IsolationRoot(
        base=tmp_path,
        org_id="org-a",
        workspace_id="ws-a",
        provider="aws",
        provider_account_id=PROVIDER_ACCOUNT_ID,
    )
    environment = root.sdk_environment()

    assert {
        "HOME",
        "SKYPILOT_DIR",
        "SKYPILOT_STATE_DIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
    } <= set(environment)
    for value in environment.values():
        assert str(root.scope_path) in value


def test_isolation_scope_comes_from_the_binding_not_the_caller(executor, lease):
    """A caller cannot choose the tenant directory its credential lands in.

    The scope triple is read from the lease's binding. A caller-chosen scope directory
    would be a caller-chosen tenant boundary.
    """
    root = executor.isolation_root_for(lease)

    assert root.org_id == lease.binding.principal.org_id
    assert root.workspace_id == lease.workspace_id
    assert root.provider == lease.binding.provider
    assert root.provider_account_id == lease.binding.provider_account_id


@pytest.mark.parametrize("component", ["../escape", "with/slash", ".", ".."])
def test_scope_components_cannot_traverse(tmp_path, component):
    """A tenant id of `../other-tenant` would resolve into a sibling's directory."""
    with pytest.raises(ContractViolation, match="cannot contain a path separator"):
        IsolationRoot(
            base=tmp_path,
            org_id=component,
            workspace_id="ws",
            provider="aws",
            provider_account_id=PROVIDER_ACCOUNT_ID,
        )


def test_two_tenants_materializing_concurrently_cannot_read_each_other(tmp_path):
    """Two live materializations under different scopes are mutually unreadable.

    Exercises the directory permissions rather than only the path difference: distinct
    paths under a world-readable parent would still be a cross-tenant read.
    """
    left = IsolationRoot(
        base=tmp_path,
        org_id="org-a",
        workspace_id="ws-a",
        provider="aws",
        provider_account_id=PROVIDER_ACCOUNT_ID,
    )
    right = IsolationRoot(
        base=tmp_path,
        org_id="org-b",
        workspace_id="ws-b",
        provider="aws",
        provider_account_id=PROVIDER_ACCOUNT_ID,
    )

    with restricted_materialization(
        SecretMaterial("LEFT-KEY"), root=left, filename="credentials"
    ) as left_path:
        with restricted_materialization(
            SecretMaterial("RIGHT-KEY"), root=right, filename="credentials"
        ) as right_path:
            assert left_path != right_path
            assert file_is_executor_only(left_path)
            assert file_is_executor_only(right_path)
            assert file_is_executor_only(left_path.parent)
            assert left_path.read_text() == "LEFT-KEY"
            assert right_path.read_text() == "RIGHT-KEY"


def test_materialization_refuses_a_filename_that_is_a_path(tmp_path):
    """A filename with a separator would write outside the scope's directory."""
    root = IsolationRoot(
        base=tmp_path,
        org_id="org-a",
        workspace_id="ws-a",
        provider="aws",
        provider_account_id=PROVIDER_ACCOUNT_ID,
    )
    with pytest.raises(ContractViolation, match="bare filename"):
        with restricted_materialization(
            SecretMaterial("KEY"), root=root, filename="../creds"
        ):
            pass


# ---------------------------------------------------------------------------
# Acceptance: management keys absent from training/inference containers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SESSION_TOKEN",
        "ADP_VAULT_TOKEN",
        "adp_vault_admin_key",
        "KUBECONFIG",
        "VAULT_ADDR",
    ],
)
def test_management_keys_are_refused_in_a_workload_environment(key):
    """A training/inference container carrying provider-management authority is refused.

    That container runs tenant workload code, and a management key there can register,
    rotate or delete every credential the key can see.
    """
    assert management_credentials_in({key: "x"}) == (key,)
    with pytest.raises(ContractViolation, match="may not carry provider-management"):
        assert_workload_environment({key: "x"})


def test_least_privileged_workload_credentials_are_permitted():
    """Dataset and MLflow access uses separate, least-privileged credentials.

    The negative test above is only meaningful with this one: a check that refused
    everything would pass it while making the workload environment unusable.
    """
    workload = {
        "MLFLOW_TRACKING_URI": "https://mlflow.internal",
        "MLFLOW_TRACKING_TOKEN_FILE": "/var/run/workload/mlflow",
        "DATASET_S3_PREFIX": "s3://bucket/prefix",
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/secrets/token",
        "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/workload",
    }
    assert management_credentials_in(workload) == ()
    assert_workload_environment(workload)


# ---------------------------------------------------------------------------
# Acceptance: the broad ExternalSecrets replication pattern is retired
# ---------------------------------------------------------------------------


def test_external_secrets_replication_is_retired_not_extended():
    """The retired pattern refuses rather than returning a success flag.

    Its real problem was that calling it *looked like it worked*: the reviewed
    `KubernetesExternalSecretClient` built a manifest, applied nothing, and returned
    `{"synced": True}`. A refusal naming the replacement is the retirement.
    """
    assert EXTERNAL_SECRETS_REPLICATION_RETIRED is True
    with pytest.raises(ContractViolation) as raised:
        refuse_external_secret_replication(
            namespace="tenant-a", secret_name="provider-creds"
        )

    message = str(raised.value)
    assert "retired" in message
    assert "recipient-bound lease" in message


def test_no_manifest_building_or_synced_flag_survives_in_this_path():
    """Neither module builds a Secret manifest or reports a `synced` flag.

    The acceptance is "replaced or retired, **not extended**", so this asserts the
    replacement did not quietly reintroduce the shape it replaced.

    Checked against **code** rather than raw source text: both modules quote the
    retired pattern in prose, on purpose, because a reader needs to know which defect
    the replacement exists for. A grep over the file text would therefore fail on the
    documentation and pass on a `synced` key added later inside a triple-quoted
    string — exactly backwards. So docstrings are stripped and the remainder is
    inspected for the manifest vocabulary and for any name a status flag could take.
    """
    for source in (_DELIVERY_SOURCE, _EXECUTOR_SOURCE):
        tree = ast.parse(source.read_text())
        literals = _string_literals_outside_docstrings(tree)
        names = _assigned_and_attribute_names(tree)

        for banned in ("synced", "apiVersion", "ExternalSecret", "kind: Secret"):
            assert not any(banned in literal for literal in literals), (
                f"{source.name} has {banned!r} in a code literal: the broad secret-"
                "replication pattern is retired, not extended"
            )
        for banned in ("synced", "delivered_ok", "sync_status"):
            assert banned not in names, (
                f"{source.name} defines {banned!r}: delivery evidence is the "
                "provider's observation, never a flag this code sets"
            )


def test_the_outcome_type_has_no_status_flag():
    """`DeliveryOutcome` exposes no `synced`/`ok`/`delivered` boolean.

    Success is the provider's observation, not a field this executor set. A status flag
    here would be the replaced defect with a new name.
    """
    fields = set(DeliveryOutcome.__dataclass_fields__)
    for banned in ("synced", "ok", "delivered", "success", "healthy", "ready"):
        assert banned not in fields
        assert not hasattr(DeliveryOutcome, banned)


def test_a_secret_existing_somewhere_is_not_evidence_of_delivery(
    executor, operation, lease
):
    """Delivery evidence is the provider's response, not the presence of a written file.

    The story is explicit that a Secret object existing in a cluster shows something was
    written — not that the credential is the exactly-bound one, reached an authorized
    recipient, or works. The outcome therefore carries what the provider said, and the
    credential file is gone by the time the outcome exists.
    """
    outcome = executor.run(lease, make_request(), operation)

    assert outcome.provider_observation == {"regions": ["us-east-1"]}
    assert not operation.performed[0]["path"].exists()


def test_validation_report_is_not_delivery_evidence():
    """A validated credential is not a delivered one.

    `ValidationReport` says the credential authenticates; it says nothing about whether
    material reached an executor. Asserted because "validated" is the nearest existing
    green signal and therefore the one most likely to be mistaken for delivery.
    """
    report = ValidationReport(
        credential_valid=True,
        permissions_sufficient=True,
        quota_available=True,
        observed_capacity=4,
        checked_at=NOW,
    )
    assert report.validated is True
    assert not hasattr(report, "delivered")
    assert "delivered" not in {f for f in ValidationReport.__dataclass_fields__}


# ---------------------------------------------------------------------------
# Recorded mock (acceptance-split rule 2 and 5)
# ---------------------------------------------------------------------------


def test_mock_is_recorded_as_a_mock(executor, operation, lease):
    """Every outcome says the trusted-delivery contract was mocked.

    A mock returning plausible values with no marker is indistinguishable from a live
    reading to whoever consumes it — the reason `vault_client.py` records
    `EXACT_BINDING_IS_MOCKED` and U17a records its facade mock.
    """
    assert TRUSTED_DELIVERY_IS_MOCKED is True

    outcome = executor.run(lease, make_request(), operation)

    assert outcome.is_mocked is True
    assert outcome.tool_result()["trusted_delivery"] == "mock"
    assert "not live" in summarize(outcome)


def test_a_lease_claiming_liveness_cannot_upgrade_a_mocked_run(executor, operation):
    """A caller-constructed lease asserting `live` provenance does not make a run live.

    That claim is exactly what would turn a mocked run into a reported live acceptance,
    so `TRUSTED_DELIVERY_IS_MOCKED` wins over anything the lease says.
    """
    claiming = make_lease(provenance={"trusted_delivery": "live"})

    outcome = executor.run(claiming, make_request(), operation)

    assert outcome.is_mocked is True
    assert outcome.tool_result()["trusted_delivery"] == "mock"


def test_live_acceptance_is_not_claimed_by_this_suite():
    """R8 acceptance 1 is not closed by anything in this file.

    Stated as a test so a green suite cannot be read as live delivery. The live
    criterion needs a named account, a credential label, spend authorization and a
    named cleanup owner; none is supplied, so no test here performs a real provider
    call. `superplane_live` is the marker the CI lane excludes, and no test in this
    module carries it — the live half runs separately, with explicit inputs.
    """
    tree = ast.parse(pathlib.Path(__file__).read_text())
    markers = {
        decorator.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
    }
    assert "superplane_live" not in markers


# ---------------------------------------------------------------------------
# Malformed-input guards
#
# Every refusal branch in both modules has a test here. That is not coverage
# theatre: an unexercised guard is one a later edit can delete with the suite
# staying green, and each of these guards exists because the shape it rejects
# would otherwise be accepted as authority. The `ContractViolation`-vs-
# `DeliveryRefused` split is asserted too — a malformed shape and a well-formed
# request the executor will not run are different things to a caller handling them.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", None, 42, b"AKIAIOSFODNN7EXAMPLE"])
def test_secret_material_requires_a_non_empty_string(value):
    """An empty or non-string value is refused rather than wrapped.

    A `SecretMaterial("")` would be written to the credential file as an empty file,
    and the provider call would then fail with an authentication error that reads like
    a bad credential rather than like a delivery bug. The `bytes` case is included
    because a secrets backend returning `SecretBinary` is the realistic way a
    non-`str` arrives, and `bytes` is truthy.
    """
    with pytest.raises(ContractViolation, match="non-empty string"):
        SecretMaterial(value)


def test_secret_material_accepts_a_value_that_is_only_whitespace():
    """Whitespace is not empty. A credential is opaque and this type does not judge it.

    The positive control for the guard above: a check that rejected whitespace would
    pass the negative tests while refusing a legitimate value, and "the credential was
    rejected before it was ever used" is a delivery failure with a misleading message.
    """
    assert SecretMaterial("   ").reveal() == "   "


def test_executor_identity_requires_an_id():
    """An executor with a blank id would make every lease's recipient check pass."""
    for blank in ("", "   "):
        with pytest.raises(ContractViolation, match="requires an executor_id"):
            ExecutorIdentity(executor_id=blank)


def test_run_binding_requires_a_resolved_principal_and_a_named_recipient():
    """A binding must carry server-resolved identity and name its recipient.

    Both are refused with their own messages because they are different mistakes: a
    caller-assembled principal is smuggled identity, while a missing recipient is a
    lease any executor could redeem.
    """
    good_principal = ResolvedPrincipal(subject="s", org_id="o", workspace_id=WORKSPACE)
    with pytest.raises(ContractViolation, match="must carry a resolved principal"):
        RunBinding(
            operation_id="op-1",
            provider="aws",
            provider_account_id=PROVIDER_ACCOUNT_ID,
            operation="describe_regions",
            principal={"subject": "s", "org_id": "o", "workspace_id": WORKSPACE},  # type: ignore[arg-type]
            recipient=ExecutorIdentity(executor_id=EXECUTOR_ID),
            permission=DELIVERY_PERMISSION,
            expires_at=LATER,
        )
    with pytest.raises(ContractViolation, match="must name its recipient executor"):
        RunBinding(
            operation_id="op-1",
            provider="aws",
            provider_account_id=PROVIDER_ACCOUNT_ID,
            operation="describe_regions",
            principal=good_principal,
            recipient="executor-1",  # type: ignore[arg-type]
            permission=DELIVERY_PERMISSION,
            expires_at=LATER,
        )


def test_run_binding_requires_an_operation_id():
    """An empty operation_id would bind every delivery and every revocation to the
    same empty target — so revoking one run would revoke all of them, or none."""
    with pytest.raises(ContractViolation, match="must carry an operation_id"):
        RunBinding(
            operation_id="   ",
            provider="aws",
            provider_account_id=PROVIDER_ACCOUNT_ID,
            operation="describe_regions",
            principal=ResolvedPrincipal(
                subject="s", org_id="o", workspace_id=WORKSPACE
            ),
            recipient=ExecutorIdentity(executor_id=EXECUTOR_ID),
            permission=DELIVERY_PERMISSION,
            expires_at=LATER,
        )


def test_run_binding_refuses_an_unrelated_permission():
    """A permission that is neither delivery nor management is still not delivery.

    The management permission gets its own named refusal (tested above). This is the
    general case, and it matters because "some permission was present" is the check a
    reimplementation would write.
    """
    with pytest.raises(ContractViolation, match="delivery requires"):
        RunBinding(
            operation_id="op-1",
            provider="aws",
            provider_account_id=PROVIDER_ACCOUNT_ID,
            operation="describe_regions",
            principal=ResolvedPrincipal(
                subject="s", org_id="o", workspace_id=WORKSPACE
            ),
            recipient=ExecutorIdentity(executor_id=EXECUTOR_ID),
            permission="workspace:read",
            expires_at=LATER,
        )


def test_run_binding_refuses_a_naive_expiry():
    """A naive expiry compares wrongly against an aware clock.

    Whether it raises or silently mis-compares depends on Python's version and the
    comparison direction, and "the expiry check threw a TypeError at 3am" is the
    good outcome. Refused at construction instead.
    """
    with pytest.raises(ContractViolation, match="must be timezone-aware"):
        RunBinding(
            operation_id="op-1",
            provider="aws",
            provider_account_id=PROVIDER_ACCOUNT_ID,
            operation="describe_regions",
            principal=ResolvedPrincipal(
                subject="s", org_id="o", workspace_id=WORKSPACE
            ),
            recipient=ExecutorIdentity(executor_id=EXECUTOR_ID),
            permission=DELIVERY_PERMISSION,
            expires_at=NOW.replace(tzinfo=None),
        )


def test_expiry_check_refuses_a_naive_now(lease):
    """`is_expired` will not answer against a naive clock either.

    The same reason as the field guard: an aware/naive comparison is where an expiry
    check quietly stops meaning what it says.
    """
    with pytest.raises(ContractViolation, match="now must be timezone-aware"):
        lease.binding.is_expired(NOW.replace(tzinfo=None))


@pytest.mark.parametrize("field_name", ["lease_id", "workspace_id"])
def test_lease_requires_its_identifiers(field_name):
    """A lease with a blank id or workspace is unconstructable.

    The workspace case is the one with teeth: a blank workspace on a lease would make
    the construction-time cross-workspace check compare `""` against `""` for any
    principal whose workspace was also blank.
    """
    fields = {
        "lease_id": "lease-1",
        "reference": make_reference(),
        "workspace_id": WORKSPACE,
        "binding": make_binding(),
    }
    fields[field_name] = "  "

    with pytest.raises(ContractViolation, match=f"DeliveryLease.{field_name}"):
        DeliveryLease(**fields)


def test_lease_refuses_a_reference_or_binding_of_the_wrong_type():
    """The lease's two authority-bearing fields cannot be duck-typed.

    A dict here is the shape a caller assembles when it has the ids but not the
    contract — which is precisely the case that must not produce a usable lease.
    """
    with pytest.raises(ContractViolation, match="must be a CredentialReference"):
        DeliveryLease(
            lease_id="lease-1",
            reference={"credential_id": "cred-1"},  # type: ignore[arg-type]
            workspace_id=WORKSPACE,
            binding=make_binding(),
        )
    with pytest.raises(ContractViolation, match="must be a RunBinding"):
        DeliveryLease(
            lease_id="lease-1",
            reference=make_reference(),
            workspace_id=WORKSPACE,
            binding={"operation_id": "op-1"},  # type: ignore[arg-type]
        )


def test_revocation_state_admits_work_must_be_a_boolean():
    """A truthy non-boolean would authorize by accident.

    `admits_work="no"` is truthy. Checked with `type(...) is not bool` rather than
    `isinstance` so a numpy bool or a 1 does not slip through as authority.
    """
    with pytest.raises(ContractViolation, match="must be a boolean"):
        RevocationState(admits_work="yes")  # type: ignore[arg-type]


def test_delivery_request_requires_a_provider_and_an_operation():
    """A blank provider would place the credential in a scope directory named ''."""
    for kwargs in ({"provider": "  "}, {"operation": "  "}):
        payload = {"provider": "aws", "operation": "describe_regions", **kwargs}
        with pytest.raises(ContractViolation, match="is required"):
            DeliveryRequest(**payload)  # type: ignore[arg-type]


def test_delivery_request_parameters_must_be_immutable_string_pairs():
    """A mutable or malformed parameter set is refused.

    Immutability is the point: a dict could be mutated between the identity check and
    the provider call, which would make the check advisory.
    """
    with pytest.raises(ContractViolation, match="immutable string pairs"):
        DeliveryRequest(
            provider="aws",
            operation="describe_regions",
            parameters=[("region", "us-east-1")],  # type: ignore[arg-type]
        )
    with pytest.raises(ContractViolation, match="immutable string pairs"):
        DeliveryRequest(
            provider="aws",
            operation="describe_regions",
            parameters=(("region", 1),),  # type: ignore[arg-type]
        )


def test_delivery_request_refuses_a_duplicate_parameter():
    """Two values for one key make the effective value depend on iteration order."""
    with pytest.raises(ContractViolation, match="duplicate delivery parameter"):
        DeliveryRequest(
            provider="aws",
            operation="describe_regions",
            parameters=(("region", "us-east-1"), ("region", "us-west-2")),
        )


def test_outcome_requires_a_mapping_observation_and_a_limitation():
    """The outcome cannot be built from a bool, nor with its limitation stripped.

    The bool case is the replaced defect trying to come back in through the
    constructor; the empty-limitation case is the surfacing acceptance being dropped.
    """
    with pytest.raises(ContractViolation, match="must be a mapping"):
        DeliveryOutcome(
            lease_id="lease-1",
            operation_id="op-1",
            provider="aws",
            operation="describe_regions",
            provider_observation=True,  # type: ignore[arg-type]
            credential=SecretMaterial(FAKE_KEY_VALUE),
        )
    with pytest.raises(ContractViolation, match="must surface its limitation"):
        DeliveryOutcome(
            lease_id="lease-1",
            operation_id="op-1",
            provider="aws",
            operation="describe_regions",
            provider_observation={},
            credential=SecretMaterial(FAKE_KEY_VALUE),
            limitation="",
        )


def test_a_non_request_is_refused(executor, operation, lease):
    """A raw dict in place of a `DeliveryRequest` skips every field guard above."""
    with pytest.raises(DeliveryRefused, match="must be a DeliveryRequest"):
        executor.run(lease, {"provider": "aws"}, operation)  # type: ignore[arg-type]


def test_a_non_mapping_provider_observation_is_refused(executor, lease, channel):
    """A provider returning a bare bool is refused, not recorded as success.

    This is the replaced defect at its source: a provider adapter that reports
    `True` gives the outcome nothing checkable, so it is rejected rather than stored.
    """

    class BooleanOperation:
        is_mock = True
        provider = "aws"
        provider_account_id = PROVIDER_ACCOUNT_ID
        operation = "describe_regions"

        def perform(self, credential_path, *, lease):
            return True

    with pytest.raises(ContractViolation, match="must return its observation"):
        executor.run(lease, make_request(), BooleanOperation())  # type: ignore[arg-type]


def test_an_isolation_scope_cannot_be_derived_from_a_non_lease(executor):
    """A scope comes from a lease's binding, so there is nothing to derive without one."""
    with pytest.raises(ContractViolation, match="derived from a lease"):
        executor.isolation_root_for({"workspace_id": WORKSPACE})  # type: ignore[arg-type]


def test_materialization_refuses_a_non_secret_material(tmp_path):
    """A bare `str` cannot be materialized, so the non-rendering property is unskippable."""
    root = IsolationRoot(
        base=tmp_path,
        org_id="org-a",
        workspace_id="ws-a",
        provider="aws",
        provider_account_id=PROVIDER_ACCOUNT_ID,
    )
    with pytest.raises(ContractViolation, match="requires SecretMaterial"):
        with restricted_materialization(
            FAKE_KEY_VALUE,  # type: ignore[arg-type]
            root=root,
            filename="credentials",
        ):
            pass


def test_secret_material_equality_does_not_reveal_the_value():
    """Two wrappers over the same value compare equal; a mismatch reveals nothing.

    Equality exists so a test can compare material without the failure message
    printing it. Cross-type comparison is False rather than raising: `material == "x"`
    is what someone writes while debugging, and it must not become a leak or a crash.
    """
    assert SecretMaterial(FAKE_KEY_VALUE) == SecretMaterial(FAKE_KEY_VALUE)
    assert SecretMaterial(FAKE_KEY_VALUE) != SecretMaterial("OTHER-VALUE")
    assert SecretMaterial(FAKE_KEY_VALUE) != FAKE_KEY_VALUE


def test_a_lease_reports_live_provenance_only_when_it_claims_it():
    """`is_mocked` reads the lease's own provenance.

    Both branches are asserted because the executor deliberately overrides this with
    `TRUSTED_DELIVERY_IS_MOCKED` (tested above). If only the mocked branch were
    covered, the override test could pass against a property that always said "mock"
    and nobody would learn that the lease-level reading had stopped working — which is
    the reading that starts mattering the day B ships the real contract.
    """
    assert make_lease().is_mocked is True
    assert make_lease(provenance={"trusted_delivery": "mock"}).is_mocked is True
    assert make_lease(provenance={"trusted_delivery": "live"}).is_mocked is False


@pytest.mark.parametrize("component", ["", "   ", None, 7])
def test_isolation_scope_components_must_be_non_empty_strings(tmp_path, component):
    """A blank scope component would collapse two tenants into one directory.

    `Path("base") / "" ` is `Path("base")`, so an empty org id silently promotes a
    tenant's credential directory up a level — into the parent that every other tenant
    also derives from.
    """
    with pytest.raises(ContractViolation, match="IsolationRoot.org_id is required"):
        IsolationRoot(
            base=tmp_path,
            org_id=component,
            workspace_id="ws-a",
            provider="aws",
            provider_account_id=PROVIDER_ACCOUNT_ID,
        )


def test_a_lease_desynchronized_from_its_binding_is_still_refused(
    executor, operation, channel
):
    """The recipient is re-checked against the binding, not only against the lease.

    `DeliveryLease.recipient` delegates to the binding today, so the executor's second
    check is the same answer — and that is exactly why it needs its own test. Without
    one, the re-check is unexecuted code that a later refactor deletes as redundant,
    and the property "the lease authorizes THIS executor" would then depend on a
    property in another module continuing to delegate.

    The stand-in below is the desynchronized case made real: a lease whose own
    `recipient` says this executor while its binding names a different one. It has to
    subclass `DeliveryLease` because the executor refuses duck-typed leases outright
    (tested separately), so this reaches the check under test rather than the earlier one.
    """

    class DesynchronizedLease(DeliveryLease):
        @property
        def recipient(self):
            return ExecutorIdentity(executor_id=EXECUTOR_ID)

    desynchronized = DesynchronizedLease(
        lease_id="lease-desync",
        reference=make_reference(),
        workspace_id=WORKSPACE,
        binding=make_binding(recipient_id=OTHER_EXECUTOR_ID),
    )
    assert desynchronized.recipient == executor.identity

    with pytest.raises(DeliveryRefused, match="not issued to this executor"):
        executor.run(desynchronized, make_request(), operation)
    assert channel.calls == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _imported_names(path: pathlib.Path) -> set[str]:
    """Every module name imported by a source file, via its AST."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """`id()` of every string expression that is a docstring.

    Collected so the retirement check can ignore prose. The modules quote the defect
    they replace deliberately — a text grep would fail on the explanation and pass on
    a real regression hidden in a later triple-quoted string.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _string_literals_outside_docstrings(tree: ast.Module) -> list[str]:
    """Every string constant in a module that is not a docstring."""
    skip = _docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
    ]


def _assigned_and_attribute_names(tree: ast.Module) -> set[str]:
    """Every name this module binds or reads as an attribute.

    Covers dataclass fields (annotated assignments), plain assignments, function and
    property names, and attribute access — so a status flag cannot be reintroduced
    under any of those spellings without this seeing it.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names
