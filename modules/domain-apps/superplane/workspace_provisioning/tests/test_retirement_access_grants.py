"""Provider uncertainty cannot turn temporary cleanup access into replayable writes."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from workspace_provisioning.retirement_access_grants import establish_access_grants
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_retirement_access_plan import compile_plan, inputs


class Journal:
    def __init__(self, plan):
        self.recipe = plan.recipe()
        self.authority = AsyncMock()
        self.events = {}

    async def intend(self, key, descriptor):
        assert descriptor == self.recipe[key]
        if key in self.events:
            if self.events[key] is None:
                raise LifecycleRefused("ambiguous intent")
            return self.events[key]
        self.events[key] = None

    async def confirm(self, key, descriptor, result):
        assert key in self.events and self.events[key] is None
        assert descriptor == self.recipe[key]
        self.events[key] = deepcopy(result)

    async def complete(self):
        assert set(self.events) == set(self.recipe)
        assert all(self.events.values())
        return deepcopy(self.events)


class Grants:
    def __init__(self, journal):
        self.journal = journal
        self.objects = {}
        self.created = []
        self.lose_reply = False
        self.bad_readback = False

    def observe(self, spec):
        value = self.objects.get(spec["key"])
        if value and self.bad_readback:
            return {**value, "uid": "replacement"}
        return deepcopy(value)

    def verify(self, spec, identity):
        if identity.get("generation") != spec["generation"]:
            raise LifecycleRefused("different generation")

    def create(self, spec):
        # Independent wire-side assertion: no provider mutation precedes intent.
        assert spec["key"] in self.journal.events
        assert self.journal.events[spec["key"]] is None
        assert spec["key"] not in self.objects
        value = {"uid": spec["key"], "generation": spec["generation"]}
        self.created.append(spec["key"])
        self.objects[spec["key"]] = value
        if self.lose_reply:
            raise TimeoutError("provider accepted the mutation but lost its reply")
        return deepcopy(value)


@pytest.fixture
def case():
    plan = compile_plan(*inputs())
    journal = Journal(plan)
    return SimpleNamespace(
        plan=plan,
        journal=journal,
        eks=Grants(journal),
        kubernetes=Grants(journal),
        verify_target=AsyncMock(),
    )


async def run(case):
    return await establish_access_grants(
        case.plan,
        case.journal,
        eks=case.eks,
        kubernetes=case.kubernetes,
        verify_target=case.verify_target,
    )


@pytest.mark.asyncio
async def test_every_provider_write_has_intent_and_exact_confirmed_readback(case):
    result = await run(case)
    assert set(result) == {spec["key"] for spec in case.plan.grants}
    assert case.eks.created == ["registrar-entry", "registrar-policy", "cleaner-entry"]
    assert case.kubernetes.created
    case.verify_target.assert_awaited()


@pytest.mark.asyncio
async def test_lost_create_reply_keeps_intent_and_cannot_repeat_the_write(case):
    case.eks.lose_reply = True
    with pytest.raises(TimeoutError):
        await run(case)
    assert case.journal.events == {"registrar-entry": None}
    case.eks.lose_reply = False
    with pytest.raises(LifecycleRefused, match="ambiguous"):
        await run(case)
    assert case.eks.created == ["registrar-entry"]
    assert case.kubernetes.created == []


@pytest.mark.asyncio
async def test_preexisting_grant_is_never_adopted_or_overwritten(case):
    first = case.plan.grants[0]
    case.eks.objects[first["key"]] = {
        "uid": "unrelated",
        "generation": first["generation"],
    }
    with pytest.raises(LifecycleRefused, match="already exists"):
        await run(case)
    assert not case.eks.created
    assert case.journal.events == {first["key"]: None}


@pytest.mark.asyncio
async def test_replaced_readback_cannot_confirm_the_original_create(case):
    case.eks.bad_readback = True
    with pytest.raises(LifecycleRefused, match="readback"):
        await run(case)
    assert case.journal.events == {"registrar-entry": None}
    assert case.eks.created == ["registrar-entry"]


@pytest.mark.asyncio
async def test_confirmed_grants_are_observed_without_new_provider_mutations(case):
    first = await run(case)
    calls = case.eks.created[:], case.kubernetes.created[:]
    assert await run(case) == first
    assert (case.eks.created, case.kubernetes.created) == calls


@pytest.mark.asyncio
async def test_lost_confirmed_grant_refuses_instead_of_recreating_it(case):
    await run(case)
    case.eks.objects.pop("registrar-entry")
    calls = case.eks.created[:]
    with pytest.raises(LifecycleRefused, match="changed or disappeared"):
        await run(case)
    assert case.eks.created == calls


@pytest.mark.asyncio
async def test_changed_target_stops_before_any_grant_is_created(case):
    case.verify_target.side_effect = LifecycleRefused("namespace was replaced")
    with pytest.raises(LifecycleRefused, match="replaced"):
        await run(case)
    assert not case.eks.created
    assert not case.kubernetes.created


@pytest.mark.asyncio
async def test_a_different_recipe_is_refused_before_provider_observation(case):
    case.journal.recipe = {**case.journal.recipe, "extra": {}}
    with pytest.raises(LifecycleRefused, match="journal differs"):
        await run(case)
    case.verify_target.assert_not_awaited()
    assert case.journal.events == {}
