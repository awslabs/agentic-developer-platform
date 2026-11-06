"""Exercise the Product client against a transport that records real HTTP requests."""

import json
import unittest
from hashlib import sha256

import httpx
from l5_services.product_client import ProductClient


class ProductClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_revision_history_and_source_requests(self):
        observed = []

        def respond(request):
            observed.append(request)
            contents = {"content": b"pinned bytes", "file_manifest": b'{"files":[]}'}
            artifacts = {name: {"bytes": len(value), "sha256": sha256(value).hexdigest(), "version": "v1"}
                         for name, value in contents.items()}
            if request.url.path.endswith("/read"):
                return httpx.Response(200, json={"revision": 7, **artifacts})
            kind = request.url.path.rsplit("/", 1)[-1]
            if kind in {"content", "manifest"}:
                value = contents["file_manifest" if kind == "manifest" else kind]
                assert request.headers["Range"] == f"bytes=0-{len(value) - 1}"
                return httpx.Response(206, content=value, headers={
                    "Content-Range": f"bytes 0-{len(value) - 1}/{len(value)}",
                    "X-Artifact-Sha256": sha256(value).hexdigest(), "X-Artifact-Version": "v1",
                    "X-Artifact-Size": str(len(value))})
            return httpx.Response(200, json={"revision": 7})

        async with httpx.AsyncClient(base_url="https://product.example.test", transport=httpx.MockTransport(respond)) as http:
            client = ProductClient(http, "delegation-a")
            assert (await client.get_profile("product-a", "project-a"))["revision"] == 7
            assert (await client.get_revision("product-a", "project-a", 7))["revision"] == 7
            assert (await client.list_changes("product-a", "project-a", cursor="one.two+three"))["revision"] == 7
            assert (await client.list_bindings("product-a", "project-a"))["revision"] == 7
            assert (await client.list_sources("product-a", "project-a"))["revision"] == 7
            pin = {
                "delegation_id": "delegation-a", "delegation_revision": 3,
                "source_ref": {
                    "tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "connection-a",
                    "record_type": "repository", "record_id": "repository-a", "record_revision": "sha-a",
                },
                "exclusions": [], "max_bytes": 1000, "max_files": 10, "retain_until": "2026-10-31T00:00:00Z",
            }
            assert (await client.pin_source("product-a", "project-a", pin, idempotency_key="once"))["revision"] == 7
            read = {
                "delegation_id": "delegation-a", "delegation_revision": 3,
                "operation_id": "run-a", "attempt_id": "attempt-a", "lease_fence": 2,
            }
            assert (await client.read_source("product-a", "project-a", "snapshot-a", read))["revision"] == 7
            assert b"".join([part async for part in client.stream_source("product-a", "project-a", "snapshot-a", read)]) == b"pinned bytes"
            assert await client.read_source_manifest("product-a", "project-a", "snapshot-a", read) == b'{"files":[]}'
        self.assertEqual(len(observed), 11)
        self.assertTrue(all(req.headers.get("x-l5-delegation-id") == "delegation-a" for req in observed))
        self.assertTrue(all("authorization" not in req.headers for req in observed))
        self.assertEqual(observed[0].url.path, "/internal/v1/products/product-a/projects/project-a")
        self.assertEqual(observed[2].url.params["cursor"], "one.two+three")
        self.assertEqual(observed[5].headers["idempotency-key"], "once")
        self.assertEqual(json.loads(observed[5].content), pin)
        self.assertEqual(json.loads(observed[6].content), read)
        self.assertEqual([request.method for request in observed], ["GET"] * 5 + ["POST"] * 6)
        self.assertEqual(observed[8].url.path, "/internal/v1/products/product-a/projects/project-a/source-snapshots/snapshot-a/content")

    async def test_denials_expiry_and_redirects_never_trigger_fallback(self):
        attempts = []

        def respond(request):
            attempts.append(request)
            return httpx.Response(
                (410, 307, 403)[len(attempts) - 1], headers={"location": "https://other.example.test"}
            )

        async with httpx.AsyncClient(base_url="https://product.example.test", transport=httpx.MockTransport(respond), follow_redirects=True) as http:
            client = ProductClient(http, "delegation-a")
            with self.assertRaises(httpx.HTTPStatusError) as failure:
                await client.get_revision("product-a", "project-a", 1)
            self.assertEqual(failure.exception.response.status_code, 410)
            with self.assertRaises(httpx.HTTPStatusError):
                await client.get_profile("product-a", "project-a")
            with self.assertRaises(httpx.HTTPStatusError) as denied:
                await client.read_source("product-a", "project-a", "snapshot-a", {"delegation_id": "delegation-a"})
            self.assertEqual(denied.exception.response.status_code, 403)
            with self.assertRaises(ValueError):
                await client.read_source("product-a", "project-a", "snapshot-a", {"delegation_id": "foreign"})
        self.assertEqual(len(attempts), 3)
