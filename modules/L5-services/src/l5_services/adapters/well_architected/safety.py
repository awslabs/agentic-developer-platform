"""Recheck durable Product and native scope immediately before a paid review."""

import asyncio
import hashlib
import json
import re

from datetime import UTC, datetime, timedelta

from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.workers.execution import AuthorityRevoked


async def verify_start_scope(sessions, source_client, archive_store, run, claim, delegated, *, now=None):
    now = now or datetime.now(UTC)
    admission = run.admission or {}
    scope = admission.get("scope") or {}
    identity = delegated
    if (run.service_id != "well_architected" or run.input.get("capability") != "architecture_review"
            or any(scope.get(field) != getattr(run, field) for field in ("tenant_id", "product_id", "project_id"))
            or claim.run_id != run.run_id or claim.tenant_id != run.tenant_id
            or not all(isinstance(getattr(identity, field, None), str) and getattr(identity, field)
                       for field in ("account_id", "role_arn", "region"))):
        raise AuthorityRevoked("current Well-Architected scope unavailable")
    async with sessions() as session:
        config = await session.get(Configuration, (run.tenant_id, run.configuration_id))
        version = await session.get(ConfigurationVersion, (
            run.tenant_id, run.configuration_id, run.configuration_revision))
        binding = await session.get(Binding, (run.tenant_id, run.binding_id))
        settings = version.settings if version is not None else {}
        review = settings.get("well_architected") or {}
        profile = admission.get("native_profile_ref") or {}
        if (config is None or version is None or binding is None
                or (config.product_id, config.project_id, config.service_id, config.current_revision,
                    config.handoff_state) != (run.product_id, run.project_id, run.service_id,
                                              run.configuration_revision, "linked")
                or version.state != "ready" or version.gaps or settings.get("enabled") is not True
                or version.hash != admission.get("configuration_hash")
                or settings.get("source_refs") != run.input.get("sources")
                or settings.get("connection_ref") != admission.get("connection_ref")
                or review.get("review_mode") != "source_only"
                or (binding.configuration_id, binding.product_id, binding.project_id, binding.service_id,
                    binding.generation, binding.state) != (
                    config.configuration_id, run.product_id, run.project_id, run.service_id,
                    run.binding_generation, "ready")
                or binding.gaps or binding.scope != scope
                or binding.connection_ref != admission.get("connection_ref")
                or binding.native_resource_ref != profile
                or any((binding.provider_identity or {}).get(field) != getattr(identity, field)
                       for field in ("account_id", "role_arn", "region"))
                or profile.get("record_revision") != admission.get("profile_hash")
                or profile.get("tenant_id") != run.tenant_id or profile.get("project_id") != run.project_id
                or profile.get("source_connection_id") != binding.connection_ref.get("record_id")):
            raise AuthorityRevoked("current Product binding or profile changed")
    snapshots = admission.get("source_snapshots")
    if (not isinstance(snapshots, list) or not snapshots
            or [snapshot.get("source_ref") for snapshot in snapshots] != run.input["sources"]):
        raise AuthorityRevoked("selected Product sources changed")
    if (run.manifest.get("source_snapshot_ids") != [snapshot["snapshot_id"] for snapshot in snapshots]
            or [item.get("source_ref") for item in run.manifest.get("architecture", {}).get("sources", [])]
                != run.input["sources"]):
        raise AuthorityRevoked("selected source manifest incomplete")
    for snapshot in snapshots:
        request = {"delegation_id": admission["delegation_id"],
                   "delegation_revision": admission["delegation_version"],
                   "operation_id": run.run_id, "attempt_id": claim.work_id,
                   "lease_owner": claim.owner, "lease_fence": claim.fence}
        try:
            receipt = await source_client.read_source(run.product_id, run.project_id,
                                                      snapshot["snapshot_id"], request)
        except Exception as error:
            raise AuthorityRevoked("current Product source unavailable") from error
        if receipt != snapshot:
            raise AuthorityRevoked("selected Product source revoked or changed")
    profile_arn = profile.get("record_id")
    if (not isinstance(profile_arn, str) or not re.fullmatch(
            rf"arn:aws:wellarchitected:{re.escape(identity.region)}:{re.escape(identity.account_id)}:agent-profile/[A-Za-z0-9_-]+",
            profile_arn) or not isinstance(review.get("pillars"), list)
            or set(review["pillars"]) != set(admission.get("generation_scope", {}).get("pillars", []))):
        raise AuthorityRevoked("native review identity changed")

    def native_readback():
        client = delegated.client("wellarchitected")
        profile_data = client.get_agent_profile(profileArn=profile_arn)
        contexts = []
        token = None
        seen = set()
        for _ in range(20):
            page = client.list_agent_contexts(**({"profileArn": profile_arn, "nextToken": token}
                                                if token else {"profileArn": profile_arn}))
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise AuthorityRevoked("native context readback unavailable")
            contexts.extend(page["items"])
            token = page.get("nextToken")
            if not token:
                break
            if not isinstance(token, str) or token in seen:
                raise AuthorityRevoked("native context readback incomplete")
            seen.add(token)
        else:
            raise AuthorityRevoked("native context readback incomplete")
        cutoff = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        token = None
        seen_tokens = set()
        count = 0
        for _ in range(20):
            request = {"profileArn": profile_arn, "recommendationType": "ARCHITECTURE"}
            if token:
                request["nextToken"] = token
            page = client.list_agent_recommendation_generations(**request)
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise AuthorityRevoked("daily native generation quota unverified")
            for item in page["items"]:
                created = item.get("createdAt") if isinstance(item, dict) else None
                if (not isinstance(item, dict) or item.get("profileArn") != profile_arn
                        or not isinstance(created, datetime) or created.tzinfo is None):
                    raise AuthorityRevoked("daily native generation quota unverified")
                if cutoff <= created.astimezone(UTC) < cutoff + timedelta(days=1):
                    count += 1
            if count >= 5:
                raise AuthorityRevoked("daily native generation quota exhausted")
            token = page.get("nextToken")
            if not token:
                break
            if not isinstance(token, str) or token in seen_tokens:
                raise AuthorityRevoked("daily native generation quota unverified")
            seen_tokens.add(token)
        else:
            raise AuthorityRevoked("daily native generation quota unverified")
        return profile_data, contexts

    try:
        observed, contexts = await asyncio.to_thread(native_readback)
    except AuthorityRevoked:
        raise
    except Exception as error:
        raise AuthorityRevoked("native profile or quota readback unavailable") from error
    if not isinstance(observed, dict):
        raise AuthorityRevoked("native profile readback unavailable")
    evidence = {"arn": profile_arn, "aggregation": observed.get("aggregationConfiguration"),
                "role": observed.get("executionRoleArn"), "pillars": observed.get("pillars"),
                "contexts": contexts, "eligible": observed.get("eligibleForArchitectureGeneration"),
                "scheduled": observed.get("eligibleForScheduledGeneration"),
                "field_errors": observed.get("fieldErrors")}
    fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()
    if (observed.get("arn") != profile_arn or contexts
            or observed.get("aggregationConfiguration") != []
            or observed.get("executionRoleArn") != review.get("execution_role_ref", {}).get("record_id")
            or set(observed.get("pillars") or []) != set(review["pillars"])
            or observed.get("eligibleForArchitectureGeneration") is not True
            or observed.get("fieldErrors") or fingerprint != admission["profile_hash"]):
        raise AuthorityRevoked("native profile eligibility or revision changed")
    archive = (run.manifest or {}).get("archive") or {}
    if (not isinstance(archive.get("bytes"), int) or archive["bytes"] > 25 * 1024 * 1024
            or archive["bytes"] < 1 or not archive.get("version")
            or (run.manifest or {}).get("archive_readback") != {
                "artifact": archive, "sha256": archive.get("sha256"), "bytes": archive["bytes"]}):
        raise AuthorityRevoked("verified source object or version unavailable")
    bucket = getattr(archive_store, "bucket", None)
    selected_store = review.get("source_store_ref")
    if selected_store is not None and selected_store.get("record_id", "").removeprefix("arn:aws:s3:::") != bucket:
        raise AuthorityRevoked("archive bucket is not the Product-selected source store")
    region = getattr(getattr(getattr(archive_store, "client", None), "meta", None), "region_name", None)
    if (not isinstance(bucket, str) or not bucket or region != identity.region
            or not archive.get("artifact_id", "").startswith(
                f"l5/well_architected/work/{run.run_id}/")
            or run.manifest.get("provider_source_uri") !=
                f"s3://{bucket}/{archive['artifact_id']}"):
        raise AuthorityRevoked("source bucket, Region or object changed")
    try:
        content = await archive_store.get_archive(run.run_id, archive)
    except Exception as error:
        raise AuthorityRevoked("source archive readback unavailable") from error
    from l5_services.adapters.well_architected.results import _source_files

    try:
        files = _source_files(run.manifest, content)
    except (KeyError, TypeError, ValueError) as error:
        raise AuthorityRevoked("source archive changed") from error
    selected = {item["archive_path"] for item in run.manifest["architecture"]["files"]
                if item.get("archive_path")}
    if set(files) != selected or not selected:
        raise AuthorityRevoked("selected source coverage incomplete")
