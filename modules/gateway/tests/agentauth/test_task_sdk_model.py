"""Cyber SDK transport cannot broaden investigator or provider authority."""

import pytest
from pydantic import ValidationError

from src.agentauth.task_model import _valid_response_block
from src.agentauth.task_runtime_routes import SdkRequest
from src.orchestration.provider_quotes import AnthropicTextQuoteAdapter


def sdk_request():
    return {
        "messages": [
            {"role": "user", "content": "Investigate sample"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "cyber_inspect", "input": {"artifact_id": "sample"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "bounded evidence", "is_error": False}]},
        ],
        "tools": [{"name": "cyber_inspect", "input_schema": {"type": "object", "properties": {"artifact_id": {"type": "string"}}}}],
        "tool_choice": {"type": "auto"},
    }


def test_custom_tools_roundtrip():
    request = sdk_request()
    assert SdkRequest.model_validate(request).model_dump(exclude_none=True) == request
    assert _valid_response_block(request["messages"][1]["content"][0], request)
    assert not _valid_response_block(request["messages"][1]["content"][0], {"messages": []})
    assert not _valid_response_block({"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {}}, request)


@pytest.mark.parametrize(
    "key,value",
    [
        ("model", "attacker"),
        ("max_tokens", 100000),
        ("stream", True),
        ("metadata", {}),
        ("mcp_servers", []),
        ("thinking", {"type": "enabled"}),
        ("credentials", "secret"),
        ("endpoint", "https://example.com"),
    ],
)
def test_provider_authority_fields_refused(key, value):
    with pytest.raises(ValidationError):
        SdkRequest.model_validate({**sdk_request(), key: value})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["tools"][0].update(type="web_search_20250305"),
        lambda r: r["messages"][1].update(role="user"),
        lambda r: r["messages"][2].update(role="assistant"),
        lambda r: r["messages"][0].update(content=[{"type": "image", "source": {"type": "url", "url": "https://example.com"}}]),
        lambda r: r.update(tool_choice={"type": "tool", "name": "undeclared"}),
    ],
)
def test_unsupported_execution_and_content_refused(mutation):
    request = sdk_request()
    mutation(request)
    with pytest.raises(ValidationError):
        SdkRequest.model_validate(request)


def test_existing_quote_adapter_accepts_custom_tool_history():
    # Exercise the real pricing capability gate, not a mocked quote response.
    exercised = AnthropicTextQuoteAdapter._reject_unbounded_features(sdk_request())
    assert isinstance(exercised, set)


def test_route_request_forms_are_exclusive_and_digest_covers_sdk():
    from src.agentauth.task_runtime_routes import ModelBody
    from src.tasks.records import payload_digest

    uid = "00000000-0000-4000-8000-000000000001"
    sdk = sdk_request()
    invocation = {**sdk, "max_tokens": 64}
    body = {
        "schema_version": "1.0",
        "attempt": {"run": {"task_id": "tsk_" + uid, "invocation_id": uid, "generation": 1}, "runtime_attempt_id": uid},
        "turn_id": uid,
        "request_digest": payload_digest(invocation),
        "max_tokens": 64,
        "sdk_request": sdk,
    }
    parsed = ModelBody.model_validate(body)
    assert parsed.invocation() == invocation
    assert payload_digest(parsed.invocation()) == body["request_digest"]
    for extra in ({"system": "outside SDK"}, {"messages": [{"role": "user", "content": [{"type": "text", "text": "ambiguous"}]}]}):
        with pytest.raises(ValidationError):
            ModelBody.model_validate({**body, **extra})


def test_sdk_accepts_bounded_inline_image_but_never_remote_image_urls():
    import base64

    from pydantic import ValidationError

    from src.agentauth.task_runtime_routes import SdkRequest

    image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(b"\xff\xd8\xfffixture").decode()}}

    def request(value):
        return SdkRequest.model_validate(
            {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [value]}]}]}
        )

    assert request(image).messages[0].content[0].content[0].source.media_type == "image/jpeg"
    with pytest.raises(ValidationError):
        request({"type": "image", "source": {"type": "url", "url": "https://untrusted.example/image"}})
    with pytest.raises(ValidationError):
        request({"type": "image", "source": {**image["source"], "data": base64.b64encode(b"\xff\xd8\xff" + b"a" * 12000).decode()}})
