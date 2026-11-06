"""Run-owned, offline I22 registry-to-projection scenarios."""

import copy
import importlib.util
import pathlib
import unittest
from decimal import Decimal
from unittest.mock import Mock


INSIGHTS = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, INSIGHTS / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter = load("i22_registry_adapter", "emitter/registry_adapter.py")
projection = load("i22_projection", "projection.py")
TENANT = "example-tenant"
PROJECT = "example-project"
EVENT_TIME = "2026-10-19T12:00:00Z"
SOURCE_REVISION = "example-fixture-revision-1"


def reference(kind, identifier, project=PROJECT, source="approved.catalog"):
    return {"tenant_id": TENANT, "project_id": project,
            "source_connection_id": source, "record_type": kind,
            "record_id": identifier, "record_revision": SOURCE_REVISION}


def native(kind, identifier, **attributes):
    return {"subject_ref": reference(kind, identifier, source="native.source"),
            "kind": kind, "effective_at": EVENT_TIME,
            "recorded_at": EVENT_TIME, **attributes}


def link(kind, identifier, source, target, *, recorded=EVENT_TIME,
         start="2026-10-18T00:00:00Z", revision="1", mapping="approved-1", end=None):
    version = {"relationship_type": kind, "relationship_id": identifier,
               "relationship_revision": revision, "source_connection_id": "approved.catalog",
               "from_ref": source, "to_ref": target, "valid_from": start,
               "recorded_at": recorded, "mapping_version": mapping,
               "source_refs": [source, target]}
    if end is not None:
        version["valid_to"] = end
    return version


class MemorySink:
    def __init__(self):
        self.rows = {}
        self.quarantined = []

    def admit(self, key, fact):
        if key in self.rows:
            return copy.deepcopy(self.rows[key])
        self.rows[key] = copy.deepcopy(fact)
        return None

    def quarantine(self, reason):
        self.quarantined.append(reason)


class CatalogFixture:
    def __init__(self, relationships):
        self.snapshots = {}
        for version in relationships:
            identifier = version["relationship_id"]
            revision = version["relationship_revision"]
            subject = reference("relationship", identifier, project=version["from_ref"]["project_id"])
            subject["record_revision"] = revision
            key = (identifier, revision)
            self.snapshots[key] = {
                "subject_ref": subject,
                "source_ref": reference("catalog_change", identifier + "-r" + revision + "-event",
                                        project=subject["project_id"]),
                "source_revisions": [subject], "source_event_id": identifier + "-r" + revision + "-event",
                "source_event_time": version["recorded_at"],
                "effective_at": version["valid_from"],
                "recorded_at": version["recorded_at"],
                "mapping_version": version["mapping_version"],
                "record_type": "context_relationship", "source_snapshot": {},
                "relationship_version": copy.deepcopy(version),
            }

    def readback(self, subject):
        return copy.deepcopy(self.snapshots[(subject["record_id"], subject["record_revision"])])


class ScenarioFixture:
    def __init__(self, relationships):
        self.source = CatalogFixture(relationships)
        self.sink = MemorySink()
        self.grants = Mock(return_value=True)
        self.registry = adapter.RegistryAdapter(
            None, self.source, self.grants, self.sink, "approved-1",
            clock=lambda: "2026-10-20T00:00:00Z",
        )

    def admitted_links(self):
        for readback in self.source.snapshots.values():
            self.registry.ingest_catalog(readback["subject_ref"], "example-publisher")
        return [copy.deepcopy(fact["relationship_version"])
                for fact in self.sink.rows.values() if fact["record_type"] == "context_relationship"]


def insights_deduplication():
    cost = native("cost", "example-cost", amount="10.00", currency="USD")
    work = native("work", "example-work")
    product = reference("product", "example-product", source="adp.profile")
    portfolios = [reference("portfolio", identifier) for identifier in
                  ("example-commerce", "example-shared")]
    relationships = [link("work_product", f"example-{fact['kind']}-mapping",
                          fact["subject_ref"], product) for fact in (cost, work)]
    relationships += [link("portfolio_product", f"example-membership-{index}",
                           portfolio, product) for index, portfolio in enumerate(portfolios)]
    fixture = ScenarioFixture(relationships)
    admitted = fixture.admitted_links()
    result = projection.query_authorized_projection(
        [cost, work], admitted, [], tenant_id=TENANT, project_ids=[PROJECT],
        can_read_project=lambda tenant, project: (tenant, project) == (TENANT, PROJECT),
        portfolio_ids=[portfolio["record_id"] for portfolio in portfolios],
    )
    assert len(fixture.sink.rows) == len(relationships)
    assert fixture.sink.quarantined == []
    assert result["coverage"] == "complete"
    assert result["work_count"] == 1
    assert result["cost_by_currency"] == {"USD": Decimal("10.00")}
    assert result["unallocated_by_currency"] == {"USD": Decimal("10.00")}
    assert {tuple(association["portfolio_ids"]) for association in result["associations"]} == {
        ("example-commerce", "example-shared")
    }
    assert {tuple(association["subject_ref"].items()) for association in result["associations"]} == {
        tuple(cost["subject_ref"].items()), tuple(work["subject_ref"].items())
    }
    assert fixture.grants.call_count >= len(relationships)


class I22Scenarios(unittest.TestCase):
    def test_insights_deduplication(self):
        insights_deduplication()




def insights_effective_time():
    cost = native("cost", "example-historical-cost", amount="10.00", currency="USD")
    original = copy.deepcopy(cost)
    product = reference("product", "example-product", source="adp.profile")
    old_portfolio = reference("portfolio", "example-commerce")
    new_portfolio = reference("portfolio", "example-shared")
    change_time = "2026-10-21T00:00:00Z"
    links = [
        link("work_product", "example-attribution", cost["subject_ref"], product),
        link("work_product", "example-attribution", cost["subject_ref"], product,
             revision="2", mapping="approved-2", recorded=change_time),
        link("portfolio_product", "example-membership", old_portfolio, product),
        link("portfolio_product", "example-membership", old_portfolio, product,
             revision="2", recorded=change_time, end="2026-10-20T00:00:00Z"),
        link("portfolio_product", "example-new-membership", new_portfolio, product,
             recorded=change_time, start="2026-10-20T00:00:00Z"),
    ]
    fixture = ScenarioFixture(links)
    admitted = fixture.admitted_links()
    def query(regroup_at=None):
        return projection.query_authorized_projection(
            [cost], admitted, [], tenant_id=TENANT, project_ids=[PROJECT],
            can_read_project=lambda tenant, project: (tenant, project) == (TENANT, PROJECT),
            regroup_at=regroup_at,
        )

    historical = query()
    current = query("2026-10-22T00:00:00Z")
    observed_before_event = [snapshot["relationship_version"] for snapshot
                             in fixture.source.snapshots.values()
                             if snapshot["recorded_at"] <= EVENT_TIME]
    observed_old = [version for version in observed_before_event
                    if version["relationship_type"] == "portfolio_product"]
    observed_policy = [version["mapping_version"] for version in observed_before_event
                       if version["relationship_type"] == "work_product"]
    assert {version["from_ref"]["record_id"] for version in observed_old} == {"example-commerce"}
    assert observed_policy == ["approved-1"]
    assert len(fixture.sink.rows) == len(links)
    assert fixture.sink.quarantined == []
    assert historical["coverage"] == current["coverage"] == "complete"
    assert historical["contexts"][0]["grouping"] == "historical"
    assert historical["contexts"][0]["portfolio_ids"] == ["example-commerce"]
    assert historical["contexts"][0]["relationship_versions"][0]["mapping_version"] == "approved-1"
    assert historical["contexts"][0]["relationship_versions"][0]["relationship_revision"] == "1"
    assert current["contexts"][0]["grouping"] == "current_regrouping"
    assert current["contexts"][0]["portfolio_ids"] == ["example-shared"]
    assert current["contexts"][0]["relationship_versions"][0]["mapping_version"] == "approved-2"
    assert current["contexts"][0]["relationship_versions"][0]["relationship_revision"] == "2"
    assert historical["cost_by_currency"] == current["cost_by_currency"] == {"USD": Decimal("10.00")}
    assert historical["contexts"][0]["subject_ref"] == current["contexts"][0]["subject_ref"] == cost["subject_ref"]
    assert cost == original


class I22EffectiveTimeScenario(unittest.TestCase):
    def test_insights_effective_time(self):
        insights_effective_time()

    def test_contribution_preserves_nonoverlapping_work_intervals(self):
        start = "2026-10-18T00:00:00Z"
        work = native("work", "example-interval-work")
        product = reference("product", "example-product", source="adp.profile")
        portfolio = reference("portfolio", "example-commerce")
        initiative = reference("initiative", "example-initiative")
        contribution = reference("contribution", "example-contribution")
        fixture = ScenarioFixture([
            link("portfolio_product", "example-membership", portfolio, product,
                 recorded=start),
        ])
        snapshot = {
            "subject_ref": contribution,
            "source_ref": reference("catalog_change", "example-contribution-event"),
            "source_revisions": [contribution, product, initiative, work["subject_ref"]],
            "source_event_id": "example-contribution-event",
            "source_event_time": start, "effective_at": start,
            "recorded_at": start, "mapping_version": "approved-1",
            "record_type": "context_record",
            "source_snapshot": {
                "product_ref": product, "initiative_ref": initiative,
                "valid_from": start,
                "work_links": [
                    {"work_ref": work["subject_ref"], "valid_from": start,
                     "valid_to": "2026-10-19T00:00:00Z"},
                    {"work_ref": work["subject_ref"],
                     "valid_from": "2026-10-19T06:00:00Z"},
                ],
            },
        }
        fixture.source.snapshots[(contribution["record_id"], contribution["record_revision"])] = snapshot
        admitted_links = fixture.admitted_links()
        admitted_contributions = [copy.deepcopy(fact) for fact in fixture.sink.rows.values()
                                  if fact["record_type"] == "context_record"]
        self.assertEqual(fixture.sink.quarantined, [])
        self.assertEqual(len(admitted_contributions), 1)
        self.assertEqual(admitted_contributions[0]["source_snapshot"], snapshot["source_snapshot"])
        original = copy.deepcopy(admitted_contributions)

        for regroup in (False, True):
            for when, active in (
                (start, True),
                ("2026-10-18T12:00:00Z", True),
                ("2026-10-19T00:00:00Z", False),
                ("2026-10-19T03:00:00Z", False),
                ("2026-10-19T06:00:00Z", True),
                (EVENT_TIME, True),
            ):
                with self.subTest(regroup=regroup, when=when):
                    fact = work if regroup else {**work, "effective_at": when}
                    result = projection.query_authorized_projection(
                        [fact], admitted_links, [], tenant_id=TENANT, project_ids=[PROJECT],
                        can_read_project=lambda tenant, project: (tenant, project) == (TENANT, PROJECT),
                        contributions=admitted_contributions, regroup_at=when if regroup else None,
                    )
                    self.assertEqual(result["work_count"], 1)
                    self.assertEqual(result["coverage"], "complete" if active else "incomplete")
                    context = result["contexts"][0]
                    self.assertEqual(context["subject_ref"], work["subject_ref"])
                    self.assertEqual(context["effective_at"], when)
                    self.assertEqual(context["grouping"], "current_regrouping" if regroup else "historical")
                    self.assertEqual(context["product_refs"], [product] if active else [])
                    self.assertEqual(context["portfolio_ids"], [portfolio["record_id"]] if active else [])
                    self.assertEqual(context["initiative_refs"], [initiative] if active else [])
                    self.assertEqual(context["contribution_refs"], [contribution] if active else [])
        self.assertEqual(admitted_contributions, original)




def insights_projection_access():
    cost = native("cost", "example-allowed-cost", amount="10.00", currency="USD")
    euros = native("cost", "example-euro-cost", amount="5.00", currency="EUR")
    work = native("work", "example-allowed-work")
    private_project = "private-project"
    hidden = native("cost", "private-cost", amount="999.00", currency="USD")
    hidden["subject_ref"]["project_id"] = private_project
    original = copy.deepcopy([cost, euros, work, hidden])
    product = reference("product", "example-product", source="adp.profile")
    portfolio = reference("portfolio", "example-commerce")
    initiative = reference("initiative", "example-initiative")
    contribution = reference("contribution", "example-contribution")
    hidden_product = reference("product", "private-product", project=private_project,
                               source="adp.profile")
    hidden_portfolio = reference("portfolio", "private-portfolio", project=private_project)
    relationships = [
        link("work_product", "example-cost-link", cost["subject_ref"], product),
        link("work_product", "example-euro-link", euros["subject_ref"], product),
        link("portfolio_product", "example-membership", portfolio, product),
        link("work_product", "private-cost-link", hidden["subject_ref"], hidden_product),
        link("portfolio_product", "private-membership", hidden_portfolio, hidden_product),
    ]
    fixture = ScenarioFixture(relationships)
    fixture.source.snapshots[(contribution["record_id"], contribution["record_revision"])] = {
        "subject_ref": contribution,
        "source_ref": reference("catalog_change", "example-contribution-event"),
        "source_revisions": [contribution, product, initiative, work["subject_ref"]],
        "source_event_id": "example-contribution-event",
        "source_event_time": EVENT_TIME, "effective_at": "2026-10-18T00:00:00Z",
        "recorded_at": EVENT_TIME, "mapping_version": "approved-1",
        "record_type": "context_record",
        "source_snapshot": {
            "product_ref": product, "initiative_ref": initiative,
            "valid_from": "2026-10-18T00:00:00Z",
            "work_links": [{"work_ref": work["subject_ref"],
                            "valid_from": "2026-10-18T00:00:00Z"}],
        },
    }
    admitted = fixture.admitted_links()
    admitted_contributions = [copy.deepcopy(fact) for fact in fixture.sink.rows.values()
                              if fact["record_type"] == "context_record"]
    can_read = Mock(side_effect=lambda tenant, project: (tenant, project) == (TENANT, PROJECT))
    allocation = {
        "allocation_id": "example-approved-allocation",
        "source_ref": cost["subject_ref"], "target_ref": initiative,
        "amount": "6.00", "currency": "USD",
        "allocation_policy_version": "approved-allocation-1",
        "allocation_basis": "approved-direct-cost",
    }
    result = projection.query_authorized_projection(
        [work, cost, euros, hidden], admitted, [allocation],
        tenant_id=TENANT, project_ids=[PROJECT, private_project],
        can_read_project=can_read, portfolio_ids=[portfolio["record_id"]],
        contributions=admitted_contributions,
    )
    observed_native = [fact for fact in original
                       if fact["subject_ref"]["project_id"] == PROJECT]
    assert can_read.call_count >= 2
    assert len(fixture.sink.rows) == len(relationships) + 1
    assert fixture.sink.quarantined == []
    assert len(observed_native) == 3
    assert result["coverage"] == "incomplete"
    assert result["work_count"] == 1
    assert result["cost_by_currency"] == {"USD": Decimal("10.00"), "EUR": Decimal("5.00")}
    assert result["allocated_by_currency"] == {"USD": Decimal("6.00")}
    assert result["unallocated_by_currency"] == {"USD": Decimal("4.00"), "EUR": Decimal("5.00")}
    assert result["allocation_versions"][0]["allocation_policy_version"] == "approved-allocation-1"
    assert result["contexts"][0]["initiative_refs"] == [initiative]
    assert result["contexts"][0]["contribution_refs"] == [contribution]
    assert result["contexts"][0]["relationship_versions"][0]["relationship_id"] == "example-contribution:example-allowed-work"
    assert {row["subject_ref"]["record_id"] for row in result["associations"]} == {
        "example-allowed-work", "example-allowed-cost", "example-euro-cost"
    }
    assert private_project not in str(result)
    assert "private-cost" not in str(result)
    assert [cost, euros, work, hidden] == original


class I22AccessScenario(unittest.TestCase):
    def test_insights_projection_access(self):
        insights_projection_access()


if __name__ == "__main__":
    unittest.main()
