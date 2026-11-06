"""CAS state machine; production durability belongs to Context PostgreSQL/Insights S3.

Every operation calls fresh authority before durable effects and again before
release. A signed request alone never grants access to an old copied result.
"""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from .contract import MAX_BYTES, REVISION, canonical, digest, validate_payload


class Unavailable(Exception):
    """Non-enumerating refusal, including a revoked or incomplete contribution."""


class Conflict(Exception):
    """An existing semantic contribution cannot be replaced by different content."""


class Contended(Exception):
    """Bounded CAS retries exhausted; caller can retry without native execution."""


class Consumer:
    def __init__(self, store, authorize):
        self.store, self.authorize = store, authorize

    async def _check(self, authority):
        if await self.authorize(authority) is not True:
            raise Unavailable()

    @staticmethod
    def _owned(record, authority):
        return record is not None and all(record.get(key) == authority[key] for key in (
            "scope", "result_ref", "destination", "contribution_id", "contribution_hash", "run_id", "kind"))

    @staticmethod
    def _base(authority):
        return {**{key: deepcopy(authority[key]) for key in (
            "scope", "result_ref", "destination", "contribution_id", "contribution_hash", "run_id", "kind")},
            "watermark": 0, "payload": None, "pages": {}, "receipt": None}

    @staticmethod
    def _receipt(record):
        return {"receipt_id": str(uuid5(NAMESPACE_URL, "adp:l5:admit:" + digest({
                    key: record[key] for key in ("scope", "result_ref", "destination", "contribution_hash")}))),
                "contribution_id": record["contribution_id"], "admitted_schema": "1.0",
                "consumer_revision": REVISION, "record_refs": [record["result_ref"]],
                "contribution_hash": record["contribution_hash"],
                "claim_count": len(record["payload"].get("claims", []))}

    @staticmethod
    def _assemble(record, payload):
        page = payload.get("claim_page")
        if page is None:
            return deepcopy(payload)
        if (record["kind"] != "security_result" or not isinstance(page, dict)
                or set(page) != {"index", "count", "total_claims", "contribution_hash"}
                or any(type(page.get(key)) is not int for key in ("index", "count", "total_claims"))
                or not 0 <= page["index"] < page["count"] <= 10000
                or not 0 <= page["total_claims"] <= 1000000
                or page["contribution_hash"] != record["contribution_hash"]
                or not isinstance(payload.get("claims"), list)
                or not isinstance(payload.get("evidence"), list)
                or len(payload["claims"]) > 100
                or (len(payload["evidence"]) > 100 and len(payload["claims"]) != 1)):
            raise ValueError("invalid claim page")
        key = str(page["index"])
        if key in record["pages"] and record["pages"][key] != payload:
            raise Conflict()
        record["pages"][key] = deepcopy(payload)
        header = {key: value for key, value in payload.items() if key not in {"claim_page", "claims", "evidence"}}
        if any({key: value for key, value in other.items() if key not in {"claim_page", "claims", "evidence"}} != header
               or any(other["claim_page"][field] != page[field] for field in ("count", "total_claims", "contribution_hash"))
               for other in record["pages"].values()):
            raise Conflict()
        if len(record["pages"]) != page["count"]:
            return None
        ordered = [record["pages"][str(index)] for index in range(page["count"])]
        full = {**header, "claims": [claim for item in ordered for claim in item["claims"]],
                "evidence": [reference for item in ordered for reference in item["evidence"]]}
        if len(full["claims"]) != page["total_claims"] or digest(full) != record["contribution_hash"]:
            raise Conflict()
        return full

    async def admit(self, payload, authority):
        validate_payload(payload, authority)
        for _ in range(12):
            await self._check(authority)
            record, revision = await self.store.read(authority["storage_key"])
            if record is not None and not self._owned(record, authority):
                raise Conflict()
            record = deepcopy(record) if record is not None else self._base(authority)
            if record["watermark"]:
                raise Unavailable()
            if record["receipt"]:
                await self._check(authority)
                return record["receipt"]
            record["payload"] = self._assemble(record, payload)
            if record["payload"] is not None:
                record["receipt"] = self._receipt(record)
                record["pages"] = {}
            if len(canonical(record)) > MAX_BYTES * 8:
                raise ValueError("contribution exceeds durable storage bound")
            await self._check(authority)
            if await self.store.compare_and_set(authority["storage_key"], revision, record):
                await self._check(authority)
                return record["receipt"]
        raise Contended()

    async def read(self, authority, *, projection=False):
        await self._check(authority)
        record, _ = await self.store.read(authority["storage_key"])
        if not self._owned(record, authority) or record["watermark"] or record["receipt"] is None:
            raise Unavailable()
        result = ({**deepcopy(record["payload"]), "contribution_hash": record["contribution_hash"]}
                  if projection else deepcopy(record["receipt"]))
        await self._check(authority)
        return result

    async def invalidate(self, payload, authority):
        watermark = payload.get("invalidation_watermark")
        if type(watermark) is not int or watermark <= 0 or watermark != authority.get("watermark"):
            raise ValueError("invalid invalidation watermark")
        for _ in range(12):
            await self._check(authority)
            record, revision = await self.store.read(authority["storage_key"])
            if record is not None and not self._owned(record, authority):
                raise Unavailable()
            record = deepcopy(record) if record is not None else self._base(authority)
            applied = max(record["watermark"], watermark)
            receipt = {"receipt_id": str(uuid5(NAMESPACE_URL,
                       f'adp:l5:invalidate:{authority["storage_key"]}:{applied}')),
                       "contribution_id": record["contribution_id"], "applied_watermark": applied,
                       "record_refs": [record["result_ref"]], "consumer_revision": REVISION}
            record.update(watermark=applied, invalidation_receipt=receipt)
            await self._check(authority)
            if await self.store.compare_and_set(authority["storage_key"], revision, record):
                await self._check(authority)
                return receipt
        raise Contended()

    async def use(self, payload, authority, *, read_only=False):
        # Task/actor values are asserted by Gateway after current canonical Task
        # ownership/policy checks; not inferred from arbitrary body strings.
        expected = {key: authority[key] for key in ("scope", "result_ref", "contribution_id", "task_id", "actor_id")}
        if (not isinstance(payload, dict) or any(payload.get(key) != value for key, value in expected.items())
                or authority["task_id"] in {authority["run_id"], authority.get("source_task_id")}
                or not isinstance(payload.get("citation"), (str, dict)) or not payload["citation"]
                or payload != authority.get("usage")):
            raise Unavailable()
        key = authority["storage_key"] + "/tasks/" + digest({key: expected[key] for key in ("task_id", "actor_id")})
        for _ in range(12):
            await self.read(authority)
            record, revision = await self.store.read(key)
            if record is not None:
                if any(record.get(field) != value for field, value in payload.items()):
                    raise Conflict()
                await self.read(authority)
                return record
            if read_only:
                raise Unavailable()
            receipt = {**deepcopy(payload), "receipt_id": str(uuid5(NAMESPACE_URL, "adp:l5:usage:" + key))}
            await self._check(authority)
            if await self.store.compare_and_set(key, revision, receipt):
                await self.read(authority)
                return receipt
        raise Contended()
