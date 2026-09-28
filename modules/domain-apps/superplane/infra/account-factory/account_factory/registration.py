"""Creation identity returned only by the trusted durable registration adapter.

This in-process value is never accepted from JSON or CLI flags. Constructing it
is not proof of provenance; the service must obtain it from load_created_account.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class CreatedAccountRegistration:
    operation_id: str
    organization_id: str
    workspace_id: str
    account_id: str
    approved_identity: str

    def __post_init__(self):
        if not all(
            (
                self.operation_id,
                self.organization_id,
                self.workspace_id,
                self.approved_identity,
            )
        ):
            raise ValueError(
                "creation registration requires complete operation identity"
            )
        if not re.fullmatch(r"[0-9]{12}", self.account_id):
            raise ValueError(
                "creation registration requires an authoritative account ID"
            )
