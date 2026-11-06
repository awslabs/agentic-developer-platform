"""Fenced knowledge publication; ambiguous native effects recover read-only."""

import asyncio

from l5_services.devops.knowledge_effects import KnowledgeEffects
from l5_services.devops.knowledge_work import current_context, resolve_admitted
from l5_services.workers.execution import AuthorityRevoked


class KnowledgeSetupWorker:
    def __init__(self, sessions, sources, driver, verify, *, clock=None):
        self.sessions, self.sources, self.driver, self.verify = (
            sessions,
            sources,
            driver,
            verify,
        )
        self.effects = KnowledgeEffects(sessions, clock)
        self.clock = self.effects.clock

    async def execute(self, intent, claim):
        async def check():
            # verify performs current owner/source/native-purpose authorization.
            await self.verify(intent, claim)
            async with self.sessions() as session:
                return await current_context(session, intent, claim, now=self.clock())

        context = await check()
        assets = await resolve_admitted(context, claim, self.sources, check)
        for asset in assets:
            await check()
            retained = await self.effects.inspect(intent, claim, asset.source_ref)
            plan = retained["plan"]
            if plan is None:
                plan = await asyncio.to_thread(
                    self.driver.prepare,
                    context.binding.native_resource_ref["record_id"],
                    asset,
                    intent.operation_id,
                    retained["ownership"],
                )
                await check()
                await self.effects.plan(intent, claim, asset.source_ref, plan)
            if retained["phase"] == "ready":
                # A completed source also needs current independent readback.
                # Its immutable result history is never overwritten on recovery.
                receipt = await asyncio.to_thread(self.driver.reconcile, plan, asset)
                if receipt != retained["receipt"]:
                    raise AuthorityRevoked("knowledge_native_receipt_changed")
                await check()
                continue
            await check()
            started = await self.effects.start(intent, claim, asset.source_ref)
            try:
                await check()
                # A retained started/unknown effect MUST NOT call mutate, even
                # when readback is absent or its metadata differs.
                method = self.driver.mutate if started else self.driver.reconcile
                receipt = await asyncio.to_thread(method, plan, asset)
                await check()
                await self.effects.finish(intent, claim, asset.source_ref, receipt)
            except Exception:
                try:
                    await self.effects.unknown(intent, claim, asset.source_ref)
                except AuthorityRevoked:
                    # The durable started event already preserves uncertainty;
                    # a stale worker cannot append or replace a newer receipt.
                    pass
                raise
        await check()
        return await self.effects.complete(intent, claim)
