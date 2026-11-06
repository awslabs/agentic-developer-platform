"""Use the proposed peer Context receipt API without assuming delivery on HTTP success."""

from hashlib import sha256
from urllib.parse import quote

from l5_services.publication.contributions import contribution_pages, validate_receipt
from l5_services.publication.invalidation import validate_invalidation_receipt


class ContextClient:
    def __init__(self, transport, delegation_id, *, private_read_fencing=False, scope=None):
        if not delegation_id:
            raise ValueError("Context publication requires a delegation")
        if scope is not None and any(not isinstance(scope.get(field), str) or not scope[field]
                                     for field in ("tenant_id", "product_id", "project_id")):
            raise ValueError("Context delegation requires a complete scope")
        self.transport = transport
        self.delegation_id = delegation_id
        self.private_read_fencing = private_read_fencing
        self.scope = dict(scope) if scope is not None else None

    def can_publish(self, publication):
        if publication.destination != "product-context" or publication.payload.get("kind") is not None:
            return False
        return self.private_read_fencing or (publication.payload["classification"] == "public"
                                             and not publication.payload["source_refs"])

    def _headers(self, publication):
        if self.scope is not None and any(publication.payload["scope"][field] != self.scope[field]
                                          for field in ("tenant_id", "product_id", "project_id")):
            raise ValueError("Context delegation does not cover this publication")
        return {"X-L5-Delegation-ID": self.delegation_id,
                "Idempotency-Key": f"{publication.tenant_id}/{publication.result_id}/"
                                   f"{publication.result_version}/{publication.destination}/"
                                   f"{publication.schema_version}"}

    async def publish(self, publication):
        if not self.can_publish(publication):
            raise ValueError("private Context read fencing is not qualified")
        if publication.state not in {"pending", "publishing", "retry_wait"}:
            raise ValueError("publication is not eligible for delivery")
        pages = contribution_pages(publication)
        for index, payload in enumerate(pages):
            headers = self._headers(publication)
            if len(pages) > 1:
                headers["Idempotency-Key"] += f"/pages/{index}"
            response = await self.transport.post("/internal/v1/context/l5-contributions",
                                                 json=payload, headers=headers, follow_redirects=False)
            response.raise_for_status()
            if response.status_code != 202:
                return validate_receipt(publication, response.json())
        raise ValueError("Context has not admitted every claim page")

    async def read_receipt(self, publication):
        if not self.can_publish(publication):
            raise ValueError("private Context read fencing is not qualified")
        response = await self.transport.get(
            f"/internal/v1/context/l5-contributions/{quote(publication.publication_id, safe='')}",
            headers=self._headers(publication), follow_redirects=False)
        response.raise_for_status()
        return validate_receipt(publication, response.json())

    async def invalidate(self, publication):
        if publication.state != "invalidation_pending" or publication.invalidation is None:
            raise ValueError("no scoped Context invalidation is owed")
        response = await self.transport.post(
            f"/internal/v1/context/l5-contributions/{quote(publication.publication_id, safe='')}/invalidate",
            json={**publication.invalidation,
                  "invalidation_watermark": publication.invalidation_watermark},
            headers=self._headers(publication), follow_redirects=False)
        response.raise_for_status()
        return validate_invalidation_receipt(publication, response.json())


class FinopsConsumerClient:
    """A separately qualified, scoped peer for cost-only contribution delivery."""

    finops = True
    _paths = {"product-context": "/internal/v1/context/l5-cost-contributions",
              "engineering-insights": "/internal/v1/insights/l5-cost-contributions"}

    def __init__(self, transport, delegation_id, *, scope, destination, private_read_fencing=False):
        if (destination not in self._paths or not delegation_id
                or not isinstance(scope, dict)
                or any(not isinstance(scope.get(field), str) or not scope[field]
                       for field in ("tenant_id", "product_id", "project_id"))):
            raise ValueError("FinOps consumer requires a verified destination and scope")
        self.transport = transport
        self.delegation_id = delegation_id
        self.scope = dict(scope)
        self.destination = destination
        self.private_read_fencing = private_read_fencing

    def can_publish(self, publication):
        return (self.private_read_fencing and publication.destination == self.destination
                and publication.payload.get("kind") == "finops_cost_result"
                and all(publication.payload["scope"].get(field) == self.scope[field]
                        for field in ("tenant_id", "product_id", "project_id")))

    def can_invalidate(self, publication):
        return self.can_publish(publication)

    def _headers(self, publication, *, invalidation=False):
        qualified = self.can_invalidate(publication) if invalidation else self.can_publish(publication)
        if not qualified:
            raise ValueError("FinOps consumer read fencing or delegation is not qualified")
        return {"X-L5-Delegation-ID": self.delegation_id,
                "Idempotency-Key": f"{publication.tenant_id}/{publication.result_id}/"
                                   f"{publication.result_version}/{publication.destination}/"
                                   f"{publication.schema_version}"}

    async def _read_projection(self, publication, headers):
        response = await self.transport.get(
            f"{self._paths[self.destination]}/{quote(publication.publication_id, safe='')}/projection",
            headers=headers, follow_redirects=False)
        if response.status_code in {404, 202}:
            raise ValueError("FinOps consumer projection is not yet readable")
        response.raise_for_status()
        projection = response.json()
        if (not isinstance(projection, dict)
                or projection.get("contribution_id") != publication.publication_id
                or projection.get("contribution_hash") != publication.contribution_hash
                or projection.get("scope") != publication.payload["scope"]
                or projection.get("result_ref") != publication.payload["result_ref"]
                or projection.get("cost") != publication.payload["cost"]
                or projection.get("evidence_refs") != publication.payload["evidence_refs"]):
            raise ValueError("FinOps consumer projection differs from the immutable result")

    async def read_receipt(self, publication):
        headers = self._headers(publication)
        response = await self.transport.get(
            f"{self._paths[self.destination]}/{quote(publication.publication_id, safe='')}",
            headers=headers, follow_redirects=False)
        response.raise_for_status()
        receipt = validate_receipt(publication, response.json())
        await self._read_projection(publication, headers)
        return receipt

    async def publish(self, publication):
        if publication.state not in {"pending", "publishing", "retry_wait"}:
            raise ValueError("publication is not eligible for delivery")
        response = await self.transport.post(
            self._paths[self.destination], json=publication.payload,
            headers=self._headers(publication), follow_redirects=False)
        response.raise_for_status()
        if response.status_code == 202:
            raise ValueError("FinOps contribution admission is pending")
        validate_receipt(publication, response.json())
        return await self.read_receipt(publication)

    async def invalidate(self, publication):
        if publication.state != "invalidation_pending" or publication.invalidation is None:
            raise ValueError("no scoped FinOps invalidation is owed")
        response = await self.transport.post(
            f"{self._paths[self.destination]}/{quote(publication.publication_id, safe='')}/invalidate",
            json={**publication.invalidation,
                  "invalidation_watermark": publication.invalidation_watermark},
            headers=self._headers(publication, invalidation=True), follow_redirects=False)
        response.raise_for_status()
        return validate_invalidation_receipt(publication, response.json())


class TransformConsumerClient(FinopsConsumerClient):
    """Transform-only delivery, enabled only with a qualified scoped peer binding."""

    finops = False
    transform = True
    _paths = {"product-context": "/internal/v1/context/l5-transform-contributions",
              "engineering-insights": "/internal/v1/insights/l5-transform-contributions"}

    def can_publish(self, publication):
        return (self.private_read_fencing and publication.destination == self.destination
                and publication.payload.get("kind") == "transform_result"
                and all(publication.payload["scope"].get(field) == self.scope[field]
                        for field in ("tenant_id", "product_id", "project_id")))

    async def _read_projection(self, publication, headers):
        response = await self.transport.get(
            f"{self._paths[self.destination]}/{quote(publication.publication_id, safe='')}/projection",
            headers=headers, follow_redirects=False)
        if response.status_code in {404, 202}:
            raise ValueError("Transform consumer projection is not yet readable")
        response.raise_for_status()
        projection = response.json()
        if (not isinstance(projection, dict)
                or projection.get("contribution_id") != publication.publication_id
                or projection.get("contribution_hash") != publication.contribution_hash
                or any(projection.get(field) != publication.payload[field] for field in (
                    "scope", "result_ref", "result", "goal", "source_refs", "evidence_refs"))):
            raise ValueError("Transform consumer projection differs from the immutable result")


class DevopsConsumerClient(FinopsConsumerClient):
    """Frozen DevOps findings, with the same durable receipt/projection boundary."""

    finops = False
    devops = True
    _paths = {"product-context": "/internal/v1/context/l5-devops-contributions",
              "engineering-insights": "/internal/v1/insights/l5-devops-contributions"}

    def can_publish(self, publication):
        return (self.private_read_fencing and publication.destination == self.destination
                and publication.payload.get("kind") == "devops_result"
                and all(publication.payload["scope"].get(field) == self.scope[field]
                        for field in ("tenant_id", "product_id", "project_id")))

    async def _read_projection(self, publication, headers):
        response = await self.transport.get(
            f"{self._paths[self.destination]}/{quote(publication.publication_id, safe='')}/projection",
            headers=headers, follow_redirects=False)
        response.raise_for_status()
        projection = response.json()
        if (response.status_code != 200 or not isinstance(projection, dict)
                or projection.get("contribution_hash") != publication.contribution_hash
                or any(projection.get(field) != publication.payload[field] for field in (
                    "contribution_id", "scope", "result_ref", "result", "source_refs", "evidence_refs"))):
            raise ValueError("DevOps consumer projection differs from the immutable result")


class WellArchitectedConsumerClient(FinopsConsumerClient):
    """Only a peer-admitted WA schema may publish private architecture results."""

    finops = False
    well_architected = True

    def __init__(self, transport, delegation_id, *, scope, destination, peer_admission):
        prefix = {"product-context": "/internal/v1/context/",
                  "engineering-insights": "/internal/v1/insights/"}.get(destination)
        if (prefix is None or not isinstance(peer_admission, dict)
                or peer_admission.get("kind") != "well_architected_result"
                or peer_admission.get("schema_version") != "1.0"
                or peer_admission.get("private_read_fencing") is not True
                or not isinstance(peer_admission.get("path"), str)
                or not peer_admission["path"].startswith(prefix)
                or peer_admission["path"].endswith("/")
                or peer_admission.get("scope") != scope):
            raise ValueError("WA publication requires an admitted scoped peer contract")
        self._paths = {destination: peer_admission["path"]}
        reuse_field = ("query_path" if destination == "product-context" else "usage_path")
        reuse_path = peer_admission.get(reuse_field)
        self.reuse_path = (reuse_path if peer_admission.get("task_fencing") is True
                           and isinstance(reuse_path, str)
                           and reuse_path.startswith(prefix) and not reuse_path.endswith("/")
                           and "?" not in reuse_path and "#" not in reuse_path else None)
        super().__init__(transport, delegation_id, scope=scope, destination=destination,
                         private_read_fencing=True)

    def can_publish(self, publication):
        return (self.can_invalidate(publication)
                and publication.result_version > 1
                and publication.payload.get("invalidation_watermark") == 0
                and publication.invalidation_watermark == 0)

    def can_invalidate(self, publication):
        return (publication.destination == self.destination
                and publication.schema_version == "1.0"
                and publication.payload.get("kind") == "well_architected_result"
                and all(publication.payload["scope"].get(field) == self.scope[field]
                        for field in ("tenant_id", "product_id", "project_id")))

    async def _read_projection(self, publication, headers):
        response = await self.transport.get(
            f"{self._paths[self.destination]}/{quote(publication.publication_id, safe='')}/projection",
            headers=headers, follow_redirects=False)
        if response.status_code in {404, 202}:
            raise ValueError("WA consumer projection is not yet readable")
        response.raise_for_status()
        projection = response.json()
        if (not isinstance(projection, dict)
                or projection.get("contribution_id") != publication.publication_id
                or projection.get("contribution_hash") != publication.contribution_hash
                or any(projection.get(field) != publication.payload[field] for field in (
                    "scope", "result_ref", "result", "source_refs", "evidence_refs"))):
            raise ValueError("WA consumer projection differs from the immutable result")

    async def reuse_receipt(self, publication, *, task_id, actor_id, citation,
                            retrieval_receipt_id=None):
        if (self.reuse_path is None or not self.can_publish(publication)
                or publication.state != "published" or publication.receipt is None):
            raise ValueError("WA consumer reuse contract is not admitted")
        payload = {"scope": publication.payload["scope"],
                   "result_ref": publication.payload["result_ref"],
                   "contribution_id": publication.publication_id,
                   "task_id": task_id, "actor_id": actor_id, "citation": citation}
        if retrieval_receipt_id is not None:
            payload["retrieval_receipt_id"] = retrieval_receipt_id
        headers = self._headers(publication)
        headers["Idempotency-Key"] += "/tasks/" + sha256(f"{task_id}/{actor_id}".encode()).hexdigest()
        path = f"{self.reuse_path}/{quote(task_id, safe='')}"
        response = await self.transport.get(path, headers=headers, follow_redirects=False)
        if response.status_code == 404:
            response = await self.transport.post(self.reuse_path, json=payload,
                                                 headers=headers, follow_redirects=False)
            response.raise_for_status()
            response = await self.transport.get(path, headers=headers, follow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("WA consumer reuse receipt is not durable")
        receipt = response.json()
        if (not isinstance(receipt, dict)
                or not isinstance(receipt.get("receipt_id"), str) or not receipt["receipt_id"]
                or any(receipt.get(field) != value for field, value in payload.items())):
            raise ValueError("WA consumer reuse receipt does not match scoped result and task")
        return receipt
