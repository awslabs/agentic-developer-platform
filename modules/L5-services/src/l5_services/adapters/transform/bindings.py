"""Conservative cleanup of a receipt-owned Transform definition."""


class TransformDefinitionCleanup:
    def __init__(self, native):
        self.native = native

    async def cleanup(self, reference, verified, receipt, intent):
        if (reference.get("record_type") != "transform_definition"
            or receipt != intent.owned_resource_receipt
            or receipt.get("native_resource_ref") != reference
            or receipt.get("operation_id") is None
            or receipt.get("intent_hash") is None
            or not isinstance(receipt.get("provider_identity"), dict)
            or any(receipt["provider_identity"].get(field) != verified[field]
                   for field in ("account_id", "role_arn", "region"))):
            raise PermissionError("ownership_required")
        observed = await self.native.read(reference, verified)
        if not isinstance(observed, dict):
            raise ValueError("native_readback_unavailable")
        if (observed.get("native_resource_ref") == reference
            and all(observed.get("provider_identity", {}).get(field) == verified[field]
                    for field in ("account_id", "role_arn", "region"))):
            await self.native.delete(reference, verified)
        elif observed.get("absent") is not True:
            raise ValueError("native_identity_unconfirmed")
        readback = await self.native.read(reference, verified)
        evidence = readback.get("absence_evidence_ref") if isinstance(readback, dict) else None
        if (not isinstance(readback, dict) or readback.get("absent") is not True
            or readback.get("native_resource_ref") != reference
            or not isinstance(evidence, dict)
            or evidence.get("tenant_id") != reference["tenant_id"]
            or evidence.get("project_id") != reference["project_id"]
            or evidence.get("record_type") != "transform_absence"
            or evidence.get("record_id") != reference["record_id"]
            or not evidence.get("record_revision")):
            raise ValueError("native_absence_unverified")
        return {"operation_id": intent.operation_id, "intent_hash": intent.intent_hash,
                "native_resource_ref": reference, "provider_identity": receipt["provider_identity"],
                "creation_receipt": receipt, "absence_evidence_ref": evidence, "absent": True}
