"""Offline assertions for Insights' read-only business-context rollup."""

import importlib.util
import pathlib
import unittest
from decimal import Decimal


PROJECTION_PATH = pathlib.Path(__file__).resolve().parents[1] / "projection.py"
spec = importlib.util.spec_from_file_location("insights_projection", PROJECTION_PATH)
projection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(projection)


def native_fact(kind, identifier, **fields):
    return {
        "subject_ref": {
            "tenant_id": "example-tenant", "project_id": "example-project",
            "source_connection_id": "example-ledger", "record_type": kind,
            "record_id": identifier, "record_revision": "1",
        },
        "kind": kind,
        **fields,
    }


class DeduplicationTests(unittest.TestCase):
    def test_union_counts_each_original_fact_once_and_keeps_secondary_memberships(self):
        cost = native_fact("cost", "example-cost", amount="10.00", currency="USD")
        work = native_fact("work", "example-work")
        rows = [(cost, ["commerce"]), (cost, ["shared"]),
                (work, ["commerce", "shared"])]
        result = projection.aggregate_work_cost(rows, ["commerce", "shared"])

        self.assertEqual(result["work_count"], 1)
        self.assertEqual(result["cost_by_currency"], {"USD": Decimal("10.00")})
        self.assertEqual(result["associations"], [
            {"subject_ref": cost["subject_ref"], "portfolio_ids": ["commerce", "shared"]},
            {"subject_ref": work["subject_ref"], "portfolio_ids": ["commerce", "shared"]},
        ])
        self.assertEqual(projection.aggregate_work_cost(rows, ["shared"])["cost_by_currency"],
                         {"USD": Decimal("10.00")})

    def test_repeated_rows_do_not_add_overlapping_portfolio_totals(self):
        cost = native_fact("cost", "example-cost", amount="10.00", currency="USD")
        rows = [(cost, ["commerce"]), (cost, ["commerce"]),
                (cost, ["shared"])]
        self.assertEqual(projection.aggregate_work_cost(rows)["cost_by_currency"],
                         {"USD": Decimal("10.00")})

    def test_conflicting_native_fact_identity_is_not_silently_deduplicated(self):
        cost = native_fact("cost", "example-cost", amount="10.00", currency="USD")
        changed = native_fact("cost", "example-cost", amount="20.00", currency="USD")
        with self.assertRaisesRegex(ValueError, "conflicting versions"):
            projection.aggregate_work_cost([(cost, ["commerce"]), (changed, ["shared"])])




def relationship(kind, identifier, source, target, revision="1", mapping="approved-1",
                 recorded="2026-10-19T11:00:00Z", start="2026-10-18T00:00:00Z", end=None):
    result = {
        "relationship_type": kind, "relationship_id": identifier,
        "relationship_revision": revision, "source_connection_id": "approved.catalog",
        "from_ref": source, "to_ref": target, "mapping_version": mapping,
        "recorded_at": recorded, "valid_from": start,
    }
    if end is not None:
        result["valid_to"] = end
    return result


class DatedContextTests(unittest.TestCase):
    def setUp(self):
        self.fact = native_fact("work", "example-work")
        self.fact.update(effective_at="2026-10-19T12:00:00Z",
                         recorded_at="2026-10-19T12:01:00Z")
        self.product = native_fact("product", "example-product")["subject_ref"]
        self.portfolio = native_fact("portfolio", "example-commerce")["subject_ref"]
        self.new_portfolio = native_fact("portfolio", "example-shared")["subject_ref"]
        self.work_link = relationship("work_product", "example-attribution",
                                      self.fact["subject_ref"], self.product)
        self.old_membership = relationship("portfolio_product", "example-membership",
                                           self.portfolio, self.product)
        self.new_membership_version = relationship(
            "portfolio_product", "example-membership", self.portfolio, self.product,
            revision="2", recorded="2026-10-21T00:00:00Z",
            end="2026-10-20T00:00:00Z",
        )
        self.new_membership = relationship(
            "portfolio_product", "example-new-membership", self.new_portfolio, self.product,
            recorded="2026-10-21T00:00:00Z", start="2026-10-20T00:00:00Z",
        )
        self.revised_policy = relationship(
            "work_product", "example-attribution", self.fact["subject_ref"], self.product,
            revision="2", mapping="approved-2", recorded="2026-10-21T00:00:00Z",
        )
        self.relationships = [self.work_link, self.old_membership,
                              self.new_membership_version, self.new_membership,
                              self.revised_policy]

    def test_historical_context_is_stable_and_current_regrouping_is_labelled(self):
        historical = projection.resolve_dated_context(self.fact, self.relationships)
        current = projection.resolve_dated_context(
            self.fact, self.relationships, regroup_at="2026-10-22T00:00:00Z"
        )
        self.assertEqual(historical["grouping"], "historical")
        self.assertEqual(historical["portfolio_ids"], ["example-commerce"])
        self.assertEqual(historical["subject_ref"], self.fact["subject_ref"])
        self.assertEqual(historical["relationship_versions"][0]["mapping_version"], "approved-1")
        self.assertEqual(historical["relationship_versions"][1]["relationship_revision"], "1")
        self.assertEqual(historical["relationship_versions"][0]["to_ref"], self.product)
        self.assertEqual(current["grouping"], "current_regrouping")
        self.assertEqual(current["portfolio_ids"], ["example-shared"])
        self.assertEqual(current["relationship_versions"][0]["mapping_version"], "approved-2")
        self.assertEqual(current["relationship_versions"][0]["relationship_revision"], "2")

    def test_future_policy_does_not_rewrite_historical_context_when_input_is_reordered(self):
        observed = projection.resolve_dated_context(self.fact, list(reversed(self.relationships)))
        self.assertEqual(observed["portfolio_ids"], ["example-commerce"])
        self.assertEqual(observed["relationship_versions"][0]["mapping_version"], "approved-1")
        effective_end = projection.select_dated_relationships(
            self.relationships, "2026-10-20T00:00:00Z", "2026-10-22T00:00:00Z"
        )
        self.assertNotIn("example-membership", [link["relationship_id"] for link in effective_end])

    def test_ambiguous_revisions_and_naive_regrouping_fail_closed(self):
        ambiguous = {**self.old_membership, "relationship_revision": "9",
                     "mapping_version": "unapproved"}
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            projection.resolve_dated_context(
                self.fact, [self.work_link, self.old_membership, ambiguous]
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            projection.resolve_dated_context(
                self.fact, self.relationships, regroup_at="2026-10-22T00:00:00"
            )



class AuthorizedProjectionTests(unittest.TestCase):
    def setUp(self):
        self.work = native_fact("work", "example-work")
        self.cost = native_fact("cost", "example-cost", amount="10.00", currency="USD")
        self.hidden = native_fact("cost", "private-cost", amount="999.00", currency="USD")
        self.hidden["subject_ref"]["project_id"] = "private-project"
        for fact in (self.work, self.cost, self.hidden):
            fact.update(effective_at="2026-10-19T12:00:00Z",
                        recorded_at="2026-10-19T12:01:00Z")
        self.product = native_fact("product", "example-product")["subject_ref"]
        self.portfolio = native_fact("portfolio", "example-commerce")["subject_ref"]
        self.initiative = native_fact("initiative", "example-initiative")["subject_ref"]
        self.links = [relationship("work_product", "example-work-link",
                                   self.work["subject_ref"], self.product),
                      relationship("work_product", "example-cost-link",
                                   self.cost["subject_ref"], self.product),
                      relationship("portfolio_product", "example-membership",
                                   self.portfolio, self.product)]
        self.contribution = {
            "subject_ref": native_fact("contribution", "example-contribution")["subject_ref"],
            "recorded_at": "2026-10-19T11:00:00Z",
            "mapping_version": "approved-1",
            "source_snapshot": {
                "product_ref": self.product, "initiative_ref": self.initiative,
                "valid_from": "2026-10-18T00:00:00Z",
                "work_links": [{"work_ref": self.work["subject_ref"],
                                "valid_from": "2026-10-18T00:00:00Z"}],
            },
        }
        self.allocation = {
            "allocation_id": "example-approved-allocation",
            "source_ref": self.cost["subject_ref"],
            "target_ref": self.initiative,
            "amount": "6.25", "currency": "USD",
            "allocation_policy_version": "approved-policy-1",
            "allocation_basis": "approved-direct-cost",
        }

    def query(self, *, facts=None, links=None, allocations=None, contributions=None,
              project_ids=("example-project", "private-project")):
        return projection.query_authorized_projection(
            facts if facts is not None else [self.work, self.cost, self.hidden],
            links if links is not None else self.links,
            allocations if allocations is not None else [self.allocation],
            tenant_id="example-tenant", project_ids=project_ids,
            can_read_project=lambda tenant, project: tenant == "example-tenant" and project == "example-project",
            portfolio_ids=["example-commerce"],
            contributions=contributions if contributions is not None else [self.contribution],
        )

    def test_partial_access_keeps_original_scope_and_exposes_unallocated_remainder(self):
        result = self.query()
        self.assertEqual(result["coverage"], "incomplete")
        self.assertEqual(result["work_count"], 1)
        self.assertEqual(result["cost_by_currency"], {"USD": Decimal("10.00")})
        self.assertEqual(result["allocated_by_currency"], {"USD": Decimal("6.25")})
        self.assertEqual(result["unallocated_by_currency"], {"USD": Decimal("3.75")})
        self.assertEqual(result["allocation_versions"][0]["allocation_policy_version"],
                         "approved-policy-1")
        self.assertEqual(result["contexts"][0]["initiative_refs"], [self.initiative])
        self.assertEqual(result["contexts"][0]["contribution_refs"],
                         [self.contribution["subject_ref"]])
        self.assertEqual(result["contexts"][1]["portfolio_ids"], ["example-commerce"])
        self.assertEqual(result["associations"][1]["subject_ref"], self.cost["subject_ref"])

    def test_hidden_facts_links_and_allocations_do_not_reveal_counts_or_ids(self):
        hidden_link = relationship("work_product", "private-link",
                                   self.hidden["subject_ref"], self.product)
        hidden_allocation = {**self.allocation, "allocation_id": "private-allocation",
                             "target_ref": self.hidden["subject_ref"]}
        result = self.query(links=self.links + [hidden_link],
                            allocations=[self.allocation, hidden_allocation])
        self.assertEqual(result["coverage"], "incomplete")
        self.assertNotIn("private-project", str(result))
        self.assertNotIn("private-cost", str(result))
        self.assertNotIn("private-link", str(result))
        self.assertNotIn("private-allocation", str(result))
        self.assertEqual(result["cost_by_currency"], {"USD": Decimal("10.00")})

    def test_per_currency_totals_and_allocation_validation(self):
        euros = native_fact("cost", "example-eur", amount="5.00", currency="EUR")
        euros.update(effective_at=self.cost["effective_at"], recorded_at=self.cost["recorded_at"])
        euro_link = relationship("work_product", "example-euro-link", euros["subject_ref"], self.product)
        result = self.query(facts=[self.work, self.cost, euros], links=self.links + [euro_link],
                            project_ids=("example-project",))
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(result["cost_by_currency"],
                         {"USD": Decimal("10.00"), "EUR": Decimal("5.00")})
        self.assertEqual(result["unallocated_by_currency"],
                         {"USD": Decimal("3.75"), "EUR": Decimal("5.00")})
        for allocations, message in (
            ([{**self.allocation, "currency": "EUR"}], "currency"),
            ([{**self.allocation, "amount": "11.00"}], "exceeds"),
            ([self.allocation, self.allocation], "duplicate"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.query(facts=[self.work, self.cost], allocations=allocations,
                           project_ids=("example-project",))


    def test_hidden_allocation_does_not_misreport_unknown_remainder(self):
        hidden_target = native_fact("initiative", "private-initiative")["subject_ref"]
        hidden_target["project_id"] = "private-project"
        result = self.query(allocations=[self.allocation, {
            **self.allocation, "allocation_id": "unknown-target-allocation",
            "target_ref": hidden_target,
        }])
        self.assertEqual(result["coverage"], "incomplete")
        self.assertNotIn("USD", result["unallocated_by_currency"])
        self.assertNotIn("private-initiative", str(result))
        with self.assertRaisesRegex(ValueError, "approved business scope"):
            self.query(allocations=[{**self.allocation, "target_ref": self.portfolio}],
                       project_ids=("example-project",))

    def test_contribution_correction_uses_latest_known_version(self):
        corrected = {
            **self.contribution,
            "subject_ref": {**self.contribution["subject_ref"], "record_revision": "2"},
            "recorded_at": "2026-10-19T11:30:00Z",
            "source_snapshot": {**self.contribution["source_snapshot"], "work_links": []},
        }
        result = self.query(contributions=[self.contribution, corrected],
                            project_ids=("example-project",))
        self.assertEqual(result["contexts"][0]["initiative_refs"], [])
        self.assertEqual(result["contexts"][0]["contribution_refs"], [])
        historical = self.query(contributions=[self.contribution, {
            **corrected, "recorded_at": "2026-10-21T11:30:00Z"}],
            project_ids=("example-project",))
        self.assertEqual(historical["contexts"][0]["contribution_refs"],
                         [self.contribution["subject_ref"]])

    def test_ambiguous_contribution_and_empty_regrouping_are_explicit(self):
        ambiguous = {
            **self.contribution,
            "subject_ref": {**self.contribution["subject_ref"], "record_revision": "2"},
        }
        with self.assertRaisesRegex(ValueError, "ambiguous contribution"):
            self.query(contributions=[self.contribution, ambiguous],
                       project_ids=("example-project",))
        result = projection.query_authorized_projection(
            [], [], [], tenant_id="example-tenant", project_ids=("example-project",),
            can_read_project=lambda tenant, project: True,
            regroup_at="2026-10-22T00:00:00Z",
        )
        self.assertEqual(result["grouping"], "current_regrouping")
        self.assertEqual(result["coverage"], "complete")


if __name__ == "__main__":
    unittest.main()
