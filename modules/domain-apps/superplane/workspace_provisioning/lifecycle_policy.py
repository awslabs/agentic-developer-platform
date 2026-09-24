"""One policy schema and digest for API approval and the private lifecycle worker."""

from typing import Annotated, Literal
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", value):
            raise ValueError("lifecycle credential must be an opaque ADP handle")
        assert_no_secret_material(value, what="lifecycle credential reference")
        return value


class LifecyclePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    adp_org_id: str = Field(min_length=1)
    aws_organization_id: str = Field(pattern=r"^o-[a-z0-9]{10,32}$")
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
