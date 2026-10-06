"""Bounded streaming provider transport, owned only by the trusted chat gateway."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import boto3
import rfc8785
from botocore.config import Config

from src.agentauth.chat_capability import ChatAuthorizationUnavailableError
from src.agentauth.chat_model_json import canonical_model_json
from src.agentauth.task_model import _valid_response_block
from src.budget.pricing_decisions import price_completed_usage
from src.proxy.bedrock_signing import bedrock_destination_signer
from src.proxy.pricing_capture import PricingCapture
from src.shared.config import get_settings


def _refuse():
    raise ChatAuthorizationUnavailableError("chat provider stream incomplete or invalid")


class ChatModelStream:
    def __init__(self, request):
        self.request = request
        self.content = []
        self.usage = {}
        self.stop_reason = None
        self.active = None
        self.partial_json = ""
        self.started = self.stopped = False
        self.events = 0

    def consume(self, event):
        self.events += 1
        if self.events > 65_536 or not isinstance(event, dict) or self.stopped:
            _refuse()
        kind = event.get("type")
        emitted = None
        if kind == "ping":
            return None
        if kind == "message_start":
            message = event.get("message")
            if self.started or not isinstance(message, dict) or message.get("content") != []:
                _refuse()
            usage = message.get("usage", {})
            if not isinstance(usage, dict) or type(usage.get("input_tokens")) is not int or usage["input_tokens"] < 0:
                _refuse()
            self.usage = {"input_tokens": usage["input_tokens"]}
            self.started = True
            return None
        if not self.started:
            _refuse()
        if kind == "content_block_start":
            block = event.get("content_block")
            if (
                self.active is not None
                or self.stop_reason is not None
                or type(event.get("index")) is not int
                or event["index"] != len(self.content)
                or len(self.content) >= 64
                or not isinstance(block, dict)
                or block.get("type") not in {"text", "tool_use", "thinking"}
            ):
                _refuse()
            self.active = event["index"]
            self.content.append(dict(block))
            if block["type"] == "thinking":
                self.content[-1].setdefault("signature", "")
            self.partial_json = ""
            if block["type"] == "text" and block.get("text"):
                emitted = {"type": "text_delta", "index": self.active, "text": block["text"]}
        elif kind in {"content_block_delta", "content_block_stop"}:
            if self.active is None or type(event.get("index")) is not int or event["index"] != self.active:
                _refuse()
            block = self.content[self.active]
            if kind == "content_block_stop":
                if self.partial_json:
                    if block.get("input") != {}:
                        _refuse()
                    try:
                        block["input"] = json.loads(self.partial_json)
                    except ValueError:
                        _refuse()
                    self.partial_json = ""
                if not _valid_response_block(block, self.request):
                    _refuse()
                self.active = None
            else:
                delta = event.get("delta")
                if not isinstance(delta, dict):
                    _refuse()
                fields = {
                    ("text", "text_delta"): "text",
                    ("thinking", "thinking_delta"): "thinking",
                    ("thinking", "signature_delta"): "signature",
                    ("tool_use", "input_json_delta"): "partial_json",
                }
                field = fields.get((block["type"], delta.get("type")))
                if not field or not isinstance(delta.get(field), str):
                    _refuse()
                if field == "partial_json":
                    self.partial_json += delta[field]
                else:
                    if not isinstance(block.get(field), str):
                        _refuse()
                    block[field] += delta[field]
                if field == "text":
                    emitted = {"type": "text_delta", "index": self.active, "text": delta[field]}
        elif kind == "message_delta":
            delta, usage = event.get("delta"), event.get("usage")
            if self.active is not None or not isinstance(delta, dict) or not isinstance(usage, dict):
                _refuse()
            if self.stop_reason is not None or type(usage.get("output_tokens")) is not int:
                _refuse()
            self.stop_reason = delta.get("stop_reason")
            self.usage["output_tokens"] = usage["output_tokens"]
        elif kind == "message_stop":
            if self.active is not None:
                _refuse()
            self.finish()
            self.stopped = True
        else:
            _refuse()
        try:
            if len(canonical_model_json(self.content)) + len(self.partial_json.encode("utf-8")) > 65_536:
                _refuse()
        except (rfc8785.CanonicalizationError, UnicodeError):
            _refuse()
        if emitted and not isinstance(emitted["text"], str):
            _refuse()
        return emitted

    def finish(self):
        allowed = {"end_turn", "max_tokens", "stop_sequence", "tool_use"}
        if (
            not self.started
            or not self.content
            or self.active is not None
            or self.stop_reason not in allowed
            or any(not _valid_response_block(block, self.request) for block in self.content)
            or (self.stop_reason == "tool_use") != any(block["type"] == "tool_use" for block in self.content)
            or type(self.usage.get("output_tokens")) is not int
            or not 0 <= self.usage["output_tokens"] <= self.request["max_tokens"]
        ):
            _refuse()


def _close(resource):
    with suppress(Exception):
        resource.close()


async def invoke_chat_messages(db, *, identity, binding, target, request, operation_id, on_event=None):
    credentials = None
    if not target.is_platform:
        from src.tasks.human_authority import principal_owner

        _, owner_id = principal_owner(identity.canonical_principal)
        credentials = await bedrock_destination_signer.get_credentials(db, target, user_id=owner_id)
        if any(
            not isinstance(getattr(credentials, field, None), str) or not getattr(credentials, field)
            for field in ("access_key_id", "secret_access_key", "session_token")
        ):
            _refuse()
    kwargs = {
        "region_name": target.region or get_settings().aws_region,
        "config": Config(connect_timeout=5, read_timeout=120, retries={"total_max_attempts": 1}),
    }
    if credentials:
        kwargs.update(
            aws_access_key_id=credentials.access_key_id,
            aws_secret_access_key=credentials.secret_access_key,
            aws_session_token=credentials.session_token,
        )
    client = boto3.client("bedrock-runtime", **kwargs)
    capture = PricingCapture(request_id=operation_id, original_model=binding["model_id"])
    capture.forwarded(client, binding["model_id"], stream=True)
    opening = asyncio.create_task(
        asyncio.to_thread(
            client.invoke_model_with_response_stream,
            modelId=binding["model_id"],
            contentType="application/json",
            accept="application/json",
            body=json.dumps({"anthropic_version": "bedrock-2023-05-31", **request}, separators=(",", ":")),
        )
    )
    try:
        response = await asyncio.shield(opening)
    except BaseException:

        def close_late(done):
            if not done.cancelled() and done.exception() is None:
                _close(done.result().get("body"))
            _close(client)

        opening.add_done_callback(close_late)
        raise
    stream = response.get("body")
    assembly = ChatModelStream(request)
    try:
        capture.response({}, response)
        iterator = iter(stream)
        while True:
            event = await asyncio.to_thread(next, iterator, None)
            if event is None:
                break
            chunk = event.get("chunk", {}).get("bytes") if isinstance(event, dict) else None
            if not isinstance(chunk, bytes) or not chunk or len(chunk) > 65_536:
                _refuse()
            try:
                decoded = json.loads(chunk)
            except (ValueError, UnicodeError):
                _refuse()
            emitted = assembly.consume(decoded)
            capture.chunk(chunk)
            if emitted is not None and on_event is not None:
                await on_event(emitted)
        if not assembly.stopped or not capture.provider_request_id:
            _refuse()
        decision = await price_completed_usage(
            request_id=operation_id, org_id=identity.tenant, raw_usage=capture.raw_usage, evidence=capture.routing, api_format="anthropic"
        )
        return {
            "content": assembly.content,
            "stop_reason": assembly.stop_reason,
            "usage": assembly.usage,
            "price": decision,
            "provider_request_id": capture.provider_request_id,
        }
    finally:
        _close(stream)
        _close(client)
