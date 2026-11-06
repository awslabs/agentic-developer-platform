"""One policy schema and digest for API approval and the private lifecycle worker."""

from typing import Annotated, Literal
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifacts import digest
from .runtime_config import validate_runtime_config


class CredentialReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    credential_id: str = Field(min_length=1, max_length=255)
    credential_service: str = Field(min_length=1, max_length=100)
    credential_label: str = Field(min_length=1, max_length=255)

    @field_validator("credential_id")
    @classmethod
    def opaque_reference(cls, value):
        from superplane_contracts.secrets import assert_no_secret_material

        # Policy references are opaque ASCII handles. Reject URI/escape forms
        # entirely, so encoded ARN or secret strings cannot become references.
        if value.lower().startswith("spda1:"):
            # Reserved installation-owned provider handles are a distinct source
            # kind. No aliases, arbitrary URI schemes or personal fallback.
            suffix = value.removeprefix("spda1:")
            try:
                parsed = UUID(suffix)
            except ValueError:
                raise ValueError("invalid installation provider handle") from None
            if value != "spda1:" + str(parsed) or parsed.version != 5:
                raise ValueError("invalid installation provider handle")
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", value):
            raise ValueError("lifecycle credential must be an opaque ADP handle")
        assert_no_secret_material(value, what="lifecycle credential reference")
        return value


class LifecyclePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    adp_org_id: str = Field(min_length=1)
    aws_organization_id: (
        Annotated[str, Field(pattern=r"^o-[a-z0-9]{10,32}$")] | Literal["standalone"]
    )
    management_account_id: str = Field(pattern=r"^[0-9]{12}$")
    management_cluster: str = Field(min_length=1)
    permitted_modes: frozenset[Literal["managed", "adopt", "new-account-managed"]]
    permitted_target_accounts: frozenset[Annotated[str, Field(pattern=r"^[0-9]{12}$")]]
    permitted_organizational_units: frozenset[str] = frozenset()
    permitted_regions: frozenset[str]
    isolation_modes: frozenset[Literal["dedicated", "namespace", "research"]]
    workspace_defaults: dict
    operation_max_runtime_seconds: int = Field(gt=0)
    runtime: dict
    credential_references: dict[
        Annotated[str, Field(pattern=r"^[0-9]{12}$")], CredentialReference
    ]

    @field_validator("runtime")
    @classmethod
    def trusted_runtime(cls, value):
        return validate_runtime_config(value)

    @model_validator(mode="after")
    def standalone_account_scope(self):
        if self.aws_organization_id == "standalone" and (
            not self.permitted_modes <= {"managed", "adopt"}
            or self.permitted_target_accounts != {self.management_account_id}
            or self.permitted_organizational_units
            or set(self.credential_references) - {self.management_account_id}
        ):
            raise ValueError(
                "standalone policy permits only managed/adopt in the management "
                "account, without organizational units or other-account credentials"
            )
        return self

    @model_validator(mode="after")
    def governed_provider_requires_reviewed_boundary(self):
        variables = self.runtime["workspace_variables"]
        for account, reference in self.credential_references.items():
            if not reference.credential_id.startswith("spda1:"):
                continue
            boundary = variables.get("workspace_role_permissions_boundary_arn", "")
            if (
                not boundary.startswith(f"arn:aws:iam::{account}:policy/")
                or variables.get("networking_mode", "owned") != "owned"
                or variables.get("supplied_vpc_id")
                or variables.get("supplied_private_subnet_ids")
            ):
                raise ValueError(
                    "installation provider requires its reviewed same-account role boundary and owned networking"
                )
        return self


def policy_document(value):
    policy = (
        value
        if isinstance(value, LifecyclePolicy)
        else LifecyclePolicy.model_validate(value)
    )
    document = policy.model_dump(mode="json")
    for key in (
        "permitted_modes",
        "permitted_target_accounts",
        "permitted_organizational_units",
        "permitted_regions",
        "isolation_modes",
    ):
        document[key] = sorted(document[key])
    return document


def policy_digest(value):
    return digest(policy_document(value))


def select_policy(document, org_id, workspace_id=None):
    """Select an installer-owned workspace policy without changing legacy digests.

    Profiles are complete policies keyed by both tenant and immutable workspace.
    They are not caller overrides and cannot change the management identity.
    """
    if (
        not isinstance(document, dict)
        or set(document) - {"version", "tenants", "workspace_policies"}
        or type(document.get("version")) is not int
        or document["version"] != 1
        or not isinstance(document.get("tenants"), dict)
    ):
        raise ValueError("invalid lifecycle policy document")
    profiles = document.get("workspace_policies", {})
    if not isinstance(profiles, dict) or set(profiles) - set(document["tenants"]):
        raise ValueError("workspace policies require an installed tenant")
    selected = None
    for tenant, entries in profiles.items():
        if not isinstance(entries, dict):
            raise ValueError("workspace policies must be keyed by workspace UUID")
        parent = LifecyclePolicy.model_validate(document["tenants"][tenant])
        for workspace, value in entries.items():
            if not isinstance(workspace, str) or str(UUID(workspace)) != workspace:
                raise ValueError("workspace policy identity is not canonical")
            policy = LifecyclePolicy.model_validate(value)
            if any(
                getattr(policy, key) != getattr(parent, key)
                for key in (
                    "adp_org_id",
                    "aws_organization_id",
                    "management_account_id",
                    "management_cluster",
                )
            ):
                raise ValueError("workspace policy changed management identity")
            if tenant == str(org_id) and workspace == str(workspace_id):
                selected = policy
    if str(org_id) not in document["tenants"]:
        raise ValueError("no lifecycle policy for this organization")
    return selected or LifecyclePolicy.model_validate(document["tenants"][str(org_id)])
