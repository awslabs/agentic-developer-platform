"""Remove LiteLLM 1.102.1's hidden POST replay before installing the image.

The router's num_retries=0 does not govern AsyncHTTPHandler.post: it catches a
RemoteProtocolError and silently sends the request again. That error can arrive
after Bedrock accepted a paid request, so the second response omits the first
attempt's exposure. Let the configured router policy own retries instead.

This is a pinned build-time correction, not a runtime mutation. A dependency
upgrade must review the new source and pass the behavioral test below.
"""

import argparse
import ast
import asyncio
import hashlib
from importlib.metadata import distribution
from pathlib import Path


SOURCE_SHA256 = "4d84f1b89afda0f558676428278dd53d5ee84c7af5a2ff4c3c64659209dc9314"


def patch(source: str) -> str:
    if hashlib.sha256(source.encode()).hexdigest() != SOURCE_SHA256:
        raise ValueError("LiteLLM HTTP source changed; review the single-attempt contract")
    tree = ast.parse(source)
    handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AsyncHTTPHandler")
    post = next(node for node in handler.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "post")
    retry = next(
        node for node in ast.walk(post)
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Tuple)
        and [getattr(item, "attr", None) for item in node.type.elts] == ["RemoteProtocolError", "ConnectError"]
    )
    lines = source.splitlines(keepends=True)
    lines[retry.lineno - 1:retry.end_lineno] = [
        "        except (httpx.RemoteProtocolError, httpx.ConnectError):\n",
        "            # A paid POST may already be accepted; only the router may retry.\n",
        "            raise\n",
    ]
    result = "".join(lines)
    compile(result, "patched-http-handler", "exec")
    return result


async def verify() -> None:
    """Exercise the installed method with a lost response and count every send."""
    import httpx
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    for error_type in (httpx.RemoteProtocolError, httpx.ConnectError):
        sends = []

        async def lost_response(request):
            sends.append(request)
            raise error_type("synthetic lost provider acknowledgement", request=request)

        transport = httpx.MockTransport(lost_response)
        client = httpx.AsyncClient(transport=transport)
        handler = AsyncHTTPHandler.__new__(AsyncHTTPHandler)
        handler.client = client
        handler.timeout = httpx.Timeout(1)
        handler.event_hooks = {}
        # A fallback must use this same fixture, never reach a real endpoint.
        handler.create_client = lambda **kwargs: httpx.AsyncClient(transport=transport)
        try:
            try:
                await handler.post("https://provider.test/model/titan/invoke", json={"inputText": "test"})
            except error_type:
                pass
            else:
                raise AssertionError("transport failure was hidden")
            if len(sends) != 1:
                raise AssertionError(f"paid POST was dispatched {len(sends)} times")
        finally:
            await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    arguments = parser.parse_args()
    package = distribution("litellm")
    if package.version != "1.102.1":
        raise ValueError("Review the single-attempt correction for this LiteLLM version")
    if not arguments.verify_only:
        path = Path(package.locate_file("litellm/llms/custom_httpx/http_handler.py"))
        path.write_text(patch(path.read_text()))
    asyncio.run(verify())
    print("LiteLLM POST transport dispatches once on a lost provider response")


if __name__ == "__main__":
    main()
