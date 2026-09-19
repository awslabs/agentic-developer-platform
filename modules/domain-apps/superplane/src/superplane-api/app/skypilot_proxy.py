"""Private authenticated transport for the pinned SkyPilot server on loopback.

Runs as a sidecar using the already pinned API image. It grants no operation or
spending authority. Only the governed controller holds its service credential.
"""

import argparse
import hmac
import os

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def token() -> str:
    value = os.environ.get("SKYPILOT_SERVICE_TOKEN", "")
    if len(value) < 32:
        raise RuntimeError("SKYPILOT_SERVICE_TOKEN must be configured")
    return value


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def forward(path: str, request: Request):
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), ("Bearer " + token()).encode()):
        raise HTTPException(401, "Service authentication required")
    if (
        "\\" in path
        or b"%" in request.scope.get("raw_path", b"")
        or any(p in {".", ".."} for p in path.split("/"))
    ):
        raise HTTPException(404, "Not found")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 2 * 1024 * 1024:
            raise HTTPException(413, "Request too large")
    client = httpx.AsyncClient(timeout=120, follow_redirects=False, trust_env=False)
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                "http://127.0.0.1:46580/" + path,
                params=request.query_params.multi_items(),
                headers={
                    "content-type": request.headers.get(
                        "content-type", "application/json"
                    )
                },
                content=bytes(body),
            ),
            stream=True,
        )
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(502, "Unexpected SkyPilot redirect")
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(503, "SkyPilot unavailable") from None

    async def chunks():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        chunks(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        try:
            response = httpx.get(
                "http://127.0.0.1:46581/api/health",
                headers={"Authorization": "Bearer " + token()},
                timeout=10,
                follow_redirects=False,
            )
            return 0 if response.status_code == 200 else 2
        except Exception:
            return 2
    token()
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=46581, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
