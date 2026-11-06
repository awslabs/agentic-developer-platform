"""Continue one exited CLI child on its original session and retained allowance.

The pinned 3.19.0 NDJSON interface resumes with ``start_session`` plus
``conversation_id`` and requires its existing local metadata. This module never
creates metadata, selects a latest session, refreshes credentials or authorizes
a replacement provider job.
"""

from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
import re

from l5_services.adapters.transform.ndjson import TransformProtocolError, resume_checkpoint


def continuation_request(original, conversation, *, home, workspace, baseline_commit, region):
    """Validate actual vendor metadata after the original process has exited."""
    native = conversation.native_id
    checkpoint = conversation.checkpoint_id
    if (conversation.finished or conversation.buffer or conversation.continuations
            or not isinstance(native, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", native)
            or not checkpoint or conversation.minutes_used is None
            or conversation.minutes_used >= conversation.agent_minutes):
        raise TransformProtocolError("Original Transform session has no bounded continuation")
    home = Path(home)
    # ATX_USE_AWS_CONFIG is enabled by the pinned executable. Never discover or
    # import metadata from another home, an arbitrary path, or a latest session.
    path = home / ".aws/atx/custom" / native / "metadata.json"
    for parent in (path, *path.parents):
        if parent == home:
            break
        if parent.is_symlink():
            raise TransformProtocolError("Transform continuation metadata is linked")
    if not path.is_file() or path.stat().st_size > 262144:
        raise TransformProtocolError("Original Transform continuation metadata unavailable")
    raw = path.read_bytes()
    metadata = json.loads(raw)
    if not isinstance(metadata, dict):
        raise TransformProtocolError("Original Transform continuation metadata is not an object")
    used = metadata.get("accumulatedAgentMinutes")
    if (not isinstance(metadata, dict) or metadata.get("conversationId") != native
            or metadata.get("latestCheckpointId") != checkpoint
            or metadata.get("initialCommitId") != baseline_commit
            or metadata.get("codeRepositoryPath") != str(workspace)
            or metadata.get("workingDirectory", str(workspace)) != str(workspace)
            or metadata.get("region") != region
            or metadata.get("conversationEndDate")
            or metadata.get("expectedResponse") not in {"toolUse", "chatMessage"}
            or not isinstance(metadata.get("expectedToolUseIds"), list)
            or len(metadata["expectedToolUseIds"]) > 128
            or any(not isinstance(value, str) or not value or len(value) > 512
                   for value in metadata["expectedToolUseIds"])
            or metadata.get("interactiveMode") is not True
            or metadata.get("trustAllTools", False) is not False
            or metadata.get("enableResumeFromAnyCheckpoint", False) is not False
            or any(metadata.get(key) for key in ("campaignName", "repoName", "lsp"))
            or type(used) not in {float, int} or not math.isfinite(used)
            or not conversation.minutes_used <= used < conversation.agent_minutes
            or type(metadata.get("agentMinutesLimit")) not in {float, int}
            or metadata.get("agentMinutesLimit") != conversation.agent_minutes):
        raise TransformProtocolError("Transform continuation changed session, source or allowance")
    request = deepcopy(original)
    if (request.get("type") != "start_session" or request.get("mode") != "exec"
            or request.get("conversation_id") or request.get("params", {}).get("trustAllTools") is not False):
        raise TransformProtocolError("Transform continuation requires the original bounded invocation")
    request["conversation_id"] = native
    request["params"].update(
        # The vendor merges stored configuration unless this explicit value is
        # present. Never allow retained metadata to import tools or identity.
        configuration={}, resumeCheckpointId=resume_checkpoint(native, checkpoint),
        enableResumeFromAnyCheckpoint=False, trustAllTools=False,
        interactiveMode=True, doNotLearn=True, useKnowledgeItems=False,
        codeRepositoryPath=str(workspace), buildCommand="true",
        agentMinutesLimit=str(conversation.agent_minutes),
    )
    record = {"native_id": native, "checkpoint_id": checkpoint,
              "metadata_sha256": sha256(raw).hexdigest(), "minutes_used": used,
              "agent_minutes_limit": conversation.agent_minutes}
    conversation.begin_continuation(record)
    return request
