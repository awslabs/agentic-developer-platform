"""Content-addressed readback of a verified resource association, not native readiness."""

from datetime import datetime

from l5_services.adapters.devops.spaces import SpaceSelection
from l5_services.devops.resource_scope import ResourceSelection, policy_digest


class PolicyUnavailable(ValueError):
    pass


def access_policy_document(binding, receipt, *, now, admitted_at=None):
    """An old reference never resolves to a changed or incomplete observation."""
    try:
        selection = SpaceSelection(**receipt.selection)
        observed = receipt.resource_observation or {}
        resource = ResourceSelection(**observed["resource_selection"])
        effective = observed["effective_scope"]
        snapshot = effective["policy_snapshot"]
        expiry = datetime.fromisoformat(observed["expires_at"])
        checked = datetime.fromisoformat(observed["checked_at"])
        # Existing native work retains the observation that was fresh at its
        # admission. This does not refresh the proof or authorize a new run.
        evaluated_at = now if admitted_at is None else admitted_at
        if (expiry.tzinfo is None or checked.tzinfo is None or evaluated_at.tzinfo is None
                or not checked <= evaluated_at < expiry or evaluated_at > now):
            raise PolicyUnavailable("access_policy_expired")
        if (
            binding.service_id != "devops" or binding.state != "ready"
            or receipt.state not in {"succeeded", "ready"}
            or receipt.scope_fingerprint != selection.fingerprint()
            or (selection.tenant_id, selection.product_id, selection.project_id, selection.binding_id)
            != (binding.tenant_id, binding.product_id, binding.project_id, binding.binding_id)
            or (binding.native_resource_ref or {}).get("record_id") != receipt.native_space_id
            or not receipt.native_space_id
            or (binding.provider_identity or {}).get("account_id") != selection.account_id
            or (binding.provider_identity or {}).get("region") != selection.region
            or observed.get("status") not in {"verifying", "ready"} or observed.get("reason")
            or observed.get("native_space_id") != receipt.native_space_id
            or observed.get("space_fingerprint") != selection.fingerprint()
            or observed.get("association_status") != "valid" or not observed.get("association_id")
            or observed.get("scope_fingerprint") != resource.fingerprint()
            or resource.connection_ref["tenant_id"] != binding.tenant_id
            or resource.connection_ref["project_id"] != binding.project_id
            or resource.connection_ref["record_type"] != "credential"
            or observed.get("account_id") != resource.account_id
            or observed.get("region") != resource.region or observed.get("role_arn") != resource.role_arn
            or effective.get("trust") != "selected_space"
            or snapshot.get("role_arn") != resource.role_arn
            or not snapshot.get("selected_resources")
            or policy_digest(snapshot) != effective.get("policy_sha256")
        ):
            raise PolicyUnavailable("access_policy_unverified")
        document = {
            "version": 1, "scope": binding.scope, "binding_id": binding.binding_id,
            "binding_generation": binding.generation, "binding_revision": binding.revision,
            "operation_id": receipt.operation_id, "configuration_revision": receipt.configuration_revision,
            "native_space_id": receipt.native_space_id,
            "hosting_connection_ref": binding.connection_ref,
            "space_selection": receipt.selection, "resource_observation": observed,
        }
        reference = {**resource.connection_ref, "record_type": "policy",
                     "record_id": receipt.operation_id, "record_revision": policy_digest(document)}
        return {"policy_ref": reference, "document": document,
                "native_readiness": observed["status"]}
    except PolicyUnavailable:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PolicyUnavailable("access_policy_unverified") from None
