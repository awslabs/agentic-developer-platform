"""Provider help-text changes must not alter the reviewed input contract."""

import copy
import hashlib
import time

import pytest

from tool_adapters.atlassian import READ_TOOLS, discover_reads, read_tool
from tool_adapters.atlassian_observe import canonical
from tool_adapters.schema_contract import same_input_contract

REVIEWED = {
    "type": "object",
    "properties": {
        "cloudId": {"type": "string", "description": "Reviewed site help"},
        "description": {"type": "string", "maxLength": 20},
    },
    "required": ["cloudId"],
    "additionalProperties": False,
}


def test_description_changes_keep_the_reviewed_catalogue_and_execute_the_same_read():
    actual = copy.deepcopy(REVIEWED)
    actual["properties"]["cloudId"]["description"] = "New provider help text"
    pins = {alias: hashlib.sha256(canonical(REVIEWED)).hexdigest() for alias in READ_TOOLS}
    reviewed = {alias: REVIEWED for alias in READ_TOOLS}
    calls = []

    class Provider:
        def __init__(self, **kwargs):
            pass

        def send(self, method, params, token):
            if method == "initialize":
                return {"result": {"protocolVersion": "2025-03-26"}}
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                return {"result": {"tools": [{"name": name, "inputSchema": actual} for name, _ in READ_TOOLS.values()]}}
            calls.append(params)
            return {"result": {"structuredContent": {"key": "APP-1"}}}

    options = {"deadline": time.monotonic() + 20, "transport_factory": Provider}
    catalogue = discover_reads("test-token", pins, reviewed_schemas=reviewed, **options)
    assert all(tool["inputSchema"] == REVIEWED for tool in catalogue)
    result = read_tool("test-token", "get_jira_issue", {"cloudId": "site"}, pins, reviewed_schemas=reviewed, **options)
    assert result["structuredContent"]["key"] == "APP-1"
    assert calls == [{"name": "getJiraIssue", "arguments": {"cloudId": "site"}}]
    calls.clear()
    for candidate in (None, {}, {alias: actual for alias in READ_TOOLS}):
        with pytest.raises(ValueError, match="unverified provider schema"):
            read_tool("test-token", "get_jira_issue", {"cloudId": "site"}, pins, reviewed_schemas=candidate, **options)
    assert not calls
    actual["properties"]["cloudId"]["type"] = "integer"
    with pytest.raises(ValueError, match="unverified provider schema"):
        read_tool("test-token", "get_jira_issue", {"cloudId": "site"}, pins, reviewed_schemas=reviewed, **options)
    assert not calls


@pytest.mark.parametrize(
    "change",
    [
        {"required": ["cloudId", "description"]},
        {"additionalProperties": True},
        {"properties": {"cloudId": {"type": "integer"}}},
        {"properties": {**REVIEWED["properties"], "newArgument": {"type": "string"}}},
        {"properties": {**REVIEWED["properties"], "description": {"type": "string", "maxLength": 200}}},
        {"$ref": "https://schemas.example.test/new-contract"},
        {"default": {"description": "changed data"}},
        {"const": {"description": "changed data"}},
        {"enum": [{"description": "changed data"}]},
        {"x-provider-rule": {"description": "changed extension"}},
        {"description": 123},
    ],
)
def test_validation_literals_extensions_and_properties_named_description_remain_exact(change):
    assert not same_input_contract({**REVIEWED, **change}, REVIEWED)


@pytest.mark.parametrize(
    "keyword,value",
    [
        ("properties", {"description": {"type": "string", "description": "help"}}),
        ("$defs", {"value": {"type": "string", "description": "help"}}),
        ("items", {"type": "string", "description": "help"}),
        ("items", [{"type": "string", "description": "help"}]),
        ("allOf", [{"type": "object", "description": "help"}]),
        ("if", {"properties": {"kind": {"const": "issue", "description": "help"}}}),
    ],
)
def test_description_changes_at_schema_locations_are_compatible(keyword, value):
    reviewed = {"type": "object", keyword: value}

    def edit(item):
        if isinstance(item, dict):
            return {
                key: "changed help" if key == "description" and isinstance(value, str) else edit(value)
                for key, value in item.items()
            }
        return [edit(child) for child in item] if isinstance(item, list) else item

    assert same_input_contract(edit(reviewed), reviewed)


def test_nested_literal_descriptions_are_not_schema_annotations():
    for keyword in ("default", "const", "enum", "examples", "x-provider-rule"):
        before = {"type": "object", keyword: {"description": "before"}}
        after = {"type": "object", keyword: {"description": "after"}}
        assert not same_input_contract(after, before)
    assert not same_input_contract({"minimum": True}, {"minimum": 1})
