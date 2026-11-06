"""Source-only comparison and per-run delegation for installed publication."""

from l5_services.models.publication import Publication
from l5_services.models.run import Run
from l5_services.publication.client import WellArchitectedConsumerClient


def compare_source_statement(recommendation, files, goals):
    statement = recommendation.get("description")
    if isinstance(statement, str) and statement.strip():
        for archive_path, source in sorted(files.items()):
            for line_number, line in enumerate(source["lines"], start=1):
                if line == statement:
                    return {
                        "status": "supported",
                        "explanation": "Pinned source states this exact recommendation; this checks "
                                       "source agreement, not deployed state or implementation correctness.",
                        "verifier_ref": "adp:well-architected:exact-source-statement:v1",
                        "citations": [{"archive_path": archive_path, "line": line_number, "quote": line}],
                    }
    return {
        "status": "not_verifiable",
        "explanation": "No exact recommendation statement was found in pinned sources. Semantic "
                       "correctness and goal satisfaction require an independent review.",
        "verifier_ref": "adp:well-architected:exact-source-statement:v1",
        "citations": [],
    }


class PublicationClient:
    well_architected = True

    @staticmethod
    def validate_contracts(contracts):
        def valid_path(path, prefix):
            return (isinstance(path, str) and path.startswith(prefix)
                    and not any(character in path for character in ("?", "#", "%", "\\"))
                    and not any(part in {"", ".", ".."} for part in path.split("/")[1:]))

        if not isinstance(contracts, dict) or set(contracts) - {"product-context", "engineering-insights"}:
            raise ValueError("unsupported Well-Architected peer contracts")
        for destination, prefix in (("product-context", "/internal/v1/context/"),
                                    ("engineering-insights", "/internal/v1/insights/")):
            if destination not in contracts:
                continue
            contract = contracts[destination]
            reuse_field = "query_path" if destination == "product-context" else "usage_path"
            if (not isinstance(contract, dict)
                    or contract.get("kind") != "well_architected_result"
                    or contract.get("schema_version") != "1.0"
                    or contract.get("private_read_fencing") is not True
                    or not valid_path(contract.get("path"), prefix)
                    or set(contract) - {"kind", "schema_version", "private_read_fencing", "path", "task_fencing", reuse_field}
                    or ((reuse_field in contract or "task_fencing" in contract)
                        and (contract.get("task_fencing") is not True or not valid_path(contract.get(reuse_field), prefix)))):
                raise ValueError("Well-Architected peer contract is unavailable or unsupported")

    def __init__(self, sessions, transport, destination, contracts):
        self.validate_contracts(contracts)
        if destination not in contracts:
            raise ValueError("unsupported Well-Architected publication destination")
        self.sessions = sessions
        self.transport = transport
        self.destination = destination
        self.contract = contracts[destination]

    def scope_conditions(self):
        return (Publication.destination == self.destination,
                Publication.payload["kind"].as_string() == "well_architected_result")

    def can_invalidate(self, publication):
        scope = publication.payload.get("scope") or {}
        return (publication.destination == self.destination and publication.schema_version == "1.0"
                and publication.payload.get("kind") == "well_architected_result"
                and scope.get("tenant_id") == publication.tenant_id
                and all(isinstance(scope.get(field), str) and scope[field]
                        for field in ("tenant_id", "product_id", "project_id")))

    def can_publish(self, publication):
        return (self.can_invalidate(publication) and publication.result_version > 1
                and publication.payload.get("invalidation_watermark") == 0
                and publication.invalidation_watermark == 0)

    async def _client(self, publication):
        if not self.can_invalidate(publication):
            raise ValueError("publication belongs to another service or destination")
        async with self.sessions() as session:
            run = await session.get(Run, (publication.tenant_id, publication.payload["run_id"]))
            scope = publication.payload["scope"]
            if (run is None or run.service_id != "well_architected"
                    or any(scope[field] != getattr(run, field)
                           for field in ("tenant_id", "product_id", "project_id"))
                    or run.input.get("sources") != publication.payload.get("source_refs")
                    or run.admission.get("scope") != scope
                    or not run.admission.get("delegation_id")):
                raise ValueError("publication does not match durable Product work")
            delegation = run.admission["delegation_id"]
        return WellArchitectedConsumerClient(
            self.transport, delegation, scope=scope, destination=self.destination,
            peer_admission={**self.contract, "scope": scope})

    async def publish(self, publication):
        return await (await self._client(publication)).publish(publication)

    async def read_receipt(self, publication):
        return await (await self._client(publication)).read_receipt(publication)

    async def invalidate(self, publication):
        return await (await self._client(publication)).invalidate(publication)
