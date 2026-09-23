"""Test support for the adopted Account Factory — Issue #5530 (w6-07).

`import account_factory` works here because `__init__.py` in this package puts the module
directory on `sys.path` before this file is imported — see the reasoning there.

Every fixture here builds a request with SYNTHETIC identities. None of them is a real
target: the organization ids, account ids and cluster names are documentation-range values
chosen so that a test fixture leaking into a real invocation could not act on anything. That
is deliberate — the legacy `config.env` shipped a real account id and a real individual's
email address as working defaults, which is the defect `test_no_legacy_targets.py` locks
shut.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from account_factory.modes import (
    AccountFactoryRequest,
    OwnershipMode,
    ValidationAuthorization,
)

from . import MODULE_DIR

# Synthetic, and not any real ADP or upstream target. 12-digit ids in an obviously-fake
# pattern; `o-` ids of legal shape; cluster and workspace names that are clearly fixtures.
FIXTURE_ORG_ID = "o-testorg1234"
FIXTURE_MANAGEMENT_ACCOUNT = "000000000001"
FIXTURE_TARGET_ACCOUNT = "000000000002"
FIXTURE_MANAGEMENT_CLUSTER = "fixture-management-cluster"
FIXTURE_WORKSPACE = "ws-fixture"
FIXTURE_REGION = "us-west-2"
# Synthetic organizational unit, of legal `ou-<root>-<suffix>` shape and obviously a fixture.
# Required by new-account-managed since #5531 (w6-08): a created account is placed somewhere
# in the organization tree, and an unstated placement means the organization root — the least
# restricted position available — so it is required rather than defaulted.
FIXTURE_ORGANIZATIONAL_UNIT = "ou-test-fixture01"
# The account the governed creation path recorded for this workspace, as
# new-account-managed rendering now requires (#5531, w6-08). Distinct from
# `FIXTURE_TARGET_ACCOUNT`, which is an ADOPTED account: rendering must refuse a
# creation-record id in the adopting modes and require one in the creating mode, so a single
# shared constant could not tell a test that mixed them up from one that did not.
FIXTURE_CREATED_ACCOUNT = "000000000777"


def governed_account_id(request: AccountFactoryRequest):
    """The `account_id` a mode's render requires, or `None` where one is forbidden.

    Rendering new-account-managed needs the id of the account the fenced creation path opened,
    because that mode's `AccountOwnership` binds to an existing account instead of declaring an
    ACK `Account` that would create one (#5531). The adopting modes take their account from
    `target_account_id` and REFUSE an id here.

    Exists so a mode-parametrized test can render every mode without either hardcoding that
    asymmetry at each call site or — worse — passing an id to every mode and silently losing
    the refusal that keeps the two sources of account identity from being interchangeable.
    """
    if not request.mode.creates_account:
        return None
    from account_factory.registration import CreatedAccountRegistration
    from account_factory.creation import account_identity_key

    return CreatedAccountRegistration(
        "op-fixture",
        request.organization_id,
        request.workspace_id,
        FIXTURE_CREATED_ACCOUNT,
        account_identity_key(request),
    )


def new_account_request(**overrides) -> AccountFactoryRequest:
    """A valid `new-account-managed` request."""
    fields = {
        "mode": OwnershipMode.NEW_ACCOUNT_MANAGED,
        "organization_id": FIXTURE_ORG_ID,
        "management_account_id": FIXTURE_MANAGEMENT_ACCOUNT,
        "management_cluster": FIXTURE_MANAGEMENT_CLUSTER,
        "region": FIXTURE_REGION,
        "workspace_id": FIXTURE_WORKSPACE,
        "account_email": "fixture-workspace@example.invalid",
        "organizational_unit_id": FIXTURE_ORGANIZATIONAL_UNIT,
        "vpc_cidr": "10.64.0.0/16",
        "availability_zones": (f"{FIXTURE_REGION}a", f"{FIXTURE_REGION}b"),
        "cluster_version": "1.31",
        "node_instance_type": "m6i.large",
    }
    fields.update(overrides)
    return AccountFactoryRequest(**fields)


def existing_account_request(**overrides) -> AccountFactoryRequest:
    """A valid `existing-account-managed` request."""
    fields = {
        "mode": OwnershipMode.EXISTING_ACCOUNT_MANAGED,
        "organization_id": FIXTURE_ORG_ID,
        "management_account_id": FIXTURE_MANAGEMENT_ACCOUNT,
        "management_cluster": FIXTURE_MANAGEMENT_CLUSTER,
        "region": FIXTURE_REGION,
        "workspace_id": FIXTURE_WORKSPACE,
        "target_account_id": FIXTURE_TARGET_ACCOUNT,
        "vpc_cidr": "10.65.0.0/16",
        "availability_zones": (f"{FIXTURE_REGION}a", f"{FIXTURE_REGION}b"),
        "cluster_version": "1.31",
        "node_instance_type": "m6i.large",
    }
    fields.update(overrides)
    return AccountFactoryRequest(**fields)


def bring_existing_cluster_request(**overrides) -> AccountFactoryRequest:
    """A valid `bring-existing-cluster` request."""
    fields = {
        "mode": OwnershipMode.BRING_EXISTING_CLUSTER,
        "organization_id": FIXTURE_ORG_ID,
        "management_account_id": FIXTURE_MANAGEMENT_ACCOUNT,
        "management_cluster": FIXTURE_MANAGEMENT_CLUSTER,
        "region": FIXTURE_REGION,
        "workspace_id": FIXTURE_WORKSPACE,
        "target_account_id": FIXTURE_TARGET_ACCOUNT,
        "existing_cluster_name": "fixture-adopted-cluster",
    }
    fields.update(overrides)
    return AccountFactoryRequest(**fields)


# Keyed by mode so tests can assert a property holds for EVERY mode rather than for the one
# mode the test author happened to pick.
BUILDERS = {
    OwnershipMode.NEW_ACCOUNT_MANAGED: new_account_request,
    OwnershipMode.EXISTING_ACCOUNT_MANAGED: existing_account_request,
    OwnershipMode.BRING_EXISTING_CLUSTER: bring_existing_cluster_request,
}


def matching_authorization(
    request: AccountFactoryRequest, **overrides
) -> ValidationAuthorization:
    """An authorization that permits exactly this request — the positive control.

    Tests that assert a MISMATCH is refused need a matching authorization to compare
    against, otherwise they only establish that some authorization was rejected.

    Derived FROM the request on purpose: this is a fixture building a known-good control,
    not the production path. Production authorization comes from
    `ValidationAuthorization.from_operation_binding`, which reads a server-resolved
    principal — if a caller could construct its own authorization from its own request the
    comparison would confirm only that the request agrees with itself.
    """
    fields = {
        "organization_id": request.organization_id,
        "management_account_id": request.management_account_id,
        "management_cluster": request.management_cluster,
        "permitted_modes": frozenset(OwnershipMode),
        "workspace_id": request.workspace_id,
        # A set, so authorizing an account is an explicit act. For a mode with no target
        # account this is simply never consulted.
        "permitted_target_accounts": (
            frozenset({request.target_account_id})
            if request.target_account_id
            else frozenset()
        ),
        # Same shape as permitted_target_accounts, for the same reason: authorizing a
        # placement is an explicit act, and a mode that places no account never consults it.
        "permitted_organizational_units": (
            frozenset({request.organizational_unit_id})
            if request.organizational_unit_id
            else frozenset()
        ),
    }
    fields.update(overrides)
    return ValidationAuthorization(**fields)


def resolved_binding(
    workspace_id: str = FIXTURE_WORKSPACE, organization_id: str = FIXTURE_ORG_ID
) -> object:
    """A stand-in for a provisioning `OperationBinding` with a resolved principal.

    Duck-typed to match what `from_operation_binding` reads, so these tests do not require
    the contracts package on the path — the same reason that function is duck-typed. Building
    one of these is not proof of authorization in production; the facade issues real bindings.
    """

    class _Principal:
        subject = "u-fixture"
        org_id = organization_id

        def __init__(self) -> None:
            self.workspace_id = workspace_id

    class _Binding:
        def __init__(self) -> None:
            self.principal = _Principal()

    return _Binding()


@pytest.fixture
def module_dir() -> Path:
    return MODULE_DIR


@pytest.fixture(params=sorted(BUILDERS, key=lambda mode: mode.value))
def any_mode_request(request) -> AccountFactoryRequest:
    """A valid request in each mode in turn."""
    return BUILDERS[request.param]()
