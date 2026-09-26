"""Serial tool wire parsing never broadens the report-only Task contract."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from src.agentauth.task_responses_contract import TaskResponsesRequest, TaskResponsesResult
from src.agentauth.task_responses_tools_contract import TaskToolsResponsesRequest, TaskToolsResponsesResult


@pytest.fixture
def request_body():
    return {
        "input": "Read the change",
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 64,
        "parallel_tool_calls": False,
        "tools": [
            {
                "type": "namespace",
                "name": "mcp__adp",
                "description": "Authorized ADP tools.",
                "tools": [
                    {
                        "type": "function",
                        "name": "read_change",
                        "description": "Read the bound change.",
                        "strict": False,
                        "parameters": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"number": {"type": "integer"}},
                            "required": ["number"],
                        },
                    }
                ],
            }
        ],
    }


@pytest.fixture
def call():
    return {"type": "function_call", "namespace": "mcp__adp", "name": "read_change", "call_id": "call_1", "arguments": '{"number":1}'}


def test_tool_contract_is_separate_and_retains_exact_wire_values(request_body, call):
    assert TaskToolsResponsesRequest.model_validate(request_body).model_dump(exclude_none=True) == request_body
    with pytest.raises(ValidationError):
        TaskResponsesRequest.model_validate(request_body)
    result = {"id": "response_1", "status": "completed", "output": [{**call, "id": "item_1"}], "usage": {"input_tokens": 10, "output_tokens": 10}}
    assert TaskToolsResponsesResult.model_validate(result).model_dump(exclude_none=True) == result
    with pytest.raises(ValidationError):
        TaskResponsesResult.model_validate(result)
    result["output"].append({**call, "id": "item_2", "call_id": "call_2"})
    with pytest.raises(ValidationError, match="parallel"):
        TaskToolsResponsesResult.model_validate(result)


@pytest.mark.parametrize("change", ["parallel", "namespace", "duplicate", "open_schema", "endpoint", "model", "schema_size"])
def test_unreviewed_wire_shapes_are_refused(request_body, change):
    namespace = request_body["tools"][0]
    tool = namespace["tools"][0]
    if change == "parallel":
        request_body["parallel_tool_calls"] = True
    elif change == "namespace":
        namespace["name"] = "foreign"
    elif change == "duplicate":
        namespace["tools"].append(deepcopy(tool))
    elif change == "open_schema":
        tool["parameters"]["additionalProperties"] = True
    elif change == "schema_size":
        tool["parameters"]["description"] = "x" * 16385
    else:
        request_body[change] = "caller_override"
    with pytest.raises(ValidationError):
        TaskToolsResponsesRequest.model_validate(request_body)


def test_history_pairing_is_structural_and_requires_separate_durable_verification(request_body, call):
    output = {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": [
            {"type": "input_text", "text": "Wall time: 0.1 seconds\nOutput:"},
            {"type": "input_text", "text": "result"},
        ],
    }
    request_body["input"] = [call, output]
    TaskToolsResponsesRequest.model_validate(request_body)
    # Parsing cannot establish that 'result' is a trusted receipt. Journal tests
    # exercise that independent requirement; unpaired/repeated IDs fail here.
    for history in [[call], [output], [call, call, output], [call, output, call, output]]:
        request_body["input"] = history
        with pytest.raises(ValidationError):
            TaskToolsResponsesRequest.model_validate(request_body)


@pytest.mark.parametrize("arguments", ["[]", "null", '{"n":NaN}', '{"n":Infinity}', "not-json", '{"n":"' + "x" * 32768 + '"}'])
def test_function_arguments_must_be_bounded_json_objects(call, arguments):
    call["arguments"] = arguments
    result = {"id": "response_1", "status": "completed", "output": [{**call, "id": "item_1"}], "usage": {"input_tokens": 10, "output_tokens": 10}}
    with pytest.raises(ValidationError):
        TaskToolsResponsesResult.model_validate(result)


def test_live_provider_empty_metadata_is_normalized_without_losing_usage():
    from src.agentauth.task_responses_contract import normalize_provider_result
    raw = {
        "id": "response", "status": "completed", "output": [
            {"id": "reason", "type": "reasoning", "summary": [], "encrypted_content": "opaque", "content": []},
            {"id": "message", "type": "message", "role": "assistant", "status": "completed", "phase": "final_answer",
             "content": [{"type": "output_text", "text": "Done", "annotations": [], "logprobs": []}]},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                  "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 30}},
    }
    normalized = normalize_provider_result(raw)
    result = TaskToolsResponsesResult.model_validate(normalized)
    assert result.usage.input_tokens_details.cache_write_tokens == 30
    assert "content" not in normalized["output"][0]
    assert "logprobs" not in normalized["output"][1]["content"][0]
    assert raw["output"][0]["content"] == [] and raw["usage"]["total_tokens"] == 120
    for mutation in ("reasoning", "logprobs", "total", "cache"):
        altered = deepcopy(raw)
        if mutation == "reasoning":
            altered["output"][0]["content"] = [{"text": "private"}]
        elif mutation == "logprobs":
            altered["output"][1]["content"][0]["logprobs"] = [{"value": 1}]
        elif mutation == "total":
            altered["usage"]["total_tokens"] = 121
        else:
            altered["usage"]["input_tokens_details"]["cache_write_tokens"] = 90
        with pytest.raises(ValueError):
            TaskToolsResponsesResult.model_validate(normalize_provider_result(altered))
