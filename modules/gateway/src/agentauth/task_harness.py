"""Freeze server-owned Codex persona snapshots into protected Task grants.

The catalogue file is deployment configuration, never a path supplied by a Task.
It contains snapshots emitted by the shared harness, not a second compatibility
registry. Registered persona class and live model evidence remain prerequisites.
"""
# The shared TypeScript wire schema deliberately uses camelCase.
# ruff: noqa: N815
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.admin.persona_models.catalogue import persona_compatibility_class

REVISION = "codex-sdk-0.155.1/adp-v1"
CAPABILITIES = Literal[
    "repository.read", "repository.write", "branch.push", "change.create", "change.update",
    "review.submit", "change.merge", "story.create", "tests.run", "artifacts.publish",
    "agents.delegate", "aws.assume", "aws.mutate",
]
Effort = Literal["low", "medium", "high", "xhigh"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
# These are implemented by the report-only Task runtime, not inferred from a
# persona's requested permissions. Extend only together with broker qualification.
TASK_RUNTIME_CAPABILITIES = ("artifacts.publish",)


class TaskHarnessError(ValueError):
    pass


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Limits(Closed):
    maxTurns: Annotated[int, Field(ge=1, le=100)]
    maxContextBytes: Annotated[int, Field(ge=1024, le=262144)]
    maxDurationMs: Annotated[int, Field(ge=1000, le=21600000)]


class Skill(Closed):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    sha256: Digest


class Persona(Closed):
    schemaVersion: Literal[1]
    key: Annotated[str, Field(pattern=r"^gpt-[a-z][a-z0-9-]{0,62}$")]
    revision: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")]
    displayName: Annotated[str, Field(min_length=1, max_length=100)]
    instructions: Annotated[str, Field(min_length=1, max_length=24000)]
    skills: Annotated[list[Skill], Field(max_length=16)]
    requiredCapabilities: list[CAPABILITIES]
    optionalCapabilities: list[CAPABILITIES]
    surfaces: Annotated[list[Literal["github", "gitlab", "task-api", "delegation"]], Field(min_length=1)]
    completionPolicy: Literal["report", "validated-change", "review-repair-merge", "operations", "aidlc"]
    effort: Effort
    limits: Limits

    @model_validator(mode="after")
    def invariants(self):
        for values in [self.requiredCapabilities, self.optionalCapabilities, self.surfaces, [s.id for s in self.skills]]:
            if len(values) != len(set(values)):
                raise ValueError("duplicate persona value")
        if set(self.requiredCapabilities) & set(self.optionalCapabilities):
            raise ValueError("overlapping capabilities")
        required = {
            "validated-change": {"repository.read", "repository.write", "tests.run", "branch.push", "change.create"},
            "review-repair-merge": {"repository.read", "repository.write", "tests.run", "branch.push", "review.submit", "change.merge"},
            "operations": {"aws.assume"}, "aidlc": {"agents.delegate", "artifacts.publish"},
        }.get(self.completionPolicy, set())
        if not required.issubset(self.requiredCapabilities):
            raise ValueError("completion policy lacks required capabilities")
        return self


class Snapshot(Closed):
    definition: Annotated[str, Field(max_length=65536)]
    digest: Digest
    instructions: Annotated[str, Field(max_length=262144)]
    skillSources: Annotated[str, Field(max_length=2097152)]


class Layers(Closed):
    tenant: list[CAPABILITIES]
    principal: list[CAPABILITIES]
    run: list[CAPABILITIES]
    surface: list[CAPABILITIES]
    runtime: list[CAPABILITIES]


class Policy(Closed):
    personaKey: str
    personaDigest: Digest
    compatibilityClass: Literal["codex-sdk"]
    harnessRevision: Literal["codex-sdk-0.155.1/adp-v1"]
    canonicalModel: Annotated[str, Field(min_length=1, max_length=128)]
    allowedEfforts: Annotated[list[Effort], Field(min_length=1, max_length=4)]
    capabilityLayers: Layers
    limits: Limits
    deadlineMs: Annotated[int, Field(ge=1, le=9007199254740991)]


class Harness(Closed):
    snapshot: Snapshot
    policy: Policy


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_snapshot(value):
    snapshot = Snapshot.model_validate(value)
    if len(snapshot.definition.encode()) > 65536 or len(snapshot.skillSources.encode()) > 2097152:
        raise TaskHarnessError("snapshot exceeds bound")
    raw_definition = json.loads(snapshot.definition)
    if not isinstance(raw_definition, dict) or type(raw_definition.get("schemaVersion")) is not int:
        raise TaskHarnessError("invalid persona schema version")
    persona = Persona.model_validate(raw_definition)
    definition = rfc8785.dumps(persona.model_dump()).decode()
    if _sha(definition) != snapshot.digest or definition != snapshot.definition:
        raise TaskHarnessError("snapshot definition digest mismatch")
    sources = json.loads(snapshot.skillSources)
    if not isinstance(sources, list) or len(sources) > 16 or any(
        not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(v, str) for v in pair) for pair in sources
    ):
        raise TaskHarnessError("invalid skill sources")
    skills = dict(sources)
    if len(skills) != len(sources) or set(skills) != {skill.id for skill in persona.skills}:
        raise TaskHarnessError("skill source membership mismatch")
    for skill in persona.skills:
        if len(skills[skill.id].encode()) > 65536 or _sha(skills[skill.id]) != skill.sha256:
            raise TaskHarnessError("skill digest mismatch")
    instructions = "\n\n".join([persona.instructions, *(skills[skill.id] for skill in persona.skills)])
    if instructions != snapshot.instructions or len(instructions.encode()) > persona.limits.maxContextBytes:
        raise TaskHarnessError("snapshot instruction binding mismatch")
    return snapshot, persona


def validate_harness(value, *, persona, model_binding, limits):
    """Validate immutable persisted values; never read mutable catalogue on resume."""
    try:
        harness = Harness.model_validate(value)
        snapshot, definition = validate_snapshot(harness.snapshot.model_dump())
        policy = harness.policy
        from datetime import datetime
        deadline_ms = int(datetime.fromisoformat(limits["deadline_at"].replace("Z", "+00:00")).timestamp() * 1000)
        if (persona != "agent-task-" + definition.key or policy.personaKey != definition.key
                or policy.personaDigest != snapshot.digest or policy.canonicalModel != model_binding["model_id"]
                or model_binding["transport"] != "openai_responses" or model_binding["invocability_verified"] is not True
                or policy.deadlineMs > deadline_ms or policy.limits.maxTurns > limits["max_turns"]
                or definition.effort not in policy.allowedEfforts or "task-api" not in definition.surfaces):
            raise TaskHarnessError("task harness binding mismatch")
        layers = policy.capabilityLayers.model_dump()
        if any(len(layer) > 32 or len(set(layer)) != len(layer) for layer in layers.values()):
            raise TaskHarnessError("invalid capability layers")
        if any(not set(definition.requiredCapabilities).issubset(layer) for layer in layers.values()):
            raise TaskHarnessError("required capabilities unavailable")
        if not set(layers["runtime"]).issubset(TASK_RUNTIME_CAPABILITIES):
            raise TaskHarnessError("runtime capabilities unavailable")
        return harness.model_dump()
    except (ValidationError, ValueError, TypeError, KeyError, OverflowError) as error:
        raise TaskHarnessError("invalid protected task harness") from error


def freeze_harness(*, persona, model_binding, limits, service_policy, env=None):
    """Called before reservations/admission; authority comes only from server config."""
    if model_binding["transport"] != "openai_responses":
        return None
    env = os.environ if env is None else env
    try:
        if persona_compatibility_class(persona) != "codex-sdk" or persona not in service_policy["allowed_personas"]:
            raise TaskHarnessError("persona is not registered and authorized")
        path = env.get("ADP_CODEX_PERSONA_CATALOG_FILE")
        if not path:
            raise TaskHarnessError("persona catalogue unavailable")
        # Bound the read itself, not just decoding after allocating an arbitrary file.
        with Path(path).open("rb") as stream:
            raw = stream.read(2097153)
        if len(raw) > 2097152:
            raise TaskHarnessError("persona catalogue exceeds bound")
        catalogue = json.loads(raw)
        if (not isinstance(catalogue, dict) or set(catalogue) != {"schemaVersion", "snapshots"}
                or type(catalogue["schemaVersion"]) is not int or catalogue["schemaVersion"] != 1):
            raise TaskHarnessError("invalid persona catalogue")
        snapshots = catalogue["snapshots"]
        if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= 64:
            raise TaskHarnessError("invalid snapshot catalogue")
        entries = {}
        for value in snapshots:
            snapshot, definition = validate_snapshot(value)
            if definition.key in entries:
                raise TaskHarnessError("duplicate persona definition")
            entries[definition.key] = (snapshot, definition)
        snapshot, definition = entries[persona.removeprefix("agent-task-")]
        from datetime import datetime
        deadline = int(datetime.fromisoformat(limits["deadline_at"].replace("Z", "+00:00")).timestamp() * 1000)
        # Submit authority already permits the worker's report publication. No
        # executable permission is conferred by instructions or catalogue content.
        layers = {key: list(TASK_RUNTIME_CAPABILITIES) for key in ("tenant", "principal", "run", "surface", "runtime")}
        value = {"snapshot": snapshot.model_dump(), "policy": {
            "personaKey": definition.key, "personaDigest": snapshot.digest,
            "compatibilityClass": "codex-sdk", "harnessRevision": REVISION,
            "canonicalModel": model_binding["model_id"], "allowedEfforts": [definition.effort],
            "capabilityLayers": layers, "limits": {
                "maxTurns": min(definition.limits.maxTurns, limits["max_turns"]),
                "maxContextBytes": definition.limits.maxContextBytes,
                "maxDurationMs": min(definition.limits.maxDurationMs, int(service_policy["limits"]["max_duration_minutes"]) * 60000),
            }, "deadlineMs": deadline}}
        return validate_harness(value, persona=persona, model_binding=model_binding, limits=limits)
    except (OSError, ValidationError, ValueError, TypeError, KeyError) as error:
        raise TaskHarnessError("task harness prerequisite unavailable") from error


def assert_bootstrap_size(harness, *, immutable_input, model_binding, limits):
    # The worker adds bounded identity/control fields and up to four artifact
    # descriptors. Reserve 4 KiB for those; input content transfers separately.
    payload = {"harness": harness, "input": immutable_input, "model_binding": model_binding, "limits": limits}
    if len(rfc8785.dumps(payload)) + 4096 > 65536:
        raise TaskHarnessError("task harness bootstrap exceeds process frame bound")
