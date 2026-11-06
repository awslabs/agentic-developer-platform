"""Durable four-connector fencing under the existing deployment-job authority."""

import hashlib
import json
import os
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from src.shared.database import get_db
from src.shared.models.vault import ToolAdapterActivation

from .operations_release_job import AUDIENCE, NativeJobRequest, github_claims, service

router = APIRouter(prefix="/tools/adapter-release", tags=["tool-adapters"])
CONNECTORS = {"atlassian", "jira", "github", "slack"}
DRAIN_SECONDS = 20


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Release(StrictModel):
    contract_version: Literal["1.0"]
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    group: Literal["tool-connectors"]
    group_generation: int = Field(ge=1)
    backend_generation: int = Field(ge=1)


class Route(StrictModel):
    backend: Literal["adapter", "legacy", "disabled"]
    target_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class Snapshot(StrictModel):
    status: Literal["active", "legacy"]
    release: Release | None
    routes: dict[str, Route]
    deployment_id: str = Field(pattern=r"^[a-z0-9]{1,32}$")

    @model_validator(mode="after")
    def complete_group(self):
        if set(self.routes) != CONNECTORS or len({route.target_id for route in self.routes.values()}) != 4:
            raise ValueError("Exactly four distinct connector targets required")
        for connector, route in self.routes.items():
            expected = "adapter" if self.status == "active" else "disabled" if connector == "atlassian" else "legacy"
            if route.backend != expected:
                raise ValueError("A connector group cannot mix active backends or route Atlassian to legacy Jira")
        if self.status == "active" and self.release is None:
            raise ValueError("An active group must have a release")
        return self


class Plan(StrictModel):
    api_id: str = Field(pattern=r"^[a-z0-9]{1,32}$")
    stage: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    before: Snapshot
    after: Snapshot

    @model_validator(mode="after")
    def next_generation(self):
        candidate, previous = self.after.release, self.before.release
        if self.after.status != "active" or candidate is None:
            raise ValueError("Handoff must name an active candidate")
        for field in ("group_generation", "backend_generation"):
            expected = getattr(previous, field) + 1 if previous else 1
            if getattr(candidate, field) != expected:
                raise ValueError("A new handoff must advance both original generations exactly once")
        return self


class TransitionRequest(StrictModel):
    job: NativeJobRequest
    action: Literal["read", "fence", "activate", "rollback", "restore"]
    plan: Plan
    observed: Snapshot | None = None


def require(condition, status=409):
    if not condition:
        raise HTTPException(status, "adapter_transition_refused")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def rollback_snapshot(plan):
    previous = plan.before.model_dump()
    previous["release"] = {
        **(previous["release"] or plan.after.release.model_dump()),
        "group_generation": plan.after.release.group_generation + 1,
        "backend_generation": plan.after.release.backend_generation + 1,
    }
    return previous


def state(row):
    if row is None:
        return None
    return {"status": row.status, "release": row.release, "transition": row.transition}


async def transition(db, body, *, now=None, configured_release=None):
    now = time.time() if now is None else now
    plan = body.plan.model_dump()
    owner = {key: getattr(body.job, key) for key in ("correlation", "source_revision", "workflow", "role_arn", "mutation_role_arn")}
    require(body.job.workflow == "tools-platform-deploy.yml", 403)
    require(body.job.source_revision == body.plan.after.release.source_revision, 403)
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(84548453)"))
    rows = list(await db.scalars(select(ToolAdapterActivation).with_for_update()))
    require(len(rows) <= 1)
    row = rows[0] if rows else None
    require(row is None or row.group == "tool-connectors")
    digest = fingerprint(plan)
    if body.action == "read":
        return state(row)
    require(body.observed is None or body.action in {"activate", "restore"})
    if body.action == "fence":
        if row is not None and row.transition and row.transition.get("plan_sha256") == digest:
            require(row.transition.get("owner") == owner)
            return state(row)
        before = body.plan.before
        require(
            (row is None and before.release is None and before.status == "legacy")
            or (
                row is not None
                and before.release is not None
                and row.status == before.status
                and row.release == before.release.model_dump()
                and row.transition is not None
                and row.transition.get("phase") in {"active", "restored"}
                and row.transition.get("verified_routes") == before.model_dump()["routes"]
                and row.transition["plan"]["api_id"] == plan["api_id"]
                and row.transition["plan"]["stage"] == plan["stage"]
                and row.transition.get("verified_deployment_id") == before.deployment_id
            )
        )
        if row is None:
            row = ToolAdapterActivation(group="tool-connectors", status="fenced", release=plan["after"]["release"])
            db.add(row)
        row.status = "fenced"
        row.release = plan["after"]["release"]
        row.transition = {
            "owner": owner,
            "plan": plan,
            "plan_sha256": digest,
            "phase": "fenced",
            "drain_until": now + DRAIN_SECONDS,
            "verified_routes": None,
        }
    else:
        saved = row.transition if row is not None else None
        if body.action == "rollback" and saved is not None and saved.get("owner") != owner:
            # The route deployment can finish before its workflow fails while
            # publishing evidence. A newly authorized rollback job may recover
            # that completed activation, never an in-flight or different plan.
            # The endpoint has already verified the current exclusive job lease.
            previous_owner = saved.get("owner", {})
            require(
                saved.get("phase") == "active"
                and saved.get("plan_sha256") == digest
                and row.status == "active"
                and row.release == plan["after"]["release"]
                and saved.get("verified_routes") == plan["after"]["routes"]
                and saved.get("verified_deployment_id") == plan["after"]["deployment_id"]
                and all(previous_owner.get(key) == owner[key] for key in owner if key != "correlation")
            )
            saved = {**saved, "owner": owner, "recovered_from_owner": previous_owner}
        require(saved is not None and saved.get("owner") == owner and saved.get("plan_sha256") == digest)
        if body.action == "rollback":
            require(saved["phase"] in {"fenced", "active", "rollback", "restored"})
            if saved["phase"] in {"rollback", "restored"}:
                return state(row)
            row.status = "fenced"
            row.release = rollback_snapshot(body.plan)["release"]
            row.transition = {**saved, "phase": "rollback", "drain_until": now + DRAIN_SECONDS, "verified_routes": None}
        else:
            phase, terminal = ("fenced", "active") if body.action == "activate" else ("rollback", "restored")
            expected = plan["after"] if body.action == "activate" else rollback_snapshot(body.plan)
            require(body.observed is not None and body.observed.model_dump() == expected)
            require(saved["phase"] in {phase, terminal} and now >= saved["drain_until"])
            require(configured_release == expected["release"])
            row.status, row.release = expected["status"], expected["release"]
            row.transition = {**saved, "phase": terminal, "verified_routes": expected["routes"], "verified_deployment_id": expected["deployment_id"]}
    try:
        await db.flush()
        result = state(row)
        await db.commit()
        return result
    except IntegrityError:
        await db.rollback()
        require(False)


@router.post("/transition")
async def update_transition(body: TransitionRequest, request: Request, response: Response, guard=Depends(service), db=Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    require(scheme.lower() == "bearer" and 0 < len(token) <= 8192 and not any(character.isspace() for character in token), 403)
    claims = await github_claims(token, audience=AUDIENCE)
    await guard.authorize(body.job, claims)
    try:
        configured = json.loads(os.environ.get("ADP_TOOL_ADAPTER_RELEASE", "null"))
    except ValueError:
        configured = None
    return {"state": await transition(db, body, configured_release=configured)}
