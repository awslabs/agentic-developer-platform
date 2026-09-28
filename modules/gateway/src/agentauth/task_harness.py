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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.admin.persona_models.catalogue import persona_compatibility_class

REVISION = "codex-sdk-0.155.1/adp-v1"
CAPABILITIES = Literal[
    "repository.read",
    "repository.write",
    "branch.push",
    "change.create",
    "change.update",
    "review.submit",
    "change.merge",
    "story.create",
    "tests.run",
    "artifacts.publish",
    "agents.delegate",
    "aws.assume",
    "aws.mutate",
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
    maxTurns: Annotated[int, Field(ge=1, le=1000)]
    maxContextBytes: Annotated[int, Field(ge=1024, le=262144)]
    maxDurationMs: Annotated[int, Field(ge=1000, le=21600000)]


class Skill(Closed):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    sha256: Digest
    requiredCapabilities: list[CAPABILITIES] | None = None
    requiredTools: Annotated[list[Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$")]], Field(max_length=32)] | None = None

    @model_validator(mode="after")
    def unique_capabilities(self):
        values = self.requiredCapabilities or []
        if len(values) != len(set(values)) or len(self.requiredTools or []) != len(set(self.requiredTools or [])):
            raise ValueError("duplicate skill capability")
        return self


class RuleReference(Closed):
    path: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_./-]+$", max_length=255)]
    sha256: Digest


class SharedRules(Closed):
    version: Literal[1]
    persona: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    sources: Annotated[list[RuleReference], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def paths(self):
        if len({s.path for s in self.sources}) != len(self.sources) or any(
            s.path.startswith("/") or any(p in ("", ".", "..") for p in s.path.split("/")) for s in self.sources
        ):
            raise ValueError("invalid shared rule paths")
        return self


class Persona(Closed):
    schemaVersion: Literal[1]
    key: Annotated[str, Field(pattern=r"^gpt-[a-z][a-z0-9-]{0,62}$")]
    revision: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")]
    displayName: Annotated[str, Field(min_length=1, max_length=100)]
    instructions: Annotated[str, Field(min_length=1, max_length=24000)]
    sharedRules: SharedRules | None = None
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
            "validated-change": {"repository.read", "repository.write", "tests.run", "change.create"},
            "review-repair-merge": {"repository.read", "repository.write", "tests.run", "branch.push", "review.submit", "change.merge"},
            "operations": {"aws.assume"},
            "aidlc": {"agents.delegate", "artifacts.publish"},
        }.get(self.completionPolicy, set())
        if not required.issubset(self.requiredCapabilities):
            raise ValueError("completion policy lacks required capabilities")
        if self.sharedRules and self.key != "gpt-" + self.sharedRules.persona:
            raise ValueError("shared rules belong to another persona")
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


class RuntimeTool(Closed):
    permission: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}\.[a-z][a-z0-9_]{0,63}$")]
    capability: CAPABILITIES
    definition: dict

    @model_validator(mode="after")
    def reviewed_definition(self):
        from src.agentauth.task_responses_tools_contract import TaskFunction
        from src.agentauth.task_tool_policy import codex_tool_name

        tool = TaskFunction.model_validate(self.definition)
        if tool.name != codex_tool_name(self.permission):
            raise ValueError("tool permission/function mismatch")
        return self


class Harness(Closed):
    traceparent: str | None = Field(default=None, pattern=r"^00-[a-f0-9]{32}-[a-f0-9]{16}-0[01]$")
    snapshot: Snapshot
    policy: Policy
    tools: Annotated[list[RuntimeTool], Field(min_length=1, max_length=64)] | None = None

    @field_validator("traceparent")
    @classmethod
    def valid_traceparent(cls, value):
        if value is not None and (value.split("-")[1] == "0" * 32 or value.split("-")[2] == "0" * 16):
            raise ValueError("zero trace identity")
        return value


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
    definition = rfc8785.dumps(persona.model_dump(exclude_none=True)).decode()
    if _sha(definition) != snapshot.digest or definition != snapshot.definition:
        raise TaskHarnessError("snapshot definition digest mismatch")
    sources = json.loads(snapshot.skillSources)
    if (
        not isinstance(sources, list)
        or len(sources) > 16
        or any(not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(v, str) for v in pair) for pair in sources)
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
        if (
            persona != "agent-task-" + definition.key
            or policy.personaKey != definition.key
            or policy.personaDigest != snapshot.digest
            or policy.canonicalModel != model_binding["model_id"]
            or model_binding["transport"] != "openai_responses"
            or model_binding["invocability_verified"] is not True
            or policy.deadlineMs > deadline_ms
            or policy.limits.maxTurns > limits["max_turns"]
            or definition.effort not in policy.allowedEfforts
            or "task-api" not in definition.surfaces
        ):
            raise TaskHarnessError("task harness binding mismatch")
        layers = policy.capabilityLayers.model_dump()
        if any(len(layer) > 32 or len(set(layer)) != len(layer) for layer in layers.values()):
            raise TaskHarnessError("invalid capability layers")
        if any(not set(definition.requiredCapabilities).issubset(layer) for layer in layers.values()):
            raise TaskHarnessError("required capabilities unavailable")
        for skill in definition.skills:
            required = set(skill.requiredCapabilities or [])
            declared = set(definition.requiredCapabilities) | set(definition.optionalCapabilities)
            if not required.issubset(declared) or any(not required.issubset(layer) for layer in layers.values()):
                raise TaskHarnessError("required skill capabilities unavailable")
        tools = harness.tools or []
        for skill in definition.skills:
            if not set(skill.requiredTools or []).issubset({tool.definition["name"] for tool in tools}):
                raise TaskHarnessError("required skill tool unavailable")
        permissions = [tool.permission for tool in tools]
        if len(permissions) != len(set(permissions)):
            raise TaskHarnessError("duplicate runtime permission")
        runtime_capabilities = {*TASK_RUNTIME_CAPABILITIES, *(tool.capability for tool in tools)}
        from src.agentauth.task_model_binding import TASK_RESPONSES_TOOLS_REQUEST_SHAPE

        if bool(tools) != (model_binding["request_shape_version"] == TASK_RESPONSES_TOOLS_REQUEST_SHAPE):
            raise TaskHarnessError("tool profile is not qualified")
        if any(tool.capability not in layer for tool in tools for layer in layers.values()):
            raise TaskHarnessError("tool capability unavailable")
        if not set(layers["runtime"]).issubset(runtime_capabilities):
            raise TaskHarnessError("runtime capabilities unavailable")
        return harness.model_dump(exclude_none=True)
    except (ValidationError, ValueError, TypeError, KeyError, OverflowError) as error:
        raise TaskHarnessError("invalid protected task harness") from error


def freeze_harness(*, persona, model_binding, limits, service_policy, tool_grants=(), env=None):
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
        if (
            not isinstance(catalogue, dict)
            or not {"schemaVersion", "snapshots"}.issubset(catalogue)
            or set(catalogue) - {"schemaVersion", "snapshots", "tools"}
            or type(catalogue["schemaVersion"]) is not int
            or catalogue["schemaVersion"] != 1
        ):
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
        reviewed = [RuntimeTool.model_validate(tool) for tool in catalogue.get("tools", [])]
        if len(reviewed) > 64 or len({tool.permission for tool in reviewed}) != len(reviewed):
            raise TaskHarnessError("invalid reviewed tool catalogue")
        tools = [tool for tool in reviewed if tool.permission in tool_grants]
        if set(tool_grants) != {tool.permission for tool in tools}:
            raise TaskHarnessError("admitted tool lacks reviewed implementation schema")
        if not set(tool_grants).issubset(service_policy.get("allowed_tools", [])):
            raise TaskHarnessError("tool lacks principal authority")
        for layer in layers.values():
            layer.extend(sorted({tool.capability for tool in tools} - set(layer)))
        value = {
            **({"tools": [tool.model_dump() for tool in tools]} if tools else {}),
            "snapshot": snapshot.model_dump(),
            "policy": {
                "personaKey": definition.key,
                "personaDigest": snapshot.digest,
                "compatibilityClass": "codex-sdk",
                "harnessRevision": REVISION,
                "canonicalModel": model_binding["model_id"],
                "allowedEfforts": [definition.effort],
                "capabilityLayers": layers,
                "limits": {
                    "maxTurns": min(definition.limits.maxTurns, limits["max_turns"]),
                    "maxContextBytes": definition.limits.maxContextBytes,
                    "maxDurationMs": min(definition.limits.maxDurationMs, int(service_policy["limits"]["max_duration_minutes"]) * 60000),
                },
                "deadlineMs": deadline,
            },
        }
        # Tracing is an optional gateway extra; disabled installations still admit Tasks.
        try:
            from opentelemetry import trace
        except ImportError:
            trace = None
        if trace is not None:
            span = trace.get_current_span().get_span_context()
            if span.is_valid:
                value["traceparent"] = f"00-{span.trace_id:032x}-{span.span_id:016x}-{int(span.trace_flags) & 1:02x}"
        return validate_harness(value, persona=persona, model_binding=model_binding, limits=limits)
    except (OSError, ValidationError, ValueError, TypeError, KeyError) as error:
        raise TaskHarnessError("task harness prerequisite unavailable") from error


def assert_bootstrap_size(harness, *, immutable_input, model_binding, limits):
    # The worker adds bounded identity/control fields and up to four artifact
    # descriptors. Reserve 4 KiB for those; input content transfers separately.
    payload = {"harness": harness, "input": immutable_input, "model_binding": model_binding, "limits": limits}
    if len(rfc8785.dumps(payload)) + 4096 > 65536:
        raise TaskHarnessError("task harness bootstrap exceeds process frame bound")
