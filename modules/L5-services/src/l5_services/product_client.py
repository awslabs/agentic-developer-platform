"""Read Product data only through an authenticated Product API transport."""

from collections.abc import AsyncIterator, Mapping
from hashlib import sha256
from typing import Any
from urllib.parse import quote

import httpx


class ProductClient:
    def __init__(self, transport: httpx.AsyncClient, delegation_id: str):
        if not delegation_id:
            raise ValueError("delegation_id is required")
        self._transport = transport
        self._delegation_id = delegation_id

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {"X-L5-Delegation-ID": self._delegation_id, **(extra or {})}

    def _product_path(self, product_id: str, project_id: str) -> str:
        if not product_id or not project_id:
            raise ValueError("product and project IDs are required")
        return f"/internal/v1/products/{quote(product_id, safe='')}/projects/{quote(project_id, safe='')}"

    def _snapshot_path(self, product_id: str, project_id: str) -> str:
        if not product_id or not project_id:
            raise ValueError("product and project IDs are required")
        return f"/internal/v1/products/{quote(product_id, safe='')}/projects/{quote(project_id, safe='')}/source-snapshots"

    def _work_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("delegation_id") != self._delegation_id:
            raise ValueError("request delegation does not match client delegation")
        return dict(request)

    async def _json(self, method: str, path: str, *, body: dict[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> Any:
        response = await self._transport.request(method, path, json=body, headers=self._headers(headers), follow_redirects=False)
        response.raise_for_status()
        return response.json()

    async def get_profile(self, product_id: str, project_id: str) -> dict[str, Any]:
        return await self._json("GET", self._product_path(product_id, project_id))

    async def get_revision(self, product_id: str, project_id: str, revision: int) -> dict[str, Any]:
        if revision < 1:
            raise ValueError("revision must be positive")
        return await self._json("GET", f"{self._product_path(product_id, project_id)}/revisions/{revision}")

    async def list_changes(self, product_id: str, project_id: str, *, cursor: str | None = None) -> dict[str, Any]:
        path = f"{self._product_path(product_id, project_id)}/changes"
        if cursor is not None:
            path += f"?cursor={quote(cursor, safe='')}"
        return await self._json("GET", path)

    async def list_bindings(self, product_id: str, project_id: str) -> dict[str, Any]:
        return await self._json("GET", f"{self._product_path(product_id, project_id)}/bindings")

    async def list_sources(self, product_id: str, project_id: str) -> dict[str, Any]:
        return await self._json("GET", f"{self._product_path(product_id, project_id)}/sources")

    async def pin_source(
        self, product_id: str, project_id: str, request: Mapping[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        return await self._json(
            "POST", self._snapshot_path(product_id, project_id), body=self._work_request(request),
            headers={"Idempotency-Key": idempotency_key},
        )

    async def read_source(self, product_id: str, project_id: str, snapshot_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        return await self._json(
            "POST", f"{self._snapshot_path(product_id, project_id)}/{quote(snapshot_id, safe='')}/read",
            body=self._work_request(request),
        )

    async def _stream_snapshot(
        self, product_id: str, project_id: str, snapshot_id: str, request: Mapping[str, Any], kind: str
    ) -> AsyncIterator[bytes]:
        path = f"{self._snapshot_path(product_id, project_id)}/{quote(snapshot_id, safe='')}/{kind}"
        receipt = await self.read_source(product_id, project_id, snapshot_id, request)
        artifact = receipt["file_manifest" if kind == "manifest" else "content"]
        total = artifact["bytes"]
        limit = 64 * 1024 * 1024 if kind == "manifest" else 1024 * 1024 * 1024
        if type(total) is not int or not 0 <= total <= limit:
            raise ValueError("source artifact exceeds transfer limit")
        digest = sha256()
        for start in range(0, total, 1024 * 1024):
            end = min(start + 1024 * 1024, total) - 1
            async with self._transport.stream(
                "POST", path, json=self._work_request(request),
                headers=self._headers({"Range": f"bytes={start}-{end}"}), follow_redirects=False
            ) as response:
                response.raise_for_status()
                if (response.status_code != 206
                        or response.headers.get("Content-Range") != f"bytes {start}-{end}/{total}"
                        or response.headers.get("X-Artifact-Sha256") != artifact["sha256"]
                        or response.headers.get("X-Artifact-Version") != artifact["version"]
                        or response.headers.get("X-Artifact-Size") != str(total)
                        or response.headers.get("Content-Length") != str(end - start + 1)):
                    raise ValueError("source artifact range identity mismatch")
                received = 0
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    received += len(chunk)
                    if received > end - start + 1:
                        raise ValueError("source artifact range exceeds requested length")
                    digest.update(chunk)
                    yield chunk
                if received != end - start + 1:
                    raise ValueError("source artifact range is incomplete")
        if digest.hexdigest() != artifact["sha256"]:
            raise ValueError("source artifact digest mismatch")

    async def stream_source(
        self, product_id: str, project_id: str, snapshot_id: str, request: Mapping[str, Any]
    ) -> AsyncIterator[bytes]:
        async for chunk in self._stream_snapshot(product_id, project_id, snapshot_id, request, "content"):
            yield chunk

    async def read_source_manifest(
        self, product_id: str, project_id: str, snapshot_id: str, request: Mapping[str, Any]
    ) -> bytes:
        manifest = bytearray()
        async for chunk in self._stream_snapshot(product_id, project_id, snapshot_id, request, "manifest"):
            manifest.extend(chunk)
            if len(manifest) > 64 * 1024 * 1024:
                raise ValueError("source manifest exceeds 64 MiB")
        return bytes(manifest)
