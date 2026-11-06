"""Conservative qualification of one pinned Well-Architected Agent Start intent."""

import asyncio
from botocore.config import Config
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import re

from l5_services.adapters.protocol import AdapterOutcome
from botocore.exceptions import BotoCoreError, ClientError


@dataclass(frozen=True)
class PreparedGeneration:
    tenant_id: str
    project_id: str
    profile_arn: str
    name: str
    creator: str
    model_hash: str
    request_hash: str
    created_after: datetime
    created_before: datetime
    scope: dict
    additional_context: dict

    def __post_init__(self):
        if (not all((self.tenant_id, self.project_id, self.profile_arn, self.name,
                     self.creator, self.model_hash, self.request_hash,
                     self.scope, self.additional_context))
                or self.created_after.tzinfo is None or self.created_before.tzinfo is None
                or self.created_after > self.created_before):
            raise ValueError("complete pinned generation identity required")

    def to_record(self):
        return {**vars(self), "created_after": self.created_after.isoformat(),
                "created_before": self.created_before.isoformat()}

    @classmethod
    def from_record(cls, record):
        return cls(**{**record, "created_after": datetime.fromisoformat(record["created_after"]),
                      "created_before": datetime.fromisoformat(record["created_before"])})


def _request_context(run, name):
    admission = run.admission
    profile = admission["native_profile_ref"]
    model_hash = admission["generation_model_hash"]
    archive = run.manifest["archive"]
    uri = run.manifest.get("provider_source_uri")
    if (not isinstance(uri, str) or not re.fullmatch(r"s3://[a-z0-9][a-z0-9.-]{2,62}/[a-zA-Z0-9._/-]+", uri)
            or not isinstance(archive.get("version"), str) or not archive["version"]
            or not isinstance(archive.get("bytes"), int) or not 0 < archive["bytes"] <= 25 * 1024 * 1024
            or set(admission["generation_scope"].get("pillars", [])) - {
                "COST_OPTIMIZATION", "SECURITY", "RESILIENCE", "PERFORMANCE"}):
        raise ValueError("documented native source or review scope unavailable")
    context = {"s3Uri": uri, "s3VersionId": archive["version"],
               "lensArns": ["arn:aws:wellarchitected::aws:lens/wellarchitected"]}
    request_hash = sha256(json.dumps({
        "profileArn": profile["record_id"], "types": ["ARCHITECTURE"], "name": name,
        "scope": admission["generation_scope"], "additionalContext": context,
        "manifest": run.manifest,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return context, request_hash


def prepare_generation(run, attempt_id, *, now):
    """Freeze the exact native request and recovery identity before a paid Start."""
    admission = run.admission
    profile = admission["native_profile_ref"]
    if (profile["tenant_id"] != run.tenant_id or profile["project_id"] != run.project_id
            or profile["record_revision"] != admission["profile_hash"]
            or run.manifest["scope"] != admission["scope"]
            or run.manifest["run_id"] != run.run_id):
        raise ValueError("pinned generation profile or scope changed")
    scope = admission["generation_scope"]
    model_hash = admission["generation_model_hash"]
    creator = admission["generation_creator"]
    name = "adp-wa-" + sha256(f"{run.tenant_id}:{run.run_id}:{attempt_id}".encode()).hexdigest()[:32]
    context, request_hash = _request_context(run, name)
    return PreparedGeneration(run.tenant_id, run.project_id, profile["record_id"], name,
                              creator, model_hash, request_hash, now, now + timedelta(minutes=15),
                              scope, context)


def _matches_identity(item, prepared):
    import re

    created_at = item.get("createdAt")
    creator = item.get("createdBy")
    selected_role = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/(.+)", prepared.creator)
    same_creator = creator == prepared.creator
    if selected_role is not None and isinstance(creator, str):
        same_creator = same_creator or re.fullmatch(
            rf"arn:aws:sts::{selected_role[1]}:assumed-role/{re.escape(selected_role[2].rsplit('/', 1)[-1])}/[^/]+", creator) is not None
    return (item.get("profileArn") == prepared.profile_arn
            and item.get("name") == prepared.name
            and same_creator
            and isinstance(created_at, datetime) and created_at.tzinfo is not None
            and prepared.created_after.astimezone(UTC) <= created_at.astimezone(UTC)
            <= prepared.created_before.astimezone(UTC))


def _native(prepared, generation_id):
    return {"tenant_id": prepared.tenant_id, "project_id": prepared.project_id,
            "profile_arn": prepared.profile_arn, "generation_id": generation_id}


def _native_json(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported native recommendation field: {type(value).__name__}")


def start_generation(client, prepared):
    """Submit one pre-admitted intent; caller persists intent before this call."""
    if client.meta.config.retries.get("total_max_attempts") != 1:
        raise ValueError("non-idempotent Start requires disabled SDK retries")
    response = client.start_agent_recommendation_generation(
        profileArn=prepared.profile_arn, types=["ARCHITECTURE"], name=prepared.name,
        additionalContext=prepared.additional_context, scope=prepared.scope,
    )
    if not response.get("id") or not _matches_identity(response, prepared):
        return AdapterOutcome("outcome_unknown")
    return AdapterOutcome("accepted", native_job_ref=_native(prepared, response["id"]))


def reconcile_generation(client, prepared, *, max_pages=20):
    """Read only: incomplete or ambiguous native results never authorize another Start."""
    token = None
    seen_tokens = set()
    candidates = []
    try:
        for _ in range(max_pages):
            request = {"profileArn": prepared.profile_arn, "recommendationType": "ARCHITECTURE"}
            if token:
                request["nextToken"] = token
            response = client.list_agent_recommendation_generations(**request)
            items = response["items"]
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                return AdapterOutcome("outcome_unknown")
            candidates.extend(item["id"] for item in items if item.get("name") == prepared.name
                              and item.get("profileArn") == prepared.profile_arn)
            token = response.get("nextToken")
            if not token:
                break
            if token in seen_tokens:
                return AdapterOutcome("outcome_unknown")
            seen_tokens.add(token)
        else:
            return AdapterOutcome("outcome_unknown")
        if len(candidates) != 1 or not candidates[0]:
            return AdapterOutcome("outcome_unknown")
        detail = client.get_agent_recommendation_generation(
            profileArn=prepared.profile_arn, generationId=candidates[0]
        )
    except (BotoCoreError, ClientError, ConnectionError, TimeoutError,
            KeyError, TypeError, ValueError, OSError, RuntimeError):
        return AdapterOutcome("outcome_unknown")
    if (not isinstance(detail, dict) or detail.get("id") != candidates[0] or not _matches_identity(detail, prepared)
            or detail.get("scope") != prepared.scope
            or detail.get("additionalContext") != prepared.additional_context):
        return AdapterOutcome("outcome_unknown")
    return AdapterOutcome("accepted", native_job_ref=_native(prepared, candidates[0]))


def cancel_generation():
    """The checked SDK model has no Stop operation; preserve native observation."""
    return AdapterOutcome("cancel_unsupported")


class GenerationAdapter:
    """Run a verified, SQL-prepared generation with delegated provider credentials."""

    def __init__(self, source_reader, *, observation_sessions=None):
        self.source_reader = source_reader
        self.observation_sessions = observation_sessions

    async def read_source_receipt(self, reference):
        if not callable(getattr(self.source_reader, "read_source_receipt", None)):
            raise ValueError("qualified provider source reader required")
        return await self.source_reader.read_source_receipt(reference)

    @staticmethod
    def _client(session, *, mutation=False, collection=False):
        if session is None:
            raise ValueError("delegated provider session required")
        options = {"config": Config(retries={"total_max_attempts": 1})} if mutation else {}
        if collection:
            options = {"config": Config(retries={"total_max_attempts": 1}, connect_timeout=3, read_timeout=15)}
        return session.client("wellarchitected", **options)

    async def submit(self, run, manifest, attempt, session):
        if manifest.get("provider_coverage") != "provider_coverage_unverified" or not attempt.prepared_request:
            raise ValueError("qualified provider source and prepared request required")
        prepared = PreparedGeneration.from_record(attempt.prepared_request)
        client = self._client(session, mutation=True)
        if self.observation_sessions is not None:
            from l5_services.adapters.well_architected.transport_observations import StartObserver
            StartObserver(self.observation_sessions, run, attempt, prepared).install(client)
        outcome = await asyncio.to_thread(start_generation, client, prepared)
        return self._with_name(outcome, prepared)

    async def reconcile_submit(self, run, attempt, session):
        if not attempt.prepared_request:
            raise ValueError("original prepared request required")
        prepared = PreparedGeneration.from_record(attempt.prepared_request)
        outcome = await asyncio.to_thread(reconcile_generation, self._client(session), prepared)
        return self._with_name(outcome, prepared)

    @staticmethod
    def _with_name(outcome, prepared):
        if outcome.classification != "accepted":
            return outcome
        return AdapterOutcome("accepted", native_job_ref={**outcome.native_job_ref, "name": prepared.name})

    async def observe(self, run, native_job_ref, attempt, session):
        response = await asyncio.to_thread(
            self._client(session).get_agent_recommendation_generation,
            profileArn=native_job_ref["profile_arn"], generationId=native_job_ref["generation_id"])
        if (response.get("id") != native_job_ref["generation_id"]
                or response.get("profileArn") != native_job_ref["profile_arn"]
                or response.get("name") != native_job_ref["name"]
                or response.get("scope") != run.admission["generation_scope"]
                or response.get("additionalContext") != _request_context(run, native_job_ref["name"])[0]):
            return AdapterOutcome("unknown")
        status = response.get("status")
        classification = {"QUEUED": "running", "IN_PROGRESS": "running",
                          "COMPLETED": "completed", "ERROR": "failed"}.get(status, "unknown")
        return AdapterOutcome(classification, observation={
            "native_job_ref": native_job_ref, "outcome": classification,
            "observed_at": datetime.now(UTC).isoformat(), "native_status": status})


    async def collect(self, run, native_job_ref, attempt, session, *, next_token=None):
        client = self._client(session, collection=True)
        request = {"profileArn": native_job_ref["profile_arn"], "maxResults": 1000}
        if next_token is not None:
            request["nextToken"] = next_token
        response = await asyncio.wait_for(asyncio.to_thread(client.list_agent_recommendations, **request), timeout=20)
        if not isinstance(response.get("items"), list):
            raise ValueError("native recommendation page is incomplete")
        pending = []
        excluded = []
        for item in response["items"]:
            if (item.get("profileArn") != native_job_ref["profile_arn"]
                    or item.get("generationId") != native_job_ref["generation_id"]):
                excluded.append({"recommendationArn": item.get("recommendationArn"),
                                 "generationId": item.get("generationId"),
                                 "reason": "unattributed" if not item.get("generationId") else "different_generation"})
                continue
            if not item.get("recommendationArn"):
                raise ValueError("attributable recommendation has no native ID")
            pending.append({key: item[key] for key in ("recommendationArn", "generationId", "profileArn")})
        return AdapterOutcome("completed", result={"native_job_ref": native_job_ref,
                                                   "recommendations": [], "pending_recommendations": pending,
                                                   "excluded": excluded,
                                                   "request_token": next_token,
                                                   "next_token": response.get("nextToken"),
                                                   "observed_at": datetime.now(UTC).isoformat()})

    async def collect_detail(self, run, native_job_ref, attempt, session, *, recommendation):
        if (recommendation.get("profileArn") != native_job_ref["profile_arn"]
                or recommendation.get("generationId") != native_job_ref["generation_id"]):
            raise ValueError("recommendation belongs to another generation")
        detail = await asyncio.wait_for(asyncio.to_thread(
            self._client(session, collection=True).get_agent_recommendation,
            recommendationArn=recommendation["recommendationArn"]), timeout=20)
        if (detail.get("recommendationArn") != recommendation["recommendationArn"]
                or detail.get("profileArn") != native_job_ref["profile_arn"]
                or detail.get("generationId") != native_job_ref["generation_id"]):
            raise ValueError("recommendation detail belongs to another generation")
        return json.loads(json.dumps({key: value for key, value in detail.items()
                                      if key != "ResponseMetadata"}, default=_native_json))
