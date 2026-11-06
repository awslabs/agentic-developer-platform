"""Installed consumers resolve authority from each durable run, never a global tenant."""

import json
import os
import re
from copy import deepcopy

from sqlalchemy import and_, or_

from l5_services.models.publication import Publication
from l5_services.models.run import Run
from l5_services.publication.client import (
    ContextClient,
    DevopsConsumerClient,
    FinopsConsumerClient,
    TransformConsumerClient,
    WellArchitectedConsumerClient,
)

KINDS = {
    "security": None,
    "devops": "devops_result",
    "finops": "finops_cost_result",
    "transform": "transform_result",
    "well_architected": "well_architected_result",
}
CLIENTS = {
    "security": ContextClient,
    "devops": DevopsConsumerClient,
    "finops": FinopsConsumerClient,
    "transform": TransformConsumerClient,
    "well_architected": WellArchitectedConsumerClient,
}


def qualification_cases(cases, service_kind, destination):
    """A finite existing outbox selection is permission to test, not acceptance."""
    fields = {
        "service_kind",
        "kind",
        "destination",
        "schema_version",
        "contribution_id",
        "contribution_hash",
        "scope",
        "result_ref",
        "run_id",
    }
    if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
        raise ValueError("qualification requires 1 to 20 exact existing contributions")
    for case in cases:
        scope = case.get("scope", {}) if isinstance(case, dict) else {}
        reference = case.get("result_ref", {}) if isinstance(case, dict) else {}
        if (
            not isinstance(case, dict)
            or set(case) != fields
            or case["service_kind"] not in KINDS
            or case["kind"] != (KINDS[case["service_kind"]] or "security_result")
            or case["destination"] not in {"product-context", "engineering-insights"}
            or case["schema_version"] != "1.0"
            or any(
                not isinstance(case[key], str) or not case[key]
                for key in ("contribution_id", "run_id")
            )
            or not isinstance(case["contribution_hash"], str)
            or not re.fullmatch("[a-f0-9]{64}", case["contribution_hash"])
            or not isinstance(scope, dict)
            or any(
                not isinstance(scope.get(key), str) or not scope[key]
                for key in (
                    "tenant_id",
                    "product_id",
                    "project_id",
                    "owner_id",
                    "visibility",
                    "relationship_revision",
                )
            )
            or not isinstance(reference, dict)
            or reference.get("tenant_id") != scope["tenant_id"]
            or reference.get("project_id") != scope["project_id"]
            or reference.get("record_type") != case["kind"]
            or any(
                not isinstance(reference.get(key), str) or not reference[key]
                for key in ("source_connection_id", "record_id", "record_revision")
            )
            or not re.fullmatch("[1-9][0-9]*", reference["record_revision"])
        ):
            raise ValueError(
                "qualification case must pin complete durable result lineage"
            )
    selected = [
        case
        for case in cases
        if case["service_kind"] == service_kind and case["destination"] == destination
    ]
    if len(
        {(case["scope"]["tenant_id"], case["contribution_id"]) for case in selected}
    ) != len(selected):
        raise ValueError("qualification cases cannot repeat a contribution")
    return selected


class InstalledPublicationClient:
    """A consumer binding is installed separately from native execution qualification."""

    def __init__(
        self,
        *,
        service_kind,
        destination,
        transport,
        sessions,
        contract=None,
        cases=None,
    ):
        if service_kind not in KINDS or destination not in {
            "product-context",
            "engineering-insights",
        }:
            raise ValueError("unsupported installed publication consumer")
        expected_path = (
            "/internal/v1/context/l5-contributions"
            if service_kind == "security"
            else f"/internal/v1/{'context' if destination == 'product-context' else 'insights'}/l5-wa-contributions"
            if service_kind == "well_architected"
            else CLIENTS[service_kind]._paths[destination]
        )
        self.qualification_cases = None
        if cases is not None:
            if contract is not None or os.environ.get("L5_SERVICE_ID") != service_kind:
                raise ValueError(
                    "qualification belongs only to the installed service worker"
                )
            self.qualification_cases = deepcopy(
                qualification_cases(cases, service_kind, destination)
            )
            if not self.qualification_cases:
                raise ValueError("no exact qualification contribution selected")
            contract = {
                "kind": KINDS[service_kind] or "security_result",
                "schema_version": "1.0",
                "path": expected_path,
                "private_read_fencing": True,
            }
        expected_fields = {"kind", "schema_version", "path", "private_read_fencing"}
        if self.qualification_cases is None:
            expected_fields.add("qualification_ref")
        if (
            service_kind == "security"
            and destination != "product-context"
            or not isinstance(contract, dict)
            or contract.get("kind") != (KINDS[service_kind] or "security_result")
            or contract.get("schema_version") != "1.0"
            or contract.get("path") != expected_path
            or contract.get("private_read_fencing") is not True
            or (
                self.qualification_cases is None
                and (
                    not isinstance(contract.get("qualification_ref"), str)
                    or not contract["qualification_ref"].strip()
                )
            )
            or set(contract) != expected_fields
        ):
            raise ValueError("installed consumer lacks a qualified exact peer contract")
        self.service_kind, self.destination = service_kind, destination
        self.transport, self.sessions = transport, sessions
        self.contract = dict(contract)
        self.finops = service_kind == "finops"
        self.transform = service_kind == "transform"
        self.devops = service_kind == "devops"
        self.well_architected = service_kind == "well_architected"

    def scope_conditions(self):
        kind = Publication.payload["kind"].as_string()
        conditions = (
            Publication.destination == self.destination,
            kind.is_(None)
            if self.service_kind == "security"
            else kind == KINDS[self.service_kind],
        )
        if self.qualification_cases is not None:
            conditions += (
                or_(
                    *(
                        and_(
                            Publication.tenant_id == case["scope"]["tenant_id"],
                            Publication.publication_id == case["contribution_id"],
                            Publication.contribution_hash == case["contribution_hash"],
                        )
                        for case in self.qualification_cases
                    )
                ),
            )
        return conditions

    def can_invalidate(self, publication):
        scope = publication.payload.get("scope") or {}
        if self.qualification_cases is not None and not any(
            all(
                case[key] == value
                for key, value in {
                    "scope": scope,
                    "result_ref": publication.payload.get("result_ref"),
                    "run_id": publication.payload.get("run_id"),
                    "contribution_id": publication.publication_id,
                    "contribution_hash": publication.contribution_hash,
                    "destination": publication.destination,
                    "schema_version": publication.schema_version,
                }.items()
            )
            for case in self.qualification_cases
        ):
            return False
        return (
            publication.destination == self.destination
            and publication.schema_version == "1.0"
            and publication.payload.get("kind") == KINDS[self.service_kind]
            and publication.payload.get("result_ref", {}).get("record_type")
            == (KINDS[self.service_kind] or "security_result")
            and scope.get("tenant_id") == publication.tenant_id
            and all(
                isinstance(scope.get(field), str) and scope[field]
                for field in ("tenant_id", "product_id", "project_id")
            )
        )

    def can_publish(self, publication):
        return (
            self.can_invalidate(publication)
            and publication.invalidation_watermark == 0
            and (
                not self.well_architected
                or publication.result_version > 1
                and publication.payload.get("invalidation_watermark") == 0
            )
        )

    async def _client(self, publication):
        if not self.can_invalidate(publication):
            raise ValueError("publication belongs to another service or destination")
        async with self.sessions() as session:
            run = await session.get(
                Run, (publication.tenant_id, publication.payload["run_id"])
            )
            scope = publication.payload["scope"]
            expected_sources = (
                (run.manifest or {}).get("sources")
                if run is not None
                and self.service_kind == "devops"
                and run.input.get("capability") == "release_readiness"
                else run.input.get("sources", [])
                if run is not None
                else None
            )
            if (
                run is None
                or run.service_id != self.service_kind
                or any(
                    scope[field] != getattr(run, field)
                    for field in ("tenant_id", "product_id", "project_id")
                )
                or not isinstance(run.admission, dict)
                or run.admission.get("scope") != scope
                or not run.admission.get("delegation_id")
                or (
                    "source_refs" in publication.payload
                    and expected_sources != publication.payload["source_refs"]
                )
            ):
                raise ValueError("publication does not match durable Product work")
            delegation = run.admission["delegation_id"]
        kwargs = {
            "scope": {
                key: scope[key] for key in ("tenant_id", "product_id", "project_id")
            },
            "private_read_fencing": True,
        }
        if self.service_kind != "security":
            kwargs["destination"] = self.destination
        if self.well_architected:
            kwargs.pop("private_read_fencing")
            kwargs["peer_admission"] = {**self.contract, "scope": kwargs["scope"]}
        return CLIENTS[self.service_kind](self.transport, delegation, **kwargs)

    async def publish(self, publication):
        return await (await self._client(publication)).publish(publication)

    async def read_receipt(self, publication):
        return await (await self._client(publication)).read_receipt(publication)

    async def invalidate(self, publication):
        return await (await self._client(publication)).invalidate(publication)

    async def replay_fixture(self, publication, *, invalidation_body=None):
        """Send the exact original bytes for finite replay tests; never edit SQL state."""
        from l5_services.publication.contributions import contribution_pages

        if self.qualification_cases is None:
            raise ValueError("replay requires an exact finite qualification case")
        client = await self._client(publication)
        path = self.contract["path"]
        headers = (
            client._headers(publication)
            if self.service_kind == "security"
            else client._headers(publication, invalidation=True)
        )
        if invalidation_body is not None:
            response = await self.transport.post(
                path + "/" + publication.publication_id + "/invalidate",
                json=invalidation_body,
                headers=headers,
                follow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
        pages = (
            contribution_pages(publication)
            if self.service_kind == "security"
            else [publication.payload]
        )
        for index, payload in enumerate(pages):
            page_headers = dict(headers)
            if len(pages) > 1:
                page_headers["Idempotency-Key"] += f"/pages/{index}"
            response = await self.transport.post(
                path, json=payload, headers=page_headers, follow_redirects=False
            )
            response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("consumer replay has not admitted every page")
        return response.json()


def installed_publication_client(
    *, service_kind, destination, transport, sessions, contracts=None
):
    """Absent configuration disables only publication, leaving native workers usable."""
    if contracts is None:
        raw = os.environ.get("L5_CONSUMER_CONTRACTS")
        contracts = json.loads(raw) if raw else {}
    if not isinstance(contracts, dict):
        raise ValueError("invalid installed consumer contracts")
    selected = contracts.get(service_kind, {})
    if not isinstance(selected, dict):
        raise ValueError("invalid service consumer contracts")
    if destination not in selected:
        raw_cases = os.environ.get("L5_CONSUMER_QUALIFICATION_CASES")
        if raw_cases:
            cases = json.loads(raw_cases)
            if qualification_cases(cases, service_kind, destination):
                return InstalledPublicationClient(
                    service_kind=service_kind,
                    destination=destination,
                    transport=transport,
                    sessions=sessions,
                    cases=cases,
                )
        return None
    return InstalledPublicationClient(
        service_kind=service_kind,
        destination=destination,
        transport=transport,
        sessions=sessions,
        contract=selected[destination],
    )
