#!/usr/bin/env python3
"""Export closed gateway Codex contracts; --check fails on schema drift."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import BaseModel  # noqa: E402

from src.agentauth.task_harness import Harness  # noqa: E402
from src.agentauth.task_responses_contract import TaskResponsesRequest, TaskResponsesResult  # noqa: E402
from src.agentauth.task_responses_tools_contract import TaskToolsResponsesRequest, TaskToolsResponsesResult  # noqa: E402


class ResponsesContracts(BaseModel):
    request: TaskResponsesRequest
    result: TaskResponsesResult


class ToolsResponsesContracts(BaseModel):
    request: TaskToolsResponsesRequest
    result: TaskToolsResponsesResult


def documents():
    result = {}
    for name, model in [("codex-harness", Harness), ("codex-responses", ResponsesContracts), ("codex-responses-tools", ToolsResponsesContracts)]:
        schema = model.model_json_schema()
        schema.update({"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": name + ".schema.json"})
        if name == "codex-harness":
            schema["description"] = (
                "Frozen server-owned Codex Task harness. Digest, capability, model and deadline bindings "
                "are additionally validated by admission and runtime."
            )
        else:
            # Pydantic's union Field max_length also affects the array branch;
            # the model validators enforce the stricter array item counts.
            for shape, field in [
                ("TaskToolsResponsesRequest" if name == "codex-responses-tools" else "TaskResponsesRequest", "input"),
                ("ResponsesMessage", "content"),
            ]:
                for branch in schema["$defs"][shape]["properties"][field]["anyOf"]:
                    if branch.get("type") == "array":
                        branch["maxItems"] = 64
        result[name + ".schema.json"] = json.dumps(schema, indent=2) + "\n"
    return result


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[3] / "docs/task-api/contracts/v1/schemas"
    for name, content in documents().items():
        file = root / name
        if "--check" in sys.argv:
            if not file.exists() or file.read_text() != content:
                raise SystemExit(f"Task schema drift: {name}")
        else:
            file.write_text(content)
