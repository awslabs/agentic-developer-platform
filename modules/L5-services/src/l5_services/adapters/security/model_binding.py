"""Reconcile the persistent threat model in an ADP-owned Security space."""

from asyncio import to_thread
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError


async def read_model(client, reference, registration, binding, selected_model=None, *, scope_document=None):
    receipt = binding.creation_receipt
    title = f'adp-{receipt["operation_id"]}-threat-model'
    description = f'ADP setup {receipt["intent_hash"]}'
    previous = next((item for item in binding.readiness or []
                     if item.get("kind") in {"threat_model", "threat_model_outcome_unknown",
                                             "threat_model_selection_required"}), None)
    matches = []
    token = None
    visited = set()
    for _ in range(10):
        options = {"agentSpaceId": reference["record_id"], "maxResults": 100}
        if token:
            options["nextToken"] = token
        try:
            page = await to_thread(client.list_threat_models, **options)
        except (BotoCoreError, ClientError) as error:
            raise ConnectionError("native_model_readback_unavailable") from error
        if not isinstance(page.get("threatModelSummaries"), list):
            raise ValueError("native_model_listing_incomplete")
        matches.extend(item["threatModelId"] for item in page["threatModelSummaries"]
                       if item.get("title") == title and item.get("threatModelId"))
        token = page.get("nextToken")
        if not token:
            break
        if token in visited:
            raise ValueError("native_model_listing_incomplete")
        visited.add(token)
    else:
        raise ValueError("native_model_listing_incomplete")
    if selected_model is not None:
        if (any(selected_model.get(field) != reference.get(field)
                for field in ("tenant_id", "project_id", "source_connection_id"))
                or selected_model.get("record_type") != "threat_model"
                or selected_model.get("record_id") not in matches
                or not selected_model.get("record_revision")):
            raise ValueError("native_model_selection_denied")
        matches = [selected_model["record_id"]]
    elif len(matches) > 1:
        raise ValueError("native_model_selection_ambiguous")
    created = False
    source_receipt = {}
    if not matches and previous is None:
        if scope_document is None:
            raise ValueError("security_architecture_document_required")
        document = await scope_document()
        if document is None:
            return None
        media_type = document["snapshot"]["content"]["media_type"]
        artifact_type, suffix = {"text/markdown": ("MD", ".md"), "text/plain": ("TXT", ".txt"),
                                 "application/pdf": ("PDF", ".pdf")}.get(media_type, (None, None))
        if artifact_type is None:
            raise ValueError("security_architecture_document_format_unsupported")
        artifact = await to_thread(client.add_artifact, agentSpaceId=reference["record_id"],
                                   artifactContent=document["content"], artifactType=artifact_type,
                                   fileName=f'setup-{receipt["operation_id"]}{suffix}')
        artifact_id = artifact.get("artifactId")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("security_architecture_artifact_unconfirmed")
        source_receipt = {"source_snapshot": document["snapshot"], "scope_artifact_id": artifact_id}
        try:
            response = await to_thread(client.create_threat_model,
                                       title=title, description=description,
                                       agentSpaceId=reference["record_id"],
                                       scopeDocs=[{"artifactId": artifact_id}],
                                       serviceRole=registration["service_role"])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                    "AccessDeniedException", "ValidationException", "ResourceNotFoundException"}:
                raise ValueError("security_threat_model_create_rejected") from error
            return {"kind": "threat_model_outcome_unknown", **source_receipt}
        except (BotoCoreError, TimeoutError, ConnectionError):
            return {"kind": "threat_model_outcome_unknown", **source_receipt}
        model_id = response.get("threatModelId")
        if not model_id:
            return {"kind": "threat_model_outcome_unknown", **source_receipt}
        matches = [model_id]
        created = True
    if not matches:
        return ({**previous, "kind": "threat_model_outcome_unknown"} if previous else None)
    if (selected_model is None and not created and (previous is None
            or previous.get("kind") == "threat_model_selection_required")):
        return {"kind": "threat_model_selection_required"}
    if (previous and (previous.get("native_model_ref", {}).get("record_id")
                      or previous.get("candidate_id")) not in (None, matches[0])):
        raise ValueError("native_model_identity_changed")
    try:
        response = await to_thread(client.batch_get_threat_models,
                                   agentSpaceId=reference["record_id"], threatModelIds=matches)
    except (BotoCoreError, ClientError) as error:
        if created:
            return {"kind": "threat_model_outcome_unknown", "candidate_id": matches[0], **source_receipt}
        raise ConnectionError("native_model_readback_unavailable") from error
    models = response.get("threatModels") or []
    if (response.get("notFound") or len(models) != 1
            or models[0].get("threatModelId") != matches[0]
            or models[0].get("agentSpaceId") != reference["record_id"]
            or models[0].get("title") != title or models[0].get("description") != description
            or models[0].get("serviceRole") != registration["service_role"]
            or created and models[0].get("scopeDocs") != [{"artifactId": source_receipt["scope_artifact_id"]}]
            or not isinstance(models[0].get("createdAt"), datetime)):
        if created:
            return {"kind": "threat_model_outcome_unknown", "candidate_id": matches[0], **source_receipt}
        raise ValueError("native_model_identity_unconfirmed")
    created_at = models[0]["createdAt"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    model_ref = {field: reference[field] for field in
                 ("tenant_id", "project_id", "source_connection_id")}
    model_ref.update(record_type="threat_model", record_id=matches[0],
                     record_revision=created_at.isoformat())
    if (selected_model is not None and selected_model != model_ref):
        raise ValueError("native_model_selection_denied")
    if (previous and previous.get("native_model_ref")
            and previous["native_model_ref"] != model_ref):
        raise ValueError("native_model_identity_changed")
    if not source_receipt and previous:
        source_receipt = {key: previous[key] for key in ("source_snapshot", "scope_artifact_id") if key in previous}
    return {"kind": "threat_model", "native_model_ref": model_ref, **source_receipt,
            "observed_at": datetime.now(UTC).isoformat()}
