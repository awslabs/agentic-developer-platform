"""Gateway-issued launch decisions used only as accounting evidence.

The worker sends an opaque launch nonce. Only a gateway-written row for the
authenticated tenant, invocation and attempt can supply its model evidence.
Missing telemetry leaves the snapshot attribution intact and the proposal NULL.
No resolver, authorizer or provider is called by this reporting projection.
"""

import json
import re
import time

from src.usage.persona_attribution import PersonaUsageAttribution

HEADER = "X-Adp-Model-Evidence"


def _key(record, nonce: str) -> str:
    return f"MODEL_USAGE#{record.invocation_id}#{record.current_attempt}#{nonce}"


def record_model_evidence(*, store, record, result: dict) -> None:
    """Persist the exact issued response; failure cannot change model admission."""
    try:
        nonce = result["nonce"]
        if not re.fullmatch(r"[0-9a-f]{64}", nonce):
            return
        store.client.put_item(
            TableName=store.table,
            Item={
                "pk": {"S": f"TENANT#{record.tenant_id}"},
                "sk": {"S": _key(record, nonce)},
                "result": {"S": json.dumps(result, sort_keys=True)},
                "ttl": {"N": str(int(time.time()) + 86400)},
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except Exception:
        # No evidence is preferable to turning a telemetry outage into a model
        # outage. The cost view explicitly reports missing decision evidence.
        return


def protected_usage_attribution(
    *, store, record, decision_id: str | None = None, approving_human_id: str | None = None
) -> PersonaUsageAttribution | None:
    from src.agentauth.model_policy import parse_snapshot

    try:
        execution = store._read(f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}") or {}
        digest = execution.get("model_policy_snapshot_digest", {}).get("S")
        snapshot = parse_snapshot(execution.get("model_policy_snapshot", {}).get("S"), digest, tenant_id=record.tenant_id)
        persona = execution.get("persona", {}).get("S")
        contract = snapshot.persona_contracts.get(persona)
        if not isinstance(contract, dict):
            return None
        proposal = None
        issued = None
        if decision_id and re.fullmatch(r"[0-9a-f]{64}", decision_id):
            item = store._read(f"TENANT#{record.tenant_id}", _key(record, decision_id)) or {}
            try:
                issued = json.loads(item.get("result", {}).get("S", "null"))
                candidate = issued["model_policy"].get("decision")
                if (
                    issued["tenant_id"] == record.tenant_id
                    and issued["invocation_id"] == record.invocation_id
                    and issued["attempt"] == record.current_attempt
                    and issued["nonce"] == decision_id
                    and issued["model_policy"].get("posture_verified") is True
                ):
                    if candidate is not None:
                        if not (
                            candidate["snapshot_digest"] == digest
                            and candidate["persona"] == persona
                            and candidate["tenant_id"] == record.tenant_id
                            and candidate["invocation_id"] == record.invocation_id
                        ):
                            raise ValueError("issued decision binding mismatch")
                        proposal = candidate
                else:
                    issued = None
            except (ValueError, KeyError, TypeError, AttributeError):
                issued = None
        proposal = proposal or {}
        return PersonaUsageAttribution(
            tenant_id=snapshot.tenant_id,
            invocation_id=record.invocation_id,
            root_invocation_id=snapshot.root_invocation_id,
            chain_id=snapshot.correlation_id,
            persona_key=persona,
            compatibility_class=contract["compatibility_class"],
            harness_contract_revision=contract["harness_contract_revision"],
            principal_kind=snapshot.principal_kind,
            principal_id=snapshot.principal_id,
            snapshot_digest=digest,
            policy_revision=snapshot.policy_revision,
            catalogue_revision=snapshot.catalogue_revision,
            requested_model_id=proposal.get("requested_model_id"),
            resolved_model_id=proposal.get("resolved_model_id"),
            resolution_source=proposal.get("resolution_source"),
            runtime_posture=proposal.get("runtime_posture"),
            posture_revision=proposal.get("posture_revision"),
            model_decision_json=json.dumps(issued) if issued else None,
            model_decision_id=decision_id if issued else None,
            approving_human_id=approving_human_id,
        )
    except Exception:
        return None
