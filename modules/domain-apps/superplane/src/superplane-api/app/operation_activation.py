"""Deployment-owned admission fence, independent of the management route list."""


def dispatch_enabled() -> bool:
    from app.config import settings

    return settings.superplane_operation_dispatch_enabled is True


def require_admission_enabled(*, enabled: bool | None = None) -> None:
    from app.services.provisioning import ProvisioningUnavailable

    if enabled is False or not dispatch_enabled():
        raise ProvisioningUnavailable("operation admission is disabled for adapter verification")


# Syntactically valid, deliberately without a reviewed plan or human approval.
# Even a regressed stage guard cannot admit this request as paid work.
STAGED_ADMISSION_PROBE = {
    "operation_id": "00000000-0000-0000-0000-000000000000",
    "name": "adapter-verification-no-approval",
    "mode": "managed",
    "isolation_mode": "dedicated",
    "cluster_placement": "dedicated",
}
