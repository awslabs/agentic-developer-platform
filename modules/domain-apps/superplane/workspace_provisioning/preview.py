"""Preview the real Account Factory request without issuing execution authority.

The API resolves authorization and pricing before calling this function. The revision
binds the exact validated inputs and policy result; it is not an approval, a Terraform
artifact digest, or an operation identity. Mutation still requires shared admission.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Mapping

from account_factory.modes import (
    AccountFactoryRequest,
    ModeError,
    ValidationAuthorization,
    ensure_valid,
)


def _canonical(value: object) -> str:
    def check_keys(item: object) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ModeError("preview objects require string keys")
            for child in item.values():
                check_keys(child)
        elif isinstance(item, (tuple, list)):
            for child in item:
                check_keys(child)

    check_keys(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ModeError("preview inputs must be finite JSON values") from exc


def _estimate(value: Mapping[str, object] | None) -> dict | None:
    if value is None:
        return None
    estimate = dict(value)
    if set(estimate) != {"amount_usd", "currency", "as_of", "assumptions"}:
        raise ModeError(
            "cost estimate requires amount_usd, currency, as_of and assumptions"
        )
    try:
        amount = Decimal(str(estimate["amount_usd"]))
        observed = datetime.fromisoformat(str(estimate["as_of"]).replace("Z", "+00:00"))
    except (ValueError, InvalidOperation) as exc:
        raise ModeError(
            "cost estimate amount or observation timestamp is invalid"
        ) from exc
    if (
        not amount.is_finite()
        or amount < 0
        or observed.tzinfo is None
        or estimate["currency"] != "USD"
        or not isinstance(estimate["assumptions"], list)
        or any(not isinstance(item, str) for item in estimate["assumptions"])
    ):
        raise ModeError(
            "cost estimate must name finite USD cost, dated evidence and assumptions"
        )
    return estimate


@dataclass(frozen=True)
class WorkspacePreview:
    """An immutable snapshot; callers receive a fresh dictionary for serialization."""

    revision: str
    _document: str

    def as_dict(self) -> dict[str, object]:
        return {"revision": self.revision, **json.loads(self._document)}


def preview_workspace(
    request: AccountFactoryRequest,
    *,
    authorization: ValidationAuthorization,
    requested_capacity: Mapping[str, object],
    cost_estimate: Mapping[str, object] | None,
    approval_required: bool,
) -> WorkspacePreview:
    """Use the canonical mode/ownership validator and bind every preview input.

    ``authorization`` and ``approval_required`` are resolved by trusted service
    composition. They must never be copied from the request body. Missing pricing
    is explicitly unknown, not zero. New-account mode has no account ID to report
    until the durable CreateAccount operation has observed one.
    """
    if not isinstance(request, AccountFactoryRequest) or not isinstance(
        authorization, ValidationAuthorization
    ):
        raise ModeError("preview requires a real request and resolved authorization")
    unchecked = ensure_valid(request, authorization)
    if unchecked:
        raise ModeError("preview authorization is incomplete: " + ", ".join(unchecked))
    if type(approval_required) is not bool or not isinstance(
        requested_capacity, Mapping
    ):
        raise ModeError(
            "preview requires the resolved approval policy and capacity object"
        )
    document = {
        "mode": request.mode.value,
        "target": {
            "account": request.target_account_id,
            "region": request.region,
            "cluster": request.cluster_name,
        },
        "ownership": {
            "account": "adp-created" if request.mode.creates_account else "adopted",
            "cluster": request.cluster_ownership.value,
            "network": "adp-created" if request.mode.creates_cluster else "adopted",
        },
        "requested_capacity": dict(requested_capacity),
        "cost_estimate": _estimate(cost_estimate),
        "approval_required": approval_required,
    }
    policy = asdict(authorization)
    for key, value in policy.items():
        if isinstance(value, (set, frozenset)):
            policy[key] = sorted(value)
    revision = hashlib.sha256(
        _canonical(
            {
                "version": 1,
                "request": asdict(request),
                "authorization": policy,
                "preview": document,
            }
        ).encode()
    ).hexdigest()
    return WorkspacePreview(revision=revision, _document=_canonical(document))
