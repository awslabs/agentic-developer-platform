"""The reviewed A6 matrix. Configuration cannot remove or relax a criterion."""

from dataclasses import asdict, dataclass
import hashlib
import json


@dataclass(frozen=True)
class Criterion:
    id: str
    description: str
    evidence: tuple[str, ...]


DELIVERY = (
    Criterion(
        "A6-2.development",
        "Two dependent real-code stories with pinned tests",
        ("plan", "code", "tests"),
    ),
    Criterion(
        "A6-2.review-repair",
        "Independent required correction, repair and exact-head approval",
        ("reviews", "pull_request", "checks"),
    ),
    Criterion(
        "A6-2.merge-deploy",
        "Reviewed merge and verified current runtime",
        ("pull_request", "deployment", "runtime"),
    ),
    Criterion(
        "A6-2.parity",
        "Current UI, preview UI and API agree for the same actor and release",
        ("current_ui", "preview_ui", "api"),
    ),
    Criterion(
        "A6-2.dependency",
        "Successor starts only after evaluation and intended human gates",
        ("graph", "executions", "decisions"),
    ),
)
FAULTS = (
    Criterion(
        "A6-3.wait-exit",
        "Normal worker exit while waiting preserves continuation",
        ("injection", "before", "after"),
    ),
    Criterion(
        "A6-3.worker-loss",
        "Disposable worker loss preserves continuation",
        ("injection", "before", "after"),
    ),
    Criterion(
        "A6-3.tick-restart",
        "Isolated engine restart preserves durable continuation",
        ("injection", "before", "after"),
    ),
    Criterion(
        "A6-3.missed-wakeup",
        "Missed wake-up is recovered by durable scheduling",
        ("injection", "before", "after"),
    ),
    Criterion(
        "A6-3.duplicate-events",
        "Duplicate events create no duplicate mutating owner",
        ("injection", "before", "after"),
    ),
    Criterion(
        "A6-3.out-of-order",
        "Out-of-order events cannot regress accepted state",
        ("injection", "before", "after"),
    ),
    Criterion(
        "A6-3.competing-launches",
        "Competing launch paths retain one mutating owner",
        ("injection", "before", "after"),
    ),
    Criterion(
        "A6-3.timeout-after-success",
        "Unknown transport outcome reconciles remote success without repost",
        ("injection", "remote", "after"),
    ),
    Criterion(
        "A6-3.failed-ci",
        "Failed required CI blocks merge",
        ("injection", "checks", "after"),
    ),
    Criterion(
        "A6-3.stale-image",
        "Stale runtime cannot satisfy deployment acceptance",
        ("injection", "runtime", "after"),
    ),
    Criterion(
        "A6-3.failed-deploy",
        "Failed deployment cannot satisfy acceptance",
        ("injection", "deployment", "after"),
    ),
)
CONTROLS = (
    Criterion(
        "A6-4.tenant",
        "Authenticated foreign tenant is denied at the service boundary",
        ("owner", "foreign"),
    ),
    Criterion(
        "A6-4.revocation",
        "Revoked authority prevents subsequent effects",
        ("injection", "before", "after"),
    ),
    Criterion(
        "A6-4.fanout-budget",
        "Fan-out shares the original allowance",
        ("policy", "cost", "after"),
    ),
    Criterion(
        "A6-4.repair-budget",
        "Repairs share the original allowance",
        ("policy", "cost", "after"),
    ),
    Criterion(
        "A6-4.halt-stop",
        "Graph halt and proven worker termination are separately observed",
        ("injection", "graph", "worker"),
    ),
    Criterion(
        "A6-4.human-refusal",
        "A real human refusal keeps successors blocked",
        ("decisions", "graph", "after"),
    ),
)
ACCOUNTING = (
    Criterion(
        "A6-5.isolation",
        "This run has its own inventory and accounts for cleanup",
        ("inventory",),
    ),
    Criterion(
        "A6-6.interventions",
        "All interventions recorded; zero coordinator re-triggers",
        ("interventions",),
    ),
)
CRITERIA = DELIVERY + FAULTS + CONTROLS + ACCOUNTING


def definition():
    return {
        "version": 1,
        "criteria": [asdict(c) for c in CRITERIA],
        "stories": STORIES,
        "tests": TESTS,
        "chain": ["first", "verify", "release", "second"],
        "planned_gates": ["release", "refusal"],
    }


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# These instructions and tests are pinned before issue creation. The review
# deliberately requires an attributable finding; the harness never posts an
# approval on a developer's behalf or counts its own test as independent review.
STORIES = (
    "Implement integer basis-point discounts in qualification/{qualification_id}/pricing.py. "
    "Expose discounted_cents(cents, basis_points), validate cents >= 0 and 0 <= basis_points <= 10000, "
    "and round half up using integer arithmetic. Add unittest coverage including (5,1000)->5, "
    "(15,1000)->14, (100,10000)->0 and invalid inputs. Required independent review: verify the "
    "half-cent boundary. A real required correction and repair must be observed before this "
    "qualification can pass; do not fabricate a finding, review, or result. Deploy through the accepted policy.",
    "After the predecessor's deployed evaluation and human gate pass, add quote_total(items, basis_points) "
    "in qualification/{qualification_id}/quote.py, using pricing.discounted_cents on each item. "
    "Add unittest coverage: [5,15] at 1000 basis points -> 19; [] -> 0. "
    "Use the existing review, merge, deployment and evaluation path; do not restart a coordinator.",
)

TESTS = (
    "import unittest\nfrom pricing import discounted_cents\n\n"
    "class PricingTest(unittest.TestCase):\n"
    "    def test_half_up(self):\n        self.assertEqual(discounted_cents(5, 1000), 5)\n"
    "        self.assertEqual(discounted_cents(15, 1000), 14)\n"
    "    def test_full_discount(self):\n        self.assertEqual(discounted_cents(100, 10000), 0)\n"
    "    def test_invalid(self):\n        for args in [(-1, 1), (1, -1), (1, 10001)]:\n"
    "            with self.assertRaises(ValueError):\n                discounted_cents(*args)\n",
    "import unittest\nfrom quote import quote_total\n\n"
    "class QuoteTest(unittest.TestCase):\n"
    "    def test_per_item_rounding(self):\n        self.assertEqual(quote_total([5, 15], 1000), 19)\n"
    "    def test_empty(self):\n        self.assertEqual(quote_total([], 1000), 0)\n",
)

DEFINITION_HASH = digest(definition())
