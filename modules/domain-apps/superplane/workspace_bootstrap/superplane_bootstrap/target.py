"""Gate 1: verify the cluster before touching it.

Issue #5533 (w6-10), EPIC #4910. Design item 1: "Verify account/region/cluster TLS
identity and scoped access".

## The failure this prevents

Bootstrap installs a tenant's namespace and establishes a scoped credential
against whatever endpoint it is pointed at. If the endpoint is not the cluster the
reviewed plan described, that installation lands somewhere nobody authorized — and
the symptom is a *successful* bootstrap, because every subsequent step works fine
against the wrong cluster. There is no later gate that catches this, which is why
it is the first one.

## Why the expected values come from Terraform outputs, not from the request

`../infra/workspaces/outputs.tf` publishes `cluster_name`, `cluster_arn`,
`cluster_endpoint`, `cluster_certificate_authority_data`, `account_id` and
`aws_region` precisely so a consumer "can reach the cluster without being told
anything out-of-band" — its words. A caller-supplied CA would make this check
compare the request against itself.

## Why the CA comparison is the load-bearing one

Account and region are cheap to check and cheap to get right. The certificate is
the only field that distinguishes *this* cluster's API server from another server
answering at a similar address: two clusters in the same account and region have
different CAs, and a stale CA means the cluster was replaced since the plan was
reviewed. So a mismatch is refused rather than reported, and it is compared in
constant time — not because the CA is secret (it is public, see `access.py`), but
because a byte-at-a-time comparison is the one that can be short-circuited, and
using the constant-time primitive everywhere means no reviewer has to work out
which comparisons were safe to make fast.

## Identity comes from the binding, never from parameters

`org_id` and `workspace_id` are read from the trusted `OperationBinding`'s
resolved principal. The shared contract's `ProvisioningIntent` deliberately
carries no workspace or org field for this reason, and `forbidden_parameters`
refuses a parameter that asserts one *even when it matches* the bound value —
accepting a matching claim would make the check depend on agreement rather than
on authority, and the next caller's claim need not match.
`../infra/account-factory/account_factory/modes.py::ValidationAuthorization.from_operation_binding`
sets this precedent for the account side; this is the cluster side of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hmac import compare_digest

from .access import ClusterIdentity, ProviderIdentity
from .errors import BootstrapRefused

# The only cluster status that permits bootstrap. A cluster that is CREATING may
# become ACTIVE in a minute, but bootstrapping one that is not ACTIVE yet means
# racing the control plane's own readiness, and the safe answer to "not yet" is
# to refuse and be re-run.
ACTIVE = "ACTIVE"


def _binding_identity(binding: object) -> tuple[str, str]:
    """Read (org_id, workspace_id) from a trusted OperationBinding.

    Only the genuine contract type is accepted. The trusted service resolver supplies
    provenance; type checking alone does not make a caller-authored object authoritative.
    Revalidate the operation purpose, permission, version and expiry before execution.
    """
    from superplane_contracts.provisioning import (
        OperationBinding,
        PROVISION,
        REQUIRED_PERMISSION,
    )
    from superplane_contracts.version import CONTRACT_VERSION

    if not isinstance(binding, OperationBinding):
        raise BootstrapRefused(
            "bootstrap requires the facade-issued OperationBinding; lookalike objects are not authority"
        )
    if (
        binding.action != PROVISION
        or binding.permission != REQUIRED_PERMISSION
        or binding.contract_version != CONTRACT_VERSION
        or not binding.operation_id.strip()
        or binding.is_expired(datetime.now(UTC))
    ):
        raise BootstrapRefused(
            "operation binding is expired or does not authorize workspace provisioning"
        )
    return binding.principal.org_id, binding.principal.workspace_id


@dataclass(frozen=True)
class VerifiedTarget:
    """A cluster whose identity has been checked against the reviewed plan.

    Constructing one of these is not possible except through `verify_target`, in
    the sense that matters: every field is copied from an observation that passed
    a gate, so a downstream module holding a `VerifiedTarget` is holding evidence
    rather than a claim. `endpoint` and `certificate_authority_data` are carried so
    later gates need no second provider read that could observe a changed cluster.
    """

    org_id: str
    workspace_id: str
    account_id: str
    region: str
    cluster_name: str
    cluster_arn: str
    endpoint: str
    certificate_authority_data: str
    principal_arn: str
    cluster_ownership: str

    @property
    def is_adopted(self) -> bool:
        """Whether the cluster was supplied by its owner rather than created by ADP.

        `retire.py` branches on this. Kept as a property over the recorded string
        so the branch cannot be spelled two different ways in two places.
        """
        return self.cluster_ownership == "adopted"


def verify_target(
    *,
    binding: object,
    provider: ProviderIdentity,
    observed: ClusterIdentity,
    expected_account_id: str,
    expected_region: str,
    expected_cluster_name: str,
    expected_cluster_arn: str,
    expected_certificate_authority_data: str,
    cluster_ownership: str,
) -> VerifiedTarget:
    """Refuse unless the observed cluster is the one the reviewed plan described.

    `expected_*` come from the workspace Terraform module's outputs. `observed`
    and `provider` come from a provider read taken immediately before this call.
    `cluster_ownership` is `ClusterOwnership.ADP_CREATED` or `.ADOPTED` from
    #5530, and is recorded rather than inferred: the request shape decides it, and
    a bootstrap that guessed would guess wrong for a supplied cluster in an
    ADP-managed account.
    """
    org_id, workspace_id = _binding_identity(binding)

    # The caller's real identity first. Every later comparison is against a
    # provider read taken with this credential, so a wrong account here makes the
    # rest of the observations answers about the wrong world.
    if provider.account_id != expected_account_id:
        raise BootstrapRefused(
            "provider identity resolves to a different AWS account than the "
            f"selected target (expected {expected_account_id}, "
            f"observed {provider.account_id}); refused before any mutation"
        )
    if observed.account_id != expected_account_id:
        raise BootstrapRefused(
            f"cluster belongs to account {observed.account_id}, not the selected "
            f"target account {expected_account_id}"
        )
    if observed.region != expected_region:
        raise BootstrapRefused(
            f"cluster is in region {observed.region}, not the selected "
            f"region {expected_region}"
        )
    if observed.name != expected_cluster_name:
        raise BootstrapRefused(
            f"cluster is named {observed.name!r}, not the expected "
            f"{expected_cluster_name!r}"
        )
    if observed.arn != expected_cluster_arn:
        raise BootstrapRefused(
            "cluster ARN does not match the reviewed plan's cluster; refused "
            "rather than reconciled, because an ARN mismatch names a different "
            "cluster and not a changed attribute"
        )
    if observed.status != ACTIVE:
        raise BootstrapRefused(
            f"cluster status is {observed.status!r}, not {ACTIVE!r}; bootstrap "
            "refuses rather than waiting, so a re-run observes actual readiness"
        )

    # The TLS identity check. Compared as bytes in constant time; the message
    # carries no certificate material, because a refusal is the most likely thing
    # to reach a log or an issue comment.
    expected_ca = expected_certificate_authority_data
    if not isinstance(expected_ca, str) or not expected_ca.strip():
        raise BootstrapRefused(
            "expected cluster certificate authority is required; an absent "
            "expectation cannot verify anything and must not default to trust"
        )
    if not compare_digest(
        observed.certificate_authority_data.encode(), expected_ca.encode()
    ):
        raise BootstrapRefused(
            "cluster certificate authority does not match the reviewed "
            "infrastructure's published certificate; the API server is not the "
            "one that was reviewed, or the cluster was replaced. Refused: "
            "installing a tenant namespace against an unverified server is the "
            "failure this gate exists to prevent"
        )

    if cluster_ownership not in {"adp-created", "adopted"}:
        raise BootstrapRefused(
            f"unknown cluster ownership {cluster_ownership!r}; expected "
            "'adp-created' or 'adopted' from the request's ownership mode"
        )

    return VerifiedTarget(
        org_id=org_id,
        workspace_id=workspace_id,
        account_id=observed.account_id,
        region=observed.region,
        cluster_name=observed.name,
        cluster_arn=observed.arn,
        endpoint=observed.endpoint,
        certificate_authority_data=observed.certificate_authority_data,
        principal_arn=provider.principal_arn,
        cluster_ownership=cluster_ownership,
    )
