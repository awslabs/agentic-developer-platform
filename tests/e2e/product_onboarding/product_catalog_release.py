"""Bind catalog recovery to a signed gateway release and observed schema head."""

import hashlib
import json
import re
from copy import deepcopy

from src.orchestration.deployment_runtime_contract import ReleaseArtifact, RuntimeComponent


WORKFLOW = ".github/workflows/gateway-deploy.yml"


def pin_catalog_release(state, selection, store, observe):
    plan = state.document["catalog"]["plan"]
    if (not isinstance(selection, dict) or selection.get("repository") != "aws-e/adp"
            or selection.get("environment") != "dev" or selection.get("workflow_path") != WORKFLOW
            or selection.get("source_revision") != state.binding["source_revision"]
            or selection["source_revision"] != plan["source_revision"]
            or any(not isinstance(selection.get(key), str) or not re.fullmatch(r"[0-9a-f]{40}", selection[key])
                   for key in ("source_revision", "workflow_revision"))
            or any(type(selection.get(key)) is not int or selection[key] <= 0
                   for key in ("repository_id", "run_id", "attempt"))
            or not isinstance(selection.get("account_id"), str)
            or not re.fullmatch(r"[0-9]{12}", selection["account_id"])
            or not isinstance(selection.get("schema_head"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", selection["schema_head"])
            or not isinstance(selection.get("image_digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", selection["image_digest"])
            or any(not isinstance(selection.get(key), str) or not selection[key]
                   for key in ("region", "resource_id"))):
        raise ValueError("Catalog release selection is incomplete or differs from owned intent")
    signed = store.read(
        account=selection["account_id"], environment="dev", region=selection["region"],
        repository=selection["repository"], repository_id=selection["repository_id"],
        run_id=selection["run_id"], attempt=selection["attempt"], kind="release",
        name="gateway-backend", workflow_path=WORKFLOW, workflow_revision=selection["workflow_revision"],
    )
    if not isinstance(signed, tuple) or len(signed) != 3:
        raise ValueError("Selected signed catalog release is missing")
    payload, digest, reference = signed
    if (not isinstance(payload, bytes) or not isinstance(digest, str)
            or digest != hashlib.sha256(payload).hexdigest()
            or not isinstance(reference, str) or not reference):
        raise ValueError("Catalog release receipt has no verified artifact bytes")
    release = ReleaseArtifact.model_validate_json(payload)
    expected = {
        "schema_version": 1, "repository_id": selection["repository_id"],
        "run_id": selection["run_id"], "run_attempt": selection["attempt"],
        "source_revision": selection["source_revision"], "workflow_revision": selection["workflow_revision"],
        "workflow_path": WORKFLOW, "account_id": selection["account_id"],
        "region": selection["region"], "resource_id": selection["resource_id"],
        "component": "gateway-backend", "image_digest": selection["image_digest"],
        "migration_head": selection["schema_head"],
    }
    if any(getattr(release, key) != value for key, value in expected.items()):
        raise ValueError("Catalog release differs from the selected artifact or schema")
    observed = observe(release, digest, reference)
    if not isinstance(observed, dict) or not isinstance(observed.get("runtime_receipt"), dict):
        raise ValueError("Independent catalog runtime receipt is missing")
    runtime = RuntimeComponent.model_validate(observed["runtime_receipt"])
    head = selection["schema_head"]
    pods = runtime.pod_uids
    if (runtime.component != "gateway-backend" or runtime.actual_revision != release.source_revision
            or runtime.image_digest != release.image_digest or runtime.artifact_hash != digest
            or runtime.evidence_ref != reference or runtime.migration_head != head
            or runtime.tick_digest != release.image_digest or not pods
            or observed.get("source_revision") != release.source_revision
            or observed.get("pod_uids") != pods or observed.get("tick_verified") is not True
            or observed.get("tick_digest") != release.image_digest
            or observed.get("database_heads") != [head] * len(pods)
            or observed.get("image_heads") != [head] * len(pods)
            or observed.get("observation_ref") != "runtime:sha256:" + hashlib.sha256(
                json.dumps(runtime.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()):
        raise ValueError("Serving catalog artifact or schema differs from the signed release")
    binding = {"source_revision": release.source_revision, "workflow_revision": release.workflow_revision,
               "run_id": release.run_id, "attempt": release.run_attempt, "image_digest": release.image_digest,
               "schema_head": head, "artifact_hash": digest, "release_ref": reference,
               "observation_ref": observed["observation_ref"]}
    previous = state.document.get("catalog_release")
    if previous is not None and {key: value for key, value in previous.items() if key != "observation_ref"} != {
        key: value for key, value in binding.items() if key != "observation_ref"
    }:
        raise ValueError("Catalog recovery release differs from original selected artifact")
    state.save({**state.document, "catalog_release": deepcopy(binding)})
    return deepcopy(binding)
