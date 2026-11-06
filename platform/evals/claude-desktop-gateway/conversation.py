#!/usr/bin/env python3
"""Validate a two-turn Anthropic tool exchange without printing message bodies."""

import json
import re
import sys
from pathlib import Path


def events(path):
    payload = Path(path).read_text()
    if not payload.startswith("event:"):
        return json.loads(payload)
    result = {"content": [], "stop_reason": None, "usage": {}}
    blocks = {}
    stopped = False
    for frame in payload.split("\n\n"):
        lines = frame.splitlines()
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if data is None:
            continue
        item = json.loads(data)
        kind = item.get("type")
        if kind == "message_start":
            result["usage"] = item.get("message", {}).get("usage", {})
        elif kind == "content_block_start":
            index = item["index"]
            if index in blocks:
                raise ValueError("client: duplicate content block index")
            blocks[index] = item["content_block"]
            if blocks[index]["type"] == "tool_use":
                blocks[index]["input"] = ""
        elif kind == "content_block_delta":
            block = blocks.get(item["index"])
            if block is None:
                raise ValueError("client: delta without content block")
            delta = item["delta"]
            if delta["type"] == "input_json_delta" and block["type"] == "tool_use":
                block["input"] += delta["partial_json"]
            elif delta["type"] == "text_delta" and block["type"] == "text":
                block["text"] += delta["text"]
            else:
                raise ValueError("client: unexpected content delta")
        elif kind == "content_block_stop":
            if item["index"] not in blocks:
                raise ValueError("client: stop without content block")
        elif kind == "message_delta":
            result["stop_reason"] = item["delta"].get("stop_reason")
        elif kind == "message_stop":
            stopped = True
        elif kind == "error":
            raise ValueError("gateway: streamed error")
    if not stopped:
        raise ValueError("gateway: missing message_stop")
    result["content"] = [blocks[index] for index in sorted(blocks)]
    return result


def tool(path):
    message = events(path)
    tools = [block for block in message["content"] if block["type"] == "tool_use"]
    if message.get("stop_reason") != "tool_use" or len(tools) != 1:
        raise ValueError("gateway: missing tool_use stop or content block")
    block = tools[0]
    if not isinstance(block.get("id"), str) or not block["id"].startswith("toolu_"):
        raise ValueError("gateway: invalid tool id")
    if block.get("name") != "add":
        raise ValueError("Bedrock: wrong tool name")
    inputs = block["input"]
    if isinstance(inputs, str):
        try:
            inputs = json.loads(inputs)
        except json.JSONDecodeError as exc:
            raise ValueError("client: incomplete input_json_delta") from exc
    if inputs != {"left": 2, "right": 3}:
        raise ValueError("Bedrock: wrong tool arguments")
    if message.get("usage", {}).get("input_tokens", 0) < 50:
        raise ValueError("Bedrock: tool call lacks expected input-token usage")
    return {"id": block["id"], "result": inputs["left"] + inputs["right"]}


def finish(path, tool_id, result):
    message = events(path)
    if message.get("stop_reason") != "end_turn":
        raise ValueError("gateway: final turn did not end")
    if any(block["type"] == "tool_use" for block in message["content"]):
        raise ValueError("gateway: unexpected second tool invocation")
    text = " ".join(block.get("text", "") for block in message["content"] if block["type"] == "text")
    if not re.search(rf"(?<!\d){result}(?!\d)", text):
        raise ValueError("Bedrock: final answer omitted tool result")
    if not tool_id.startswith("toolu_"):
        raise ValueError("client: wrong tool id")


def request(model, stream, tool_id="", result=None):
    messages = [{"role": "user", "content": "Use add to calculate 2 plus 3."}]
    if tool_id:
        messages.extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "add", "input": {"left": 2, "right": 3}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": str(result)}]},
        ])
    return {"model": model, "max_tokens": 256, "stream": stream, "messages": messages,
            "tools": [{"name": "add", "description": "Add two integers", "input_schema": {
                "type": "object", "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
                "required": ["left", "right"]}}]}


if __name__ == "__main__":
    try:
        action = sys.argv[1]
        if action == "tool":
            print(json.dumps(tool(sys.argv[2])))
        elif action == "finish":
            finish(sys.argv[2], sys.argv[3], int(sys.argv[4]))
        elif action == "request":
            print(json.dumps(request(sys.argv[2], sys.argv[3] == "true", sys.argv[4] if len(sys.argv) > 4 else "", int(sys.argv[5]) if len(sys.argv) > 5 else None)))
        else:
            raise ValueError("client: unknown conversation action")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
