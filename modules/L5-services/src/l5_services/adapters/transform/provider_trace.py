"""Verify bounded producer traces from independently read provider checkpoints.

A trace proves only the observed operation and mediation controls. It is not a
qualification catalog and cannot establish installed kernel or Task boundaries.
"""

import json
import math
import re

from l5_services.adapters.transform.ndjson import resume_checkpoint


TOOL_CONTROLS = {"allowed_source_read", "allowed_source_write", "denied_shell",
                 "denied_source_read", "denied_source_write"}


def verify_provider_trace(value, *, native_id, agent_minutes):
    def require(condition):
        if not condition:
            raise ValueError("Transform retained provider trace differs from the original bounded session")

    require(isinstance(value, dict) and set(value) == {"schema", "frames", "continuations", "agent_minutes_limit"}
            and value["schema"] == "transform-provider-trace/v1"
            and type(value["agent_minutes_limit"]) is int and value["agent_minutes_limit"] == agent_minutes
            and isinstance(value["frames"], list) and 1 <= len(value["frames"]) <= 1024
            and isinstance(value["continuations"], list) and len(value["continuations"]) <= 1
            and len(json.dumps(value).encode()) <= 270336)
    started = completed = resumed = False
    checkpoint = pending = usage = None
    continuation = []
    controls = set()
    usage_after_resume = True
    for frame in value["frames"]:
        require(isinstance(frame, dict) and not completed)
        kind = frame.get("type")
        if kind == "session_started":
            require(set(frame) == {"type", "native_id"} and not started and frame["native_id"] == native_id)
            started = True
            continue
        if kind == "checkpoint":
            require(set(frame) == {"type", "checkpoint_id"} and pending is None
                    and isinstance(frame["checkpoint_id"], str) and 0 < len(frame["checkpoint_id"]) <= 1024)
            resume_checkpoint(native_id, frame["checkpoint_id"])
            checkpoint = frame["checkpoint_id"]
            continue
        require(started)
        if kind == "agent_minutes_update":
            observed = frame.get("used")
            require(set(frame) == {"type", "used"} and pending is None
                    and type(observed) in {int, float} and math.isfinite(observed)
                    and 0 <= observed <= agent_minutes and (usage is None or usage <= observed))
            usage = observed
            usage_after_resume = True
        elif kind == "continuation_requested":
            require(set(frame) == {"type", "native_id", "checkpoint_id", "metadata_sha256", "minutes_used", "agent_minutes_limit"}
                    and not continuation and pending is None and frame["native_id"] == native_id
                    and checkpoint is not None and frame["checkpoint_id"] == checkpoint and usage is not None
                    and isinstance(frame["metadata_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", frame["metadata_sha256"])
                    and type(frame["minutes_used"]) in {int, float}
                    and math.isfinite(frame["minutes_used"]) and usage <= frame["minutes_used"] < agent_minutes
                    and type(frame["agent_minutes_limit"]) is int and frame["agent_minutes_limit"] == agent_minutes)
            continuation.append({key: item for key, item in frame.items() if key != "type"})
            pending = checkpoint
            usage = frame["minutes_used"]
            usage_after_resume = False
        elif kind == "resume_status":
            require(set(frame) == {"type", "native_id", "checkpoint_id"} and pending is not None
                    and frame["native_id"] == native_id
                    and frame["checkpoint_id"] == resume_checkpoint(native_id, pending) and not resumed)
            resumed, pending = True, None
        elif kind == "tool_mediation":
            control = frame.get("control")
            require(set(frame) == {"type", "decision", "control", "input_sha256"} and pending is None
                    and control in TOOL_CONTROLS | {"denied_other_tool"}
                    and frame["decision"] == ("approve" if control.startswith("allowed_") else "deny")
                    and isinstance(frame["input_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", frame["input_sha256"]))
            controls.add(control)
        elif kind == "complete":
            require(set(frame) == {"type", "status"} and frame["status"] == "completed"
                    and pending is None and usage is not None and usage_after_resume)
            completed = True
        else:
            require(False)
    require(completed and value["continuations"] == continuation and (not continuation or resumed))
    return {"submit_observed": started, "read_observed": completed, "resume_observed": resumed,
            "tool_controls": sorted(controls), "missing_tool_controls": sorted(TOOL_CONTROLS - controls),
            "minutes_used": usage, "agent_minutes_limit": agent_minutes,
            "remote_cancel": "unsupported"}
