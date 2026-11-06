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


def fixture_test(index, qualification_id, *, product=False):
    tests = PRODUCT_TESTS if product else TESTS
    return tests[index].format(qualification_id=qualification_id)


def fixture_story(index, qualification_id, *, product=False):
    stories = PRODUCT_STORIES if product else STORIES
    return stories[index].format(qualification_id=qualification_id)


def fixture_test_path(index, qualification_id, *, product=False):
    if product:
        suffix = "pricing" if index == 0 else "quote"
        return (
            f"tests/test_qualification_{qualification_id.replace('-', '_')}_{suffix}.py"
        )
    name = "test_pricing.py" if index == 0 else "test_quote.py"
    return f"modules/gateway/tests/qualification/{qualification_id}/{name}"


def fixture_source_paths(index, qualification_id, *, product=False):
    prefix = (
        ("qualification-api/" if index == 0 else "qualification-client/")
        if product
        else "modules/gateway/src/"
    )
    root = f"{prefix}qualification/{qualification_id}/"
    if product:
        if index == 0:
            return (root + "pricing.py", "app.py")
        return (
            root + "pricing.py",
            root + "quote.py",
            "client.py",
        )
    return (root + ("pricing.py" if index == 0 else "quote.py"),)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# These instructions and tests are pinned before issue creation. The review
# deliberately requires an attributable finding; the harness never posts an
# approval on a developer's behalf or counts its own test as independent review.
STORIES = (
    "Implement integer basis-point discounts in modules/gateway/src/qualification/{qualification_id}/pricing.py. "
    "Expose discounted_cents(cents, basis_points), validate cents >= 0 and 0 <= basis_points <= 10000, "
    "and round half up using integer arithmetic. Add unittest coverage including (5,1000)->5, "
    "(15,1000)->14, (100,10000)->0 and invalid inputs. Required independent review: verify the "
    "half-cent boundary. For the predeclared failed-CI/review fixture, the first PR head must "
    "use Python round(cents * (10000 - basis_points) / 10000), with the pinned tests unchanged. "
    "Keep that deliberately defective head unmerged until the independent reviewer records "
    "the rounding finding and the repair replaces it with integer half-up arithmetic. A real required correction and repair must be observed before this "
    "qualification can pass; do not fabricate a finding, review, or result. Deploy through the accepted policy.",
    "After the predecessor's deployed evaluation and human gate pass, add quote_total(items, basis_points) "
    "in modules/gateway/src/qualification/{qualification_id}/quote.py, using a relative import of pricing.discounted_cents on each item. "
    "Add unittest coverage: [5,15] at 1000 basis points -> 19; [] -> 0. "
    "Use the existing review, merge, deployment and evaluation path; do not restart a coordinator.",
)

TESTS = (
    "import unittest\nfrom importlib import import_module\n"
    "discounted_cents = import_module('src.qualification.{qualification_id}.pricing').discounted_cents\n\n"
    "class PricingTest(unittest.TestCase):\n"
    "    def test_half_up(self):\n        self.assertEqual(discounted_cents(5, 1000), 5)\n"
    "        self.assertEqual(discounted_cents(15, 1000), 14)\n"
    "    def test_full_discount(self):\n        self.assertEqual(discounted_cents(100, 10000), 0)\n"
    "    def test_invalid(self):\n        for args in [(-1, 1), (1, -1), (1, 10001)]:\n"
    "            with self.assertRaises(ValueError):\n                discounted_cents(*args)\n",
    "import unittest\nfrom importlib import import_module\n"
    "quote_total = import_module('src.qualification.{qualification_id}.quote').quote_total\n\n"
    "class QuoteTest(unittest.TestCase):\n"
    "    def test_per_item_rounding(self):\n        self.assertEqual(quote_total([5, 15], 1000), 19)\n"
    "    def test_empty(self):\n        self.assertEqual(quote_total([], 1000), 0)\n",
)

DEFINITION_HASH = digest(definition())

PRODUCT_STORIES = (
    "In this API repository, implement integer half-up basis-point discounts at "
    "qualification-api/qualification/{qualification_id}/pricing.py. Expose discounted_cents(cents, basis_points), "
    "reject cents < 0 or basis_points outside 0..10000, and implement with Python's standard library. "
    "Wire that module into the existing root app.py without changing its serving command. "
    "Add qualification-api/qualification/__init__.py and "
    "qualification-api/qualification/{qualification_id}/__init__.py. "
    "Extend the existing qualification-api/Dockerfile, retaining "
    "its dependencies, COPY app.py into /app, working directory and startup command. "
    "Add COPY qualification-api/qualification /app/qualification-api/qualification "
    "and ENV PYTHONPATH=/app/qualification-api so the root entrypoint imports the package. "
    "Keep the existing qualification-api/build.py and .github/workflows/qualification-api-deploy.yml: "
    "they already build from repository root with -f qualification-api/Dockerfile. "
    "This packaging change needs no workflow rewrite. Preserve permissions, gates and receipt handling. "
    "The changed source still requires reviewed artifact source pins before dispatch; never bypass that check. "
    "The initial PR head must deliberately use round(cents * (10000 - basis_points) / 10000) "
    "and fail the pinned half-cent test. Keep it unmerged until an independent reviewer identifies "
    "the defect and the repair uses integer half-up arithmetic. Run the pinned test from this "
    "repository root with python -m unittest discover -s tests -v, and ensure required CI collects it. "
    "Keep the reviewed source-pin release manifest; do not claim an invented review or deployment.",
    "After the predecessor's deployed evaluation and human release gate, implement "
    "qualification-client/qualification/{qualification_id}/pricing.py and "
    "qualification-client/qualification/{qualification_id}/quote.py "
    "in this CLIENT repository. Implement discounted_cents with integer half-up arithmetic "
    "and invalid-input checks locally; quote_total(items, basis_points) must import it "
    "relatively from .pricing and apply it to each item. Wire it into "
    "the existing root client.py in the completed Job, retaining python client.py and its arguments. "
    "Add qualification-client/qualification/__init__.py and "
    "qualification-client/qualification/{qualification_id}/__init__.py. "
    "Extend the existing qualification-client/Dockerfile, retaining "
    "its dependencies, COPY client.py into /app, working directory and startup command. "
    "Add COPY qualification-client/qualification /app/qualification-client/qualification "
    "and ENV PYTHONPATH=/app/qualification-client. Keep the existing qualification-client/build.py "
    "and .github/workflows/qualification-client-deploy.yml: they already build from repository root "
    "with -f qualification-client/Dockerfile. This packaging change needs no workflow rewrite. "
    "Preserve permissions, gates and receipt handling. The changed source still requires reviewed "
    "artifact source pins before dispatch; never bypass that check. "
    "The API repository's module is not "
    "available here. Run the pinned test with python -m unittest discover -s tests -v "
    "from this repository root; required CI must collect it. Retain the reviewed "
    "source-pin manifest and existing review, merge and deployment gates.",
)
PRODUCT_TESTS = (
    "import sys\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qualification-api'))\n"
    + TESTS[0].replace("src.qualification.", "qualification."),
    "import sys\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qualification-client'))\n"
    + (TESTS[0] + "\n" + TESTS[1]).replace("src.qualification.", "qualification."),
)
