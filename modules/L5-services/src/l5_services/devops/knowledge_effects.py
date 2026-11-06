"""Append-only effect history fenced by the current shared binding and work."""

from datetime import UTC, datetime

from sqlalchemy import select

from l5_services.devops.knowledge_admission import digest
from l5_services.devops.knowledge_sync import source_key
from l5_services.devops.knowledge_work import current_context
from l5_services.models.knowledge_effect import KnowledgeEffect, KnowledgeOwnership
from l5_services.workers.execution import AuthorityRevoked


class KnowledgeEffects:
    def __init__(self, sessions, clock=None):
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))

    async def _read(self, session, intent, claim, source):
        context = await current_context(
            session, intent, claim, now=self.clock(), lock=True
        )
        references = [item["source_ref"] for item in context.admission["publications"]]
        if source not in references:
            raise AuthorityRevoked("knowledge_source_outside_admission")
        key = source_key(source)
        space_id = context.binding.native_resource_ref["record_id"]
        ownership = await session.get(
            KnowledgeOwnership,
            (intent.tenant_id, space_id, key),
            with_for_update=True,
            populate_existing=True,
        )
        if ownership is not None and (
            ownership.binding_id != context.binding.binding_id
            or ownership.operation_id != intent.operation_id
            and ownership.state not in {"ready", "cancelled"}
        ):
            raise AuthorityRevoked("knowledge_owned_asset_busy")
        events = (
            await session.scalars(
                select(KnowledgeEffect)
                .where(
                    KnowledgeEffect.tenant_id == intent.tenant_id,
                    KnowledgeEffect.operation_id == intent.operation_id,
                    KnowledgeEffect.source_key == key,
                )
                .order_by(KnowledgeEffect.sequence)
            )
        ).all()
        previous = None
        for sequence, event in enumerate(events, 1):
            if (
                event.sequence != sequence
                or event.previous_hash != previous
                or event.event_hash != digest(self._body(event))
            ):
                raise AuthorityRevoked("knowledge_effect_history_changed")
            previous = event.event_hash
        if events and (
            ownership is None or ownership.operation_id != intent.operation_id
        ):
            raise AuthorityRevoked("knowledge_effect_ownership_changed")
        return context, ownership, events, key, space_id

    @staticmethod
    def _body(event):
        body = {
            field: getattr(event, field)
            for field in (
                "tenant_id",
                "operation_id",
                "source_key",
                "sequence",
                "phase",
                "work_id",
                "lease_owner",
                "fence",
                "payload",
                "previous_hash",
            )
        }
        body["created_at"] = event.created_at.replace(
            tzinfo=event.created_at.tzinfo or UTC
        ).isoformat()
        return body

    def _append(self, session, intent, claim, source, events, phase, payload):
        event = KnowledgeEffect(
            tenant_id=intent.tenant_id,
            operation_id=intent.operation_id,
            source_key=source,
            sequence=len(events) + 1,
            phase=phase,
            work_id=claim.work_id,
            lease_owner=claim.owner,
            fence=claim.fence,
            created_at=self.clock(),
            payload=payload,
            previous_hash=events[-1].event_hash if events else None,
            event_hash="",
        )
        event.event_hash = digest(self._body(event))
        session.add(event)

    async def inspect(self, intent, claim, source):
        async with self.sessions() as session:
            _, ownership, events, _, _ = await self._read(
                session, intent, claim, source
            )
            return {
                "ownership": ownership.receipt if ownership else None,
                "plan": events[0].payload if events else None,
                "phase": events[-1].phase if events else None,
                "receipt": events[-1].payload
                if events and events[-1].phase == "ready"
                else None,
            }

    async def plan(self, intent, claim, source, plan):
        async with self.sessions() as session:
            context, ownership, events, key, space_id = await self._read(
                session, intent, claim, source
            )
            if events:
                if events[0].payload != plan:
                    raise AuthorityRevoked("knowledge_native_plan_changed")
                return
            snapshot = next(
                row
                for row in context.admission["source_snapshots"]
                if row["source_ref"] == source
            )
            if (
                plan.get("space_id") != space_id
                or plan.get("source_key") != key
                or plan.get("source_ref") != source
                or plan.get("source_hash") != snapshot["content"]["sha256"]
                or plan.get("asset_id")
                != ((ownership.receipt or {}).get("asset_id") if ownership else None)
            ):
                raise AuthorityRevoked("knowledge_native_plan_changed")
            if ownership is None:
                ownership = KnowledgeOwnership(
                    tenant_id=intent.tenant_id,
                    space_id=space_id,
                    source_key=key,
                    binding_id=context.binding.binding_id,
                    operation_id=intent.operation_id,
                    state="planned",
                )
                session.add(ownership)
            ownership.operation_id, ownership.state = intent.operation_id, "planned"
            self._append(session, intent, claim, key, events, "planned", plan)
            await session.commit()

    async def start(self, intent, claim, source):
        async with self.sessions() as session:
            _, ownership, events, key, _ = await self._read(
                session, intent, claim, source
            )
            if not events:
                raise AuthorityRevoked("knowledge_native_plan_missing")
            if events[-1].phase != "planned":
                return False
            self._append(
                session,
                intent,
                claim,
                key,
                events,
                "started",
                {"plan_hash": events[0].event_hash},
            )
            ownership.state = "started"
            await session.commit()
            return True

    async def finish(self, intent, claim, source, receipt):
        async with self.sessions() as session:
            _, ownership, events, key, _ = await self._read(
                session, intent, claim, source
            )
            if not events or events[-1].phase not in {
                "started",
                "outcome_unknown",
                "ready",
            }:
                raise AuthorityRevoked("knowledge_native_effect_missing")
            plan = events[0].payload
            if (
                receipt.get("source_ref") != source
                or receipt.get("source_hash") != plan["source_hash"]
                or receipt.get("metadata") != plan["metadata"]
                or receipt.get("client_token") != plan["client_token"]
                or not receipt.get("asset_id")
                or type(receipt.get("version")) is not int
                or receipt["version"] < 1
                or receipt.get("content_written") is not True
                or receipt.get("native_use_qualified") is not False
                or plan["asset_id"] is not None
                and receipt["asset_id"] != plan["asset_id"]
            ):
                raise AuthorityRevoked("knowledge_native_receipt_changed")
            if events[-1].phase == "ready":
                if events[-1].payload != receipt:
                    raise AuthorityRevoked("knowledge_native_receipt_changed")
                return
            self._append(session, intent, claim, key, events, "ready", receipt)
            ownership.state, ownership.receipt = "ready", receipt
            await session.commit()

    async def unknown(self, intent, claim, source):
        async with self.sessions() as session:
            context, ownership, events, key, _ = await self._read(
                session, intent, claim, source
            )
            if not events or events[-1].phase not in {"started", "outcome_unknown"}:
                return
            if events[-1].phase == "started":
                self._append(
                    session,
                    intent,
                    claim,
                    key,
                    events,
                    "outcome_unknown",
                    {
                        "code": "knowledge_native_outcome_unknown",
                        "plan_hash": events[0].event_hash,
                    },
                )
            ownership.state = "outcome_unknown"
            context.operation.state = "outcome_unknown"
            context.operation.gaps = ["knowledge_native_outcome_unknown"]
            context.operation.revision += 1
            await session.commit()

    async def complete(self, intent, claim):
        async with self.sessions() as session:
            context = await current_context(
                session, intent, claim, now=self.clock(), lock=True
            )
            receipts = []
            for publication in context.admission["publications"]:
                _, _, events, _, _ = await self._read(
                    session, intent, claim, publication["source_ref"]
                )
                if not events or events[-1].phase != "ready":
                    raise AuthorityRevoked("knowledge_native_readback_missing")
                receipts.append(events[-1].payload)
            context.operation.state = "waiting"
            context.operation.gaps = ["knowledge_native_use_unqualified"]
            context.operation.revision += 1
            context.operation.readback_ref = {
                "knowledge_publications": receipts,
                "content_written": True,
                "native_use_qualified": False,
            }
            context.work.completed_at = self.clock()
            await session.commit()
            return receipts
