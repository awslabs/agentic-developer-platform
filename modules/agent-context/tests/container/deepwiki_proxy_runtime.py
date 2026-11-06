"""Exercise upstream chat, RAG and embedder constructors without network access."""

import asyncio
import inspect
import json
import os
import socket


def deny_network(*args, **kwargs):
    raise AssertionError("Client construction attempted network access")


async def check_chat_stream(config):
    import httpx
    from openai import AsyncOpenAI, AuthenticationError
    from api.chat import ChatStreamer

    prompt = "Describe Requests architecture"
    calls = []

    def transport(request):
        calls.append(request)
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload == {**config, "stream": True, "messages": [{"role": "user", "content": prompt}]}
        chunks = []
        for delta in [{"role": "assistant"}, {"content": "Requests "}, {"content": "architecture"}, {}]:
            chunks.append({"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                           "model": config["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        chunks.append({"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                       "model": config["model"], "choices": []})
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    chat = ChatStreamer.create(provider="openai", model_config=config)
    async with AsyncOpenAI(
        api_key="fixture-not-a-secret", base_url=os.environ["OPENAI_BASE_URL"],
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)), max_retries=0,
    ) as client:
        chat.client.async_client = client
        assert "".join([text async for text in chat.respond_stream(prompt)]) == "Requests architecture"
    assert len(calls) == 1

    def rejected(request):
        return httpx.Response(401, json={"error": {"message": "fixture denied", "type": "authentication_error"}})

    async with AsyncOpenAI(
        api_key="fixture-not-a-secret", base_url=os.environ["OPENAI_BASE_URL"],
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(rejected)), max_retries=0,
    ) as client:
        chat.client.async_client = client
        try:
            _ = [text async for text in chat.respond_stream(prompt)]
        except AuthenticationError:
            pass
        else:
            raise AssertionError("Provider failure was hidden as generated text")


def main():
    socket.socket.connect = deny_network
    os.environ["OPENAI_API_KEY"] = "fixture-not-a-secret"
    os.environ.pop("OPENAI_BASE_URL", None)

    from adalflow.components.model_client.openai_client import OpenAIClient as UpstreamClient
    from api.clients import OpenAIClient
    from api.chat import ChatStreamer
    from api.config import get_embedder, get_model_config

    default = OpenAIClient()
    assert str(default.sync_client.base_url).rstrip("/") == "https://api.openai.com/v1"
    os.environ["OPENAI_BASE_URL"] = "https://proxy.example.invalid/v1"
    config = get_model_config("openai")
    chat = ChatStreamer.create(provider="openai", model_config=config["model_kwargs"])
    # RAG creates the provider's model_client directly, independently of chat.
    rag_client = config["model_client"]()
    embedder = get_embedder(embedder_type="openai")
    for client in (chat.client, rag_client, embedder.model_client):
        assert str(client.sync_client.base_url).rstrip("/") == os.environ["OPENAI_BASE_URL"]
        assert str(client.init_async_client().base_url).rstrip("/") == os.environ["OPENAI_BASE_URL"]

    explicit = "https://explicit.example.invalid/v1"
    assert str(OpenAIClient(base_url=explicit).sync_client.base_url).rstrip("/") == explicit
    # Preserve the upstream positional constructor as well as keyword callers.
    args = []
    for name, parameter in inspect.signature(UpstreamClient.__init__).parameters.items():
        if name == "self":
            continue
        if name == "base_url":
            args.append(explicit)
            break
        assert parameter.default is not inspect.Parameter.empty
        args.append(parameter.default)
    assert str(OpenAIClient(*args).sync_client.base_url).rstrip("/") == explicit
    asyncio.run(check_chat_stream(config["model_kwargs"]))
    print("DeepWiki chat, RAG, embeddings, async clients and explicit URL preservation passed")
    print("Actual upstream chat streamer consumed SDK text chunks and propagated provider failures")


if __name__ == "__main__":
    main()
