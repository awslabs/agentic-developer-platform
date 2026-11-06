"""Immutable S3 admission and final-usage evidence for durable metering."""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import boto3
import rfc8785
from botocore.exceptions import ClientError

from src.orchestration.provider_quotes import ProviderQuote, TrustedUsage

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class MeteringObjectReceipt:
    tenant_id: str
    request_id: str
    owner_id: str
    key: str
    version_id: str
    sha256: str


class ImmutableMeteringJournal:
    def __init__(self, bucket: str, *, region: str, client: Any = None) -> None:
        if not bucket:
            raise ValueError("metering journal bucket is required")
        self._bucket = bucket
        self._region = region
        self._client = client

    def _s3(self) -> Any:
        if self._client is None:
            self._client = boto3.client("s3", region_name=self._region)
        return self._client

    @staticmethod
    def _prefix(tenant_id: str, request_id: str) -> str:
        if not tenant_id or not request_id:
            raise ValueError("metering requires tenant and server request ID")
        tenant_key = hashlib.sha256(tenant_id.encode()).hexdigest()
        attempt_key = hashlib.sha256(request_id.encode()).hexdigest()
        return f"metering/v1/{tenant_key}/{attempt_key}"

    @classmethod
    def object_keys(cls, tenant_id: str, request_id: str) -> tuple[str, str]:
        prefix = cls._prefix(tenant_id, request_id)
        return f"{prefix}/admission.json", f"{prefix}/final-usage.json"

    async def _put(
        self, *, tenant_id: str, request_id: str, owner_id: str, key: str, payload: dict[str, Any], deadline: float
    ) -> MeteringObjectReceipt:
        body = rfc8785.dumps(payload)
        digest = hashlib.sha256(body).hexdigest()

        def put() -> str:
            client = self._s3()
            try:
                response = client.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType="application/json", IfNoneMatch="*")
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "PreconditionFailed":
                    raise
                response = client.get_object(Bucket=self._bucket, Key=key)
                if hashlib.sha256(response["Body"].read()).hexdigest() != digest:
                    raise ValueError("conflicting immutable metering evidence") from error
            version_id = response.get("VersionId")
            if not version_id or version_id == "null":
                raise ValueError("metering journal requires versioned objects")
            return version_id

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("metering journal deadline expired")
        version_id = await asyncio.wait_for(asyncio.to_thread(put), timeout=remaining)
        return MeteringObjectReceipt(tenant_id, request_id, owner_id, key, version_id, digest)

    async def write_admission(
        self,
        *,
        tenant_id: str,
        request_id: str,
        owner_id: str,
        quote: ProviderQuote,
        submitted_sha256: str,
        deadline: float,
    ) -> MeteringObjectReceipt:
        if not owner_id or not _DIGEST.fullmatch(submitted_sha256) or not _DIGEST.fullmatch(quote.request_sha256):
            raise ValueError("invalid metering admission binding")
        payload = {
            "schema_version": 1,
            "kind": "admission",
            "tenant_id": tenant_id,
            "request_id": request_id,
            "owner_id": owner_id,
            "provider": quote.provider,
            "endpoint": quote.endpoint,
            "capability": quote.capability,
            "billing_model_id": quote.billing_model_id,
            "request_sha256": quote.request_sha256,
            "submitted_sha256": submitted_sha256,
            "pricing_revision": quote.pricing_revision,
            "quote_issued_at": quote.issued_at.isoformat(),
            "quote_expires_at": quote.expires_at.isoformat(),
            "bound_usd": str(quote.total_usd),
            "upper_bound_usd": str(quote.maximum_cost_usd),
            "input_count_method": quote.input_count_method,
            "max_input_tokens": quote.max_input_tokens,
            "max_output_tokens": quote.max_output_tokens,
            "max_tool_tokens": quote.max_tool_tokens,
            "max_cache_write_tokens": quote.max_cache_write_tokens,
        }
        return await self._put(
            tenant_id=tenant_id,
            request_id=request_id,
            owner_id=owner_id,
            key=f"{self._prefix(tenant_id, request_id)}/admission.json",
            payload=payload,
            deadline=deadline,
        )

    @classmethod
    def unsent_key(cls, tenant_id: str, request_id: str) -> str:
        return f"{cls._prefix(tenant_id, request_id)}/proven-unsent.json"

    async def write_unsent(
        self,
        admission: MeteringObjectReceipt,
        *,
        revision: int,
        producer_epoch: int,
        request_sha256: str,
        submitted_sha256: str,
        quote_sha256: str,
        destination_sha256: str,
        deadline: float,
    ) -> MeteringObjectReceipt:
        if (
            admission.key != self.object_keys(admission.tenant_id, admission.request_id)[0]
            or not _DIGEST.fullmatch(admission.sha256)
            or type(revision) is not int
            or revision < 3
            or type(producer_epoch) is not int
            or producer_epoch < 1
            or any(not _DIGEST.fullmatch(value) for value in (request_sha256, submitted_sha256, quote_sha256, destination_sha256))
        ):
            raise ValueError("invalid proven-unsent binding")
        return await self._put(
            tenant_id=admission.tenant_id,
            request_id=admission.request_id,
            owner_id=admission.owner_id,
            key=self.unsent_key(admission.tenant_id, admission.request_id),
            payload={
                "schema_version": 1,
                "kind": "proven_unsent",
                "tenant_id": admission.tenant_id,
                "request_id": admission.request_id,
                "owner_id": admission.owner_id,
                "admission_sha256": admission.sha256,
                "admission_version_id": admission.version_id,
                "revision": revision,
                "producer_epoch": producer_epoch,
                "request_sha256": request_sha256,
                "submitted_sha256": submitted_sha256,
                "quote_sha256": quote_sha256,
                "destination_sha256": destination_sha256,
            },
            deadline=deadline,
        )

    async def discover_unsent(self, admission: MeteringObjectReceipt, *, deadline: float) -> MeteringObjectReceipt | None:
        key = self.unsent_key(admission.tenant_id, admission.request_id)

        def get() -> MeteringObjectReceipt | None:
            try:
                response = self._s3().get_object(Bucket=self._bucket, Key=key)
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                    return None
                raise
            version_id = response.get("VersionId")
            if not version_id or version_id == "null":
                raise ValueError("metering recovery requires versioned no-send evidence")
            digest = hashlib.sha256(response["Body"].read()).hexdigest()
            return MeteringObjectReceipt(admission.tenant_id, admission.request_id, admission.owner_id, key, version_id, digest)

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("metering no-send discovery deadline expired")
        return await asyncio.wait_for(asyncio.to_thread(get), timeout=remaining)

    async def read_unsent(self, receipt: MeteringObjectReceipt, *, deadline: float) -> dict[str, Any]:
        if receipt.key != self.unsent_key(receipt.tenant_id, receipt.request_id) or not _DIGEST.fullmatch(receipt.sha256):
            raise ValueError("invalid no-send object reference")

        def get() -> dict[str, Any]:
            response = self._s3().get_object(Bucket=self._bucket, Key=receipt.key, VersionId=receipt.version_id)
            if response.get("VersionId") != receipt.version_id:
                raise ValueError("metering no-send version changed")
            body = response["Body"].read()
            if hashlib.sha256(body).hexdigest() != receipt.sha256:
                raise ValueError("metering no-send checksum changed")
            payload = json.loads(body)
            if (
                not isinstance(payload, dict)
                or rfc8785.dumps(payload) != body
                or payload.get("schema_version") != 1
                or payload.get("kind") != "proven_unsent"
                or payload.get("tenant_id") != receipt.tenant_id
                or payload.get("request_id") != receipt.request_id
                or payload.get("owner_id") != receipt.owner_id
                or type(payload.get("revision")) is not int
                or type(payload.get("producer_epoch")) is not int
                or any(
                    not isinstance(payload.get(name), str) or not _DIGEST.fullmatch(payload[name])
                    for name in ("admission_sha256", "request_sha256", "submitted_sha256", "quote_sha256", "destination_sha256")
                )
                or not isinstance(payload.get("admission_version_id"), str)
            ):
                raise ValueError("metering no-send evidence changed")
            return payload

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("metering no-send read deadline expired")
        return await asyncio.wait_for(asyncio.to_thread(get), timeout=remaining)

    async def write_final(
        self,
        *,
        admission: MeteringObjectReceipt,
        usage: dict[str, Any],
        trusted: TrustedUsage,
        provider_request_id: str | None,
        deadline: float,
        pricing_decision: dict | None = None,
        completed_at: str | None = None,
        latency_ms: int | None = None,
    ) -> MeteringObjectReceipt:
        if not trusted.known or type(trusted.input_tokens) is not int or type(trusted.output_tokens) is not int:
            raise ValueError("final metering receipt requires trusted measured usage")
        if trusted.input_tokens < 0 or trusted.output_tokens < 0:
            raise ValueError("invalid measured usage")
        if any(type(usage.get(name)) is not int or usage[name] < 0 for name in ("input_tokens", "output_tokens")):
            raise ValueError("missing or invalid provider usage")
        if trusted.input_tokens != usage["input_tokens"] or trusted.output_tokens != usage["output_tokens"]:
            raise ValueError("trusted usage does not match provider evidence")
        evidence = {name: usage[name] for name in ("input_tokens", "output_tokens")}
        for name in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            if name in usage:
                if type(usage[name]) is not int or usage[name] < 0:
                    raise ValueError("invalid provider cache usage")
                evidence[name] = usage[name]
        for name, counter in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
            if name in usage:
                details = usage[name]
                if not isinstance(details, dict) or type(details.get(counter)) is not int or details[counter] < 0:
                    raise ValueError("invalid provider usage details")
                evidence[name] = {counter: details[counter]}
        payload = {
            "schema_version": 1,
            "kind": "final_usage",
            "tenant_id": admission.tenant_id,
            "request_id": admission.request_id,
            "owner_id": admission.owner_id,
            "admission_sha256": admission.sha256,
            "admission_version_id": admission.version_id,
            "provider_request_id": provider_request_id,
            "usage": evidence,
        }
        if pricing_decision is not None:
            if not completed_at or type(latency_ms) is not int or latency_ms < 0:
                raise ValueError("priced final metering requires measured completion time and latency")
            payload.update(pricing_decision=pricing_decision, completed_at=completed_at, latency_ms=latency_ms)
        return await self._put(
            tenant_id=admission.tenant_id,
            request_id=admission.request_id,
            owner_id=admission.owner_id,
            key=f"{self._prefix(admission.tenant_id, admission.request_id)}/final-usage.json",
            payload=payload,
            deadline=deadline,
        )

    async def discover_final(self, admission: MeteringObjectReceipt, *, deadline: float) -> MeteringObjectReceipt | None:
        key = self.object_keys(admission.tenant_id, admission.request_id)[1]

        def get() -> MeteringObjectReceipt | None:
            try:
                response = self._s3().get_object(Bucket=self._bucket, Key=key)
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                    return None
                raise
            version_id = response.get("VersionId")
            if not version_id or version_id == "null":
                raise ValueError("metering recovery requires versioned final evidence")
            digest = hashlib.sha256(response["Body"].read()).hexdigest()
            return MeteringObjectReceipt(admission.tenant_id, admission.request_id, admission.owner_id, key, version_id, digest)

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("metering final discovery deadline expired")
        return await asyncio.wait_for(asyncio.to_thread(get), timeout=remaining)

    async def read_admission(self, receipt: MeteringObjectReceipt, *, deadline: float) -> dict[str, Any]:
        if receipt.key != self.object_keys(receipt.tenant_id, receipt.request_id)[0] or not _DIGEST.fullmatch(receipt.sha256):
            raise ValueError("invalid admission metering object reference")

        def get() -> dict[str, Any]:
            response = self._s3().get_object(Bucket=self._bucket, Key=receipt.key, VersionId=receipt.version_id)
            if response.get("VersionId") != receipt.version_id:
                raise ValueError("metering admission version changed")
            body = response["Body"].read()
            if hashlib.sha256(body).hexdigest() != receipt.sha256:
                raise ValueError("metering admission checksum changed")
            payload = json.loads(body)
            if (
                not isinstance(payload, dict)
                or rfc8785.dumps(payload) != body
                or payload.get("schema_version") != 1
                or payload.get("kind") != "admission"
                or payload.get("tenant_id") != receipt.tenant_id
                or payload.get("request_id") != receipt.request_id
                or payload.get("owner_id") != receipt.owner_id
            ):
                raise ValueError("metering admission identity changed")
            return payload

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("metering admission read deadline expired")
        return await asyncio.wait_for(asyncio.to_thread(get), timeout=remaining)

    async def read_final(self, receipt: MeteringObjectReceipt, *, deadline: float) -> dict[str, Any]:
        """Fetch the exact version and verify immutable provider evidence before SQL."""
        if receipt.key != self.object_keys(receipt.tenant_id, receipt.request_id)[1] or not _DIGEST.fullmatch(receipt.sha256):
            raise ValueError("invalid final metering object reference")

        def get() -> dict[str, Any]:
            response = self._s3().get_object(Bucket=self._bucket, Key=receipt.key, VersionId=receipt.version_id)
            if response.get("VersionId") != receipt.version_id:
                raise ValueError("metering final version changed")
            body = response["Body"].read()
            if hashlib.sha256(body).hexdigest() != receipt.sha256:
                raise ValueError("metering final checksum changed")
            payload = json.loads(body)
            if not isinstance(payload, dict) or rfc8785.dumps(payload) != body:
                raise ValueError("metering final is not canonical evidence")
            if (
                payload.get("schema_version") != 1
                or payload.get("kind") != "final_usage"
                or payload.get("tenant_id") != receipt.tenant_id
                or payload.get("request_id") != receipt.request_id
                or payload.get("owner_id") != receipt.owner_id
            ):
                raise ValueError("metering final identity changed")
            return payload

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("metering final read deadline expired")
        return await asyncio.wait_for(asyncio.to_thread(get), timeout=remaining)
