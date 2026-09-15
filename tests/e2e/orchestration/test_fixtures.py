"""Fixture lifecycle tests (#5156).

Proves provisioning, resume and cleanup behave safely when a run is
interrupted: no duplicate ownership after an interrupted provision, and cleanup
refuses anything it cannot positively prove it owns. Network-free — the
provider is a fake that can be told to fail at an exact point.
"""

from __future__ import annotations

import pytest

from tests.e2e.orchestration.fixtures import (
    BoundExceededError,
    FixtureError,
    FixtureRequest,
    cleanup,
    provision,
    readback,
    resume,
)
from tests.e2e.orchestration.inventory import (
    CREATED,
    DELETED,
    PLANNED,
    RECONCILE_FAILED,
    Inventory,
)

QUAL_ID = "q-0123456789abcdef"


class FakeProvider:
    """An in-memory provider that records calls and can fail on demand.

    ``resources`` maps resource id -> tags, standing in for the account. The
    ``fail_*`` switches let a test stop the world at exactly the point the
    issue's failure modes describe.
    """

    def __init__(self, kind: str = "organization", *, honour_idempotency: bool = True):
        self.kind = kind
        self.resources: dict[str, dict[str, str]] = {}
        self.honour_idempotency = honour_idempotency
        self.tokens: dict[str, str] = {}  # token -> resource id
        self.create_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.fail_create = False
        self.fail_create_after_resource_exists = False
        self.fail_find = False
        self.fail_read_tags = False
        self.unreadable_tags = False
        self.fail_delete = False
        self._counter = 0

    def create(self, *, intended_identity: str, ownership_tags: dict[str, str], idempotency_token: str) -> str:
        self.create_calls.append(intended_identity)
        if self.honour_idempotency and idempotency_token in self.tokens:
            return self.tokens[idempotency_token]
        if self.fail_create:
            raise RuntimeError("provider rejected the create")
        self._counter += 1
        resource_id = f"{self.kind}-{self._counter}"
        self.resources[resource_id] = dict(ownership_tags)
        self.tokens[idempotency_token] = resource_id
        if self.fail_create_after_resource_exists:
            # The resource EXISTS but the caller never learns its id: exactly the
            # crash-after-create case that would leak without an inventory.
            raise RuntimeError("connection dropped after the resource was created")
        return resource_id

    def find(self, *, intended_identity: str, idempotency_token: str) -> str | None:
        if self.fail_find:
            raise RuntimeError("provider unreachable")
        return self.tokens.get(idempotency_token)

    def read_tags(self, resource_id: str) -> dict[str, str] | None:
        if self.fail_read_tags:
            raise RuntimeError("tag read failed")
        if self.unreadable_tags:
            return None
        return self.resources.get(resource_id)

    def delete(self, resource_id: str) -> None:
        self.delete_calls.append(resource_id)
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.resources.pop(resource_id, None)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def inventory(valid_config) -> Inventory:
    return Inventory.create(valid_config.artifact_directory, QUAL_ID, valid_config.environment)


ORG = FixtureRequest(fixture_id="org", kind="organization", intended_identity="qual-org")


class TestProvision:
    def test_happy_path_records_the_observed_resource_id(self, inventory, valid_config, provider):
        record = provision(inventory, valid_config, provider, ORG)
        assert record.state == CREATED
        assert record.observed_resource_id == "organization-1"
        assert provider.resources["organization-1"]["adp:qualification-id"] == QUAL_ID

    def test_ownership_tags_are_stamped_on_the_resource(self, inventory, valid_config, provider):
        """Cleanup can only verify ownership if provisioning tagged it."""
        provision(inventory, valid_config, provider, ORG)
        tags = provider.resources["organization-1"]
        assert tags == valid_config.ownership_tags(QUAL_ID)

    def test_intent_is_persisted_before_the_provider_is_called(self, inventory, valid_config, provider):
        """Asserts the write-ahead ordering by inspecting the file mid-create."""
        seen: list[str] = []
        original = provider.create

        def spy(**kwargs):
            # At this instant the inventory must already name the fixture.
            reloaded = Inventory.load(valid_config.artifact_directory, QUAL_ID, "dev")
            seen.append(reloaded.get("org").state)
            return original(**kwargs)

        provider.create = spy
        provision(inventory, valid_config, provider, ORG)
        assert seen == [PLANNED]

    def test_mismatched_provider_kind_is_refused(self, inventory, valid_config):
        with pytest.raises(FixtureError, match="provider handles"):
            provision(inventory, valid_config, FakeProvider("team"), ORG)

    def test_provider_returning_no_id_is_an_error(self, inventory, valid_config, provider):
        provider.create = lambda **kwargs: ""
        with pytest.raises(FixtureError, match="no resource id"):
            provision(inventory, valid_config, provider, ORG)

    def test_failed_create_retains_the_planned_entry_for_resume(self, inventory, valid_config, provider):
        """The entry must NOT be rolled back: the create may have half-succeeded."""
        provider.fail_create = True
        with pytest.raises(FixtureError, match="retained for resume"):
            provision(inventory, valid_config, provider, ORG)
        assert inventory.get("org").state == PLANNED
        assert inventory.get("org") in inventory.unresolved


class TestBounds:
    def test_provisioning_past_max_resources_is_refused(self, inventory, valid_config, provider):
        """The cap is enforced before the provider call, not observed after."""
        for index in range(valid_config.max_resources):
            provision(
                inventory,
                valid_config,
                provider,
                FixtureRequest(f"org{index}", "organization", f"qual-org{index}"),
            )
        before = len(provider.create_calls)
        with pytest.raises(BoundExceededError, match="max_resources"):
            provision(inventory, valid_config, provider, ORG)
        assert len(provider.create_calls) == before, "provider must not be called once the bound is reached"

    def test_unresolved_planned_fixtures_count_against_the_bound(self, inventory, valid_config, provider):
        """An interrupted run cannot exceed the cap by leaving entries planned."""
        provider.fail_create = True
        for index in range(valid_config.max_resources):
            with pytest.raises(FixtureError):
                provision(
                    inventory,
                    valid_config,
                    provider,
                    FixtureRequest(f"org{index}", "organization", f"qual-org{index}"),
                )
        with pytest.raises(BoundExceededError):
            provision(inventory, valid_config, provider, ORG)


class TestInterruptedProvisioning:
    def test_crash_after_create_is_reconciled_without_a_duplicate(self, inventory, valid_config, provider):
        """The core no-duplicate-ownership guarantee.

        The provider creates the resource then the call fails, so the harness
        never learned the id. Resume finds the existing resource by its derived
        idempotency token and adopts it — no second resource is created.
        """
        provider.fail_create_after_resource_exists = True
        with pytest.raises(FixtureError):
            provision(inventory, valid_config, provider, ORG)
        assert inventory.get("org").state == PLANNED
        assert len(provider.resources) == 1, "the provider did create one resource"

        provider.fail_create_after_resource_exists = False
        unresolved = resume(inventory, {"organization": provider})

        assert inventory.get("org").state == CREATED
        assert inventory.get("org").observed_resource_id == "organization-1"
        assert len(provider.resources) == 1, "resume must not create a second resource"
        assert unresolved == [inventory.get("org")]

    def test_resume_leaves_a_never_created_fixture_retryable(self, inventory, valid_config, provider):
        """Positively absent means the caller may simply retry."""
        provider.fail_create = True
        with pytest.raises(FixtureError):
            provision(inventory, valid_config, provider, ORG)

        resume(inventory, {"organization": provider})
        assert inventory.get("org").state == PLANNED

        # A retry reuses the recorded intent rather than adding a second entry.
        provider.fail_create = False
        inventory.mark_created(
            "org",
            provider.create(
                intended_identity="qual-org",
                ownership_tags=valid_config.ownership_tags(QUAL_ID),
                idempotency_token=f"{QUAL_ID}-org",
            ),
        )
        assert inventory.get("org").state == CREATED
        assert len([f for f in inventory.fixtures if f.fixture_id == "org"]) == 1

    def test_undeterminable_state_is_marked_reconcile_failed(self, inventory, valid_config, provider):
        """An unreachable provider must not be read as 'nothing was created'."""
        provider.fail_create = True
        with pytest.raises(FixtureError):
            provision(inventory, valid_config, provider, ORG)

        provider.fail_find = True
        resume(inventory, {"organization": provider})

        record = inventory.get("org")
        assert record.state == RECONCILE_FAILED
        assert "could not determine" in (record.detail or "")

    def test_missing_provider_for_a_kind_is_reconcile_failed(self, inventory, valid_config, provider):
        """Without a provider the fixture may exist and leak, so flag it."""
        provider.fail_create = True
        with pytest.raises(FixtureError):
            provision(inventory, valid_config, provider, ORG)
        resume(inventory, {})
        assert inventory.get("org").state == RECONCILE_FAILED
        assert "no provider registered" in (inventory.get("org").detail or "")

    def test_resume_is_idempotent(self, inventory, valid_config, provider):
        """Running resume twice must not change a settled inventory."""
        provision(inventory, valid_config, provider, ORG)
        resume(inventory, {"organization": provider})
        first = inventory.path.read_text()
        resume(inventory, {"organization": provider})
        assert inventory.path.read_text() == first
        assert len(provider.create_calls) == 1


class TestReadback:
    def test_readback_confirms_a_real_owned_resource(self, inventory, valid_config, provider):
        provision(inventory, valid_config, provider, ORG)
        record = readback(inventory, provider, "org", valid_config.ownership_tags(QUAL_ID))
        assert record.observed_resource_id == "organization-1"

    def test_readback_fails_when_tags_do_not_match(self, inventory, valid_config, provider):
        """Guards against the provider returning an id it did not tag as ours."""
        provision(inventory, valid_config, provider, ORG)
        provider.resources["organization-1"] = {"adp:qualification-id": "q-somebodyelse00"}
        with pytest.raises(FixtureError, match="could not confirm ownership"):
            readback(inventory, provider, "org", valid_config.ownership_tags(QUAL_ID))

    def test_readback_of_a_planned_fixture_is_refused(self, inventory, valid_config, provider):
        provider.fail_create = True
        with pytest.raises(FixtureError):
            provision(inventory, valid_config, provider, ORG)
        with pytest.raises(FixtureError, match="nothing to read back"):
            readback(inventory, provider, "org", valid_config.ownership_tags(QUAL_ID))


class TestCleanup:
    def test_verified_owned_fixtures_are_deleted(self, inventory, valid_config, provider):
        provision(inventory, valid_config, provider, ORG)
        outcome = cleanup(inventory, valid_config, {"organization": provider})
        assert outcome.deleted == ("org",)
        assert outcome.clean is True
        assert provider.resources == {}
        assert inventory.get("org").state == DELETED

    def test_cleanup_refuses_a_foreign_fixture(self, inventory, valid_config, provider):
        """A resource tagged for another qualification is left untouched."""
        provision(inventory, valid_config, provider, ORG)
        provider.resources["organization-1"]["adp:qualification-id"] = "q-somebodyelse00"

        outcome = cleanup(inventory, valid_config, {"organization": provider})

        assert outcome.deleted == ()
        assert outcome.clean is False
        assert "mismatch" in outcome.refused[0][1]
        assert provider.delete_calls == [], "a foreign resource must never be deleted"
        assert "organization-1" in provider.resources

    def test_cleanup_refuses_when_tags_cannot_be_read(self, inventory, valid_config, provider):
        """Unverifiable ownership is not ownership."""
        provision(inventory, valid_config, provider, ORG)
        provider.unreadable_tags = True
        outcome = cleanup(inventory, valid_config, {"organization": provider})
        assert outcome.deleted == ()
        assert provider.delete_calls == []
        assert "could not be read" in outcome.refused[0][1]

    def test_cleanup_refuses_when_the_tag_read_raises(self, inventory, valid_config, provider):
        provision(inventory, valid_config, provider, ORG)
        provider.fail_read_tags = True
        outcome = cleanup(inventory, valid_config, {"organization": provider})
        assert provider.delete_calls == []
        assert "tags could not be read" in outcome.refused[0][1]

    def test_cleanup_refuses_an_unreconciled_planned_fixture(self, inventory, valid_config, provider):
        """With no verified id, deleting would mean guessing at a resource."""
        provider.fail_create = True
        with pytest.raises(FixtureError):
            provision(inventory, valid_config, provider, ORG)
        outcome = cleanup(inventory, valid_config, {"organization": provider})
        assert provider.delete_calls == []
        assert "run --resume first" in outcome.refused[0][1]

    def test_cleanup_reports_a_failed_delete_without_marking_it_deleted(self, inventory, valid_config, provider):
        """A resource that survived a failed delete must stay in the record."""
        provision(inventory, valid_config, provider, ORG)
        provider.fail_delete = True
        outcome = cleanup(inventory, valid_config, {"organization": provider})
        assert outcome.failed[0][0] == "org"
        assert inventory.get("org").state == CREATED
        assert outcome.clean is False

    def test_cleanup_preserves_evidence_after_deleting(self, inventory, valid_config, provider):
        """Identity and resource id survive cleanup so a leak stays investigable."""
        provision(inventory, valid_config, provider, ORG)
        cleanup(inventory, valid_config, {"organization": provider})
        record = inventory.get("org")
        assert record.state == DELETED
        assert record.observed_resource_id == "organization-1"
        assert record.intended_identity == "qual-org"
        assert record.detail == "ownership verified before delete"

    def test_interrupted_cleanup_resumes_without_redeleting(self, inventory, valid_config, provider):
        """Each delete is flushed, so a second pass only handles what remains."""
        provision(inventory, valid_config, provider, ORG)
        provision(
            inventory,
            valid_config,
            provider,
            FixtureRequest("team", "organization", "qual-team"),
        )
        # First pass deletes only 'org', then is interrupted.
        original_delete = provider.delete

        def delete_once(resource_id: str) -> None:
            original_delete(resource_id)
            raise KeyboardInterrupt("operator interrupted cleanup")

        provider.delete = delete_once
        with pytest.raises(KeyboardInterrupt):
            cleanup(inventory, valid_config, {"organization": provider})

        provider.delete = original_delete
        reloaded = Inventory.load(valid_config.artifact_directory, QUAL_ID, "dev")
        outcome = cleanup(reloaded, valid_config, {"organization": provider})

        # 'org' was already gone and is not deleted twice.
        assert outcome.deleted == ("team",)
        assert provider.resources == {}

    def test_cleanup_of_an_empty_inventory_is_clean(self, inventory, valid_config, provider):
        outcome = cleanup(inventory, valid_config, {"organization": provider})
        assert outcome.deleted == ()
        assert outcome.clean is True

    def test_cleanup_refuses_a_kind_with_no_provider(self, inventory, valid_config, provider):
        provision(inventory, valid_config, provider, ORG)
        outcome = cleanup(inventory, valid_config, {})
        assert "no provider registered" in outcome.refused[0][1]
        assert inventory.get("org").state == CREATED
