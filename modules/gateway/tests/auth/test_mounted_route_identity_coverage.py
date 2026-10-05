"""Mounted-route identity coverage for /internal/* — S10 continuation (#5972).

Builds on the Appendix B route-mount probe from S10-revalidation.md (PR #5890).
Constructs the real FastAPI application, walks every ``/internal/*`` route's
dependency tree, and classifies each route by its authentication mechanism.

The test fails when:
- A new ``/internal/*`` route is added without any authentication dependency
  and is not in the reviewed exceptions list.
- The total count of internal routes drops (removal without deliberate intent).
- The reviewed exceptions list grows without an update to this test.

No cloud credentials or live requests are needed. The app is constructed but
never entered into its lifespan or sent traffic.

Issue #5609 (S10 parent), PR #5890 evidence, issue #5972 continuation.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute

from src.agentauth.routes import require_agent_transport
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.persona_model_probe_routes import verify_model_probe_irsa

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def dependency_calls(dependant) -> set[object]:
    """Walk a FastAPI Dependant tree and return actual dependency callables."""
    names: set[object] = set()
    seen: set[int] = set()
    pending = [dependant]
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        names.add(item.call)
        pending.extend(item.dependencies)
    return names


# Guards that are equivalent to verify_internal_or_irsa (reviewed wrappers).
# Adding a new wrapper here requires reviewing that it calls the canonical guard
# on every code path. This is an explicit allowlist, not a wildcard.
REVIEWED_WRAPPERS: frozenset[object] = frozenset(
    {
        require_agent_transport,
        verify_model_probe_irsa,
    }
)

# Routes that use independent cryptographic proof, body-level authentication,
# or are intentionally public. Each entry is (methods_frozenset, path).
# Extending this list is a deliberate decision that must come with a review
# of the independent mechanism.
#
# Routes are mounted under /internal/v1/ in the real app because the router
# prefix is /internal/v1 and sub-routers add further prefixes.
REVIEWED_EXCEPTIONS: frozenset[tuple[frozenset[str], str]] = frozenset(
    {
        # knowledge_assets_internal.py: signed asset/tenant/attempt grant,
        # verified against the stored grant digest and current attempt before writes.
        (frozenset({"POST"}), "/internal/v1/knowledge-assets/status-callback"),
        # --- Agent routes: STS GetCallerIdentity SigV4 proof (/agent/ subrouter) ---
        # work_routes.py: STS SigV4 proof + signed invocation header + role allowlist
        (frozenset({"POST"}), "/internal/v1/agent/work/admit"),
        (frozenset({"POST"}), "/internal/v1/agent/roots/admit"),
        (frozenset({"POST"}), "/internal/v1/agent/chat/data/admit"),
        # persona_model_selection.py: STS SigV4 proof via require_agent_transport
        (frozenset({"POST"}), "/internal/v1/agent/persona-model/resolve"),
        # chat_model.py: Kubernetes TokenReview pod identity + live grant + bound dispatch
        (frozenset({"POST"}), "/internal/v1/agent/chat/model-decision"),
        # arc_model.py: GitHub OIDC claims -> registered workflow + human owner
        (frozenset({"POST"}), "/internal/v1/agent/arc/model-decision"),
        # cyber_jobs.py: GitHub OIDC claims -> registered workflow + human owner
        (frozenset({"POST"}), "/internal/v1/agent/arc/cyber/jobs"),
        (frozenset({"POST"}), "/internal/v1/agent/arc/cyber/result"),
        # model_policy_keys.py: legacy-chat-preflight uses provenance helper
        (frozenset({"POST"}), "/internal/v1/agent/legacy-chat-preflight"),
        # model_policy_keys.py: intentionally public — publishes only the public
        # half of a signing key; no secret material in the response
        (frozenset({"GET"}), "/internal/v1/agent/model-policy-keys"),
        # --- Task routes: STS SigV4 producer proof (verify_producer in body) ---
        # task_admission_routes.py: require_agent_transport + verify_producer + caller authz
        (frozenset({"POST"}), "/internal/v1/tasks/admit"),
        # task_dispatch_routes.py: require_adapters_enabled (Depends) + verify_producer in body
        (frozenset({"POST"}), "/internal/v1/tasks/dispatch/claim"),
        (frozenset({"POST"}), "/internal/v1/tasks/dispatch/settle"),
        # task_dispatch_routes.py: recovery endpoints, same pattern
        (frozenset({"POST"}), "/internal/v1/tasks/recovery/claim"),
        (frozenset({"POST"}), "/internal/v1/tasks/recovery/settle"),
    }
)


def _classify_route(route: APIRoute) -> str:
    """Classify an internal route by its authentication mechanism."""
    names = dependency_calls(route.dependant)
    if verify_internal_or_irsa in names:
        return "direct"
    if names & REVIEWED_WRAPPERS:
        return "wrapper"
    methods = frozenset(route.methods) if route.methods else frozenset()
    if (methods, route.path) in REVIEWED_EXCEPTIONS:
        return "exception"
    return "unguarded"


# ---------------------------------------------------------------------------
# App construction (cached per module to avoid repeated startup cost)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def internal_routes():
    """All /internal/* APIRoutes from the real application.

    The app is constructed with placeholder env vars so that import-time
    configuration reads succeed. No lifespan or requests are executed.
    """
    env_patches = {
        "BG_TOKEN_SECRET_KEY": "test-placeholder-key-for-route-probe",
        "BG_INTERNAL_API_KEY": "test-placeholder-key-for-route-probe",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    with patch.dict(os.environ, env_patches):
        from src.app import create_app

        app = create_app()

    routes = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path.startswith("/internal/"):
            routes.append(route)
    return routes


# ---------------------------------------------------------------------------
# Coverage tests
# ---------------------------------------------------------------------------


class TestMountedRouteInventory:
    """The inventory reproduces and pins the route count from the S10 probe."""

    def test_internal_route_count_at_least_78(self, internal_routes):
        """The S10 revalidation counted 78 /internal/* routes. New routes may be
        added but the count must not drop — that would indicate removed coverage."""
        assert len(internal_routes) >= 78, (
            f"Expected at least 78 /internal/* routes (S10 baseline), found {len(internal_routes)}. "
            f"If routes were intentionally removed, update this bound."
        )

    def test_every_internal_route_has_reviewed_authentication_classification(self, internal_routes):
        """Every /internal/* route must have a reviewed authentication classification.

        Routes are classified as: direct (verify_internal_or_irsa), wrapper
        (reviewed wrapper that calls it), exception (independent crypto proof
        or intentionally public), or unguarded (a finding).
        """
        unguarded = []
        for route in internal_routes:
            if _classify_route(route) == "unguarded":
                methods = ",".join(sorted(route.methods)) if route.methods else "?"
                unguarded.append(f"{methods} {route.path}")

        assert unguarded == [], (
            "Found /internal/* routes with no authentication dependency:\n"
            + "\n".join(f"  {u}" for u in unguarded)
            + "\nEach route must either depend on verify_internal_or_irsa, use a "
            "reviewed wrapper, or be added to REVIEWED_EXCEPTIONS with a review "
            "of its independent authentication mechanism."
        )

    def test_classification_counts_are_plausible(self, internal_routes):
        """Guard against a broken classifier that vacuously passes.

        If the classifier marks everything as 'exception' or if no routes are
        found at all, the authentication test passes trivially. This pins the
        expected shape: most routes should be direct or wrapper, and the
        exception count should match the pinned number.
        """
        from collections import Counter

        counts = Counter(_classify_route(r) for r in internal_routes)
        # At least some routes must be directly guarded
        assert counts["direct"] >= 30, f"Expected >= 30 direct, found {counts['direct']}"
        assert counts["wrapper"] >= 25, f"Expected >= 25 wrapper, found {counts['wrapper']}"
        assert counts["unguarded"] == 0


class TestReviewedExceptionsAreStable:
    """The exceptions list must not grow silently."""

    def test_exception_count_is_pinned(self):
        """S10 counted 9 exceptions (Appendix B §2). This continuation added 5
        task-api routes with independent STS SigV4 producer-proof authentication,
        and the asset/attempt-scoped signed ingestion callback brought the total to 15.
        Chat data admission adds body-bound STS producer proof, restricted by the
        registered chat producer, tenant and persona, for a total of 16. Updating this count requires reviewing the
        new route's authentication mechanism."""
        assert len(REVIEWED_EXCEPTIONS) == 16, (
            f"REVIEWED_EXCEPTIONS has {len(REVIEWED_EXCEPTIONS)} entries, expected 16. If a new exception was reviewed and added, update this count."
        )

    def test_every_exception_exists_in_the_app(self, internal_routes):
        """Every reviewed exception must correspond to a real mounted route.

        A stale exception entry hides drift — it makes the list look complete
        when a route has actually been removed or renamed.
        """
        mounted = {(frozenset(r.methods) if r.methods else frozenset(), r.path) for r in internal_routes}
        stale = REVIEWED_EXCEPTIONS - mounted
        assert stale == set(), "REVIEWED_EXCEPTIONS contains entries not mounted in the app:\n" + "\n".join(
            f"  {','.join(sorted(m))} {p}" for m, p in sorted(stale)
        )


# ---------------------------------------------------------------------------
# R4 residual: no non-constant-time secret comparison in source
# ---------------------------------------------------------------------------


class TestNoNonConstantTimeSecretComparison:
    """R4 from S10 revalidation: the unused duplicate _verify_internal_key that
    used != for secret comparison has been removed (#5972). This test ensures
    no such comparison is reintroduced anywhere in the gateway source.

    The check is AST-based, not regex, so it detects ``x != expected`` and
    ``x == expected`` regardless of formatting. The variable-name heuristic
    targets the specific secret names rather than flagging every != in the codebase.
    """

    @staticmethod
    def _gateway_python_files() -> list[Path]:
        """All .py files under modules/gateway/src/."""
        src = Path(__file__).resolve().parents[2] / "src"
        return sorted(src.rglob("*.py"))

    def test_no_plain_operator_comparison_of_internal_api_key(self):
        """No != or == comparison of internal_api_key / x_internal_api_key in source.

        The canonical comparison is hmac.compare_digest in auth_deps.py.
        Any plain operator comparison is a timing oracle reintroduction (R4).
        """
        suspicious: list[str] = []
        secret_names = {"internal_api_key", "x_internal_api_key"}

        for pyfile in self._gateway_python_files():
            try:
                tree = ast.parse(pyfile.read_text(), filename=str(pyfile))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                # Check if any comparator or the left side names a secret variable
                all_operands = [node.left, *node.comparators]
                names_in_compare = set()
                for operand in all_operands:
                    if isinstance(operand, ast.Name):
                        names_in_compare.add(operand.id)
                    elif isinstance(operand, ast.Attribute):
                        names_in_compare.add(operand.attr)

                if not (names_in_compare & secret_names):
                    continue

                # Only flag != and == (not `is`, `in`, etc.)
                for op in node.ops:
                    if isinstance(op, ast.NotEq | ast.Eq):
                        rel = pyfile.relative_to(pyfile.parents[2])
                        suspicious.append(f"{rel}:{node.lineno}")

        assert suspicious == [], (
            "Found non-constant-time comparison of internal API key secret:\n"
            + "\n".join(f"  {s}" for s in suspicious)
            + "\nUse hmac.compare_digest (see src/internal/auth_deps.py) instead."
        )

    def test_removed_duplicate_in_routes_py(self):
        """The unused _verify_internal_key in src/internal/routes.py is gone.

        It was the R4 residual from S10 revalidation: an unused function using !=
        for secret comparison. No mounted route called it, but its presence was
        a future reuse hazard.
        """
        routes_py = Path(__file__).resolve().parents[2] / "src" / "internal" / "routes.py"
        source = routes_py.read_text()
        assert "_verify_internal_key" not in source, (
            "src/internal/routes.py still contains _verify_internal_key. This unused duplicate was removed by #5972 (S10 R4 residual)."
        )
