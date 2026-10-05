"""Deployment-owned admission fence, independent of the management route list."""


def dispatch_enabled() -> bool:
    from app.config import settings

    return settings.superplane_operation_dispatch_enabled is True


def require_admission_enabled(
    *, enabled: bool | None = None, lifecycle: bool = False
) -> None:
    from app.config import settings
    from app.services.provisioning import ProvisioningUnavailable

    if enabled is False or not dispatch_enabled():
        raise ProvisioningUnavailable(
            "operation admission is disabled for adapter verification"
        )
    if lifecycle:
        if settings.superplane_paid_worker_mode != "native-lifecycle":
            raise ProvisioningUnavailable(
                "native paid worker does not support workspace lifecycle admission"
            )
        if (
            not settings.superplane_paid_worker_binding_file
            or not settings.superplane_operation_gateway_url
        ):
            raise ProvisioningUnavailable(
                "paid worker binding verification is unavailable"
            )


# Syntactically valid, deliberately without a reviewed plan or human approval.
# Even a regressed stage guard cannot admit this request as paid work.
STAGED_ADMISSION_PROBE = {
    "operation_id": "00000000-0000-0000-0000-000000000000",
    "name": "adapter-verification-no-approval",
    "mode": "managed",
    "isolation_mode": "dedicated",
    "cluster_placement": "dedicated",
}


def expected_lifecycle_binding():
    """Load exact deployment-owned inputs, never from an admission request."""
    import json
    from pathlib import Path

    from app.config import settings
    from app.services.provisioning import ProvisioningUnavailable

    try:
        path = Path(settings.superplane_paid_worker_binding_file)
        if not path.is_absolute() or path.stat().st_size > 4096:
            raise ValueError("invalid deployment binding file")
        value = json.loads(path.read_text())
        fields = {
            "producer_registry_id",
            "worker_registry_id",
            "worker_namespace",
            "worker_service_account",
            "worker_role_arn",
            "worker_image_digest",
            "operation_schema",
            "queue_arn",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or any(not isinstance(item, str) or not item for item in value.values())
        ):
            raise ValueError("incomplete deployment binding")
        return value
    except (OSError, ValueError, TypeError):
        raise ProvisioningUnavailable(
            "paid worker binding verification is unavailable"
        ) from None
