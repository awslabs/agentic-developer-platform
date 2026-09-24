"""The shared onboarding contract must describe the app, not a past version of it.

Issue #5535, EPIC #4910. The contract is consumed by #5730 (onboarding UI/CLI).

A checked-in document describing an API is a second source of truth, and the
failure mode is silent: the document keeps describing the old shape, the client
keeps failing against the new one, and nothing fails in between to say which is
right. So the document is generated from the app's own OpenAPI schema and
authorization inventory, and this file fails when the two disagree.

The zero-workspace tests are here rather than beside the workspace routes on
purpose: they are the *contract's* central claim — a control plane must start with
no workspaces — and a client team reading the contract needs to know that claim is
enforced somewhere.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parents[1]
CONTRACT = COMPONENT / "docs" / "onboarding-api-contract.md"
GENERATOR = COMPONENT / "scripts" / "generate-onboarding-contract.py"


def _generator():
    """Load the generator by path; `scripts/` is not an importable package."""
    spec = importlib.util.spec_from_file_location("_onboarding_generator", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheContractIsCurrent:
    def test_mounted_routes_cannot_shadow_another_authority_handler(self):
        """OpenAPI hides duplicate routes while requests use the first handler.

        A merge once restored the legacy capability handler beside the governed
        onboarding handler. Check the actual mounted routes, not OpenAPI's map.
        """
        from collections import Counter

        from fastapi.routing import APIRoute

        from app.main import app

        routes = Counter(
            (method, route.path)
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        )
        assert {key: count for key, count in routes.items() if count > 1} == {}

    def test_it_is_checked_in(self):
        """#5730 builds against this file, so it must exist in the tree."""
        assert CONTRACT.exists(), (
            "the onboarding contract is missing; run "
            "scripts/generate-onboarding-contract.py"
        )

    def test_it_matches_what_the_application_implies(self):
        """The drift guard.

        Fails when a route, permission, request model or status code changes without
        the contract being regenerated — which is exactly when a client built against
        the document would start failing for reasons the document cannot explain.
        """
        assert CONTRACT.read_text() == _generator().render(), (
            "the onboarding contract is stale; regenerate it with "
            "python scripts/generate-onboarding-contract.py"
        )

    def test_it_is_marked_generated(self):
        """A hand-edit is a drift source; the file must say not to make one."""
        assert "DO NOT EDIT BY HAND" in CONTRACT.read_text()


class TestTheContractStatesWhatClientsGetWrong:
    """The distinctions a client cannot infer from a schema."""

    def test_it_separates_refusal_from_unavailability(self):
        text = CONTRACT.read_text()
        assert "`403`" in text and "`503`" in text
        assert "must not be collapsed" in text

    def test_it_says_an_empty_workspace_list_is_a_success(self):
        assert "empty list" in CONTRACT.read_text()

    def test_it_says_unknown_is_not_a_failure(self):
        """A client rendering `unknown` as failure tells a user their workspace is gone.

        It may be running and billable. This is the same three-valued distinction
        `app/services/provisioning.py` keeps between terminal and inconclusive states.
        """
        text = CONTRACT.read_text()
        assert "`unknown` is **not** a failure" in text

    def test_it_says_request_fields_cannot_confer_authority(self):
        text = CONTRACT.read_text()
        assert "never confer authority" in text
        assert "server-side" in text

    def test_it_says_a_201_is_acceptance_rather_than_completion(self):
        """Provisioning is asynchronous; treating `201` as done is the old defect.

        `app/services/provisioning.py` records that the deleted GitHub-dispatch path
        returned `201 Created` with `status=Provisioning` while nothing was
        provisioning it. A client that reads `201` as completion reproduces that.
        """
        assert "not that infrastructure exists" in CONTRACT.read_text()


class TestTheZeroWorkspaceOrdering:
    """The control-plane-first claim, enforced rather than only documented."""

    def test_listing_workspaces_is_organization_scoped(self):
        """It must not require a workspace grant, or a fresh install cannot list.

        This is the ordering the contract asserts: organization-administrator
        authority exists before any workspace grant does.
        """
        from app.endpoint_inventory import Scope, classify

        _, (scope, _permission) = classify("GET", "/workspaces")
        assert scope is Scope.ORGANIZATION

    def test_creating_the_first_workspace_is_organization_scoped(self):
        """A workspace-scoped create could never create the *first* workspace.

        It would require a grant on the thing being created, which cannot exist
        yet — a prerequisite loop that makes onboarding impossible.
        """
        from app.endpoint_inventory import Scope, classify

        _, (scope, _permission) = classify("POST", "/workspaces")
        assert scope is Scope.ORGANIZATION

    def test_resolving_the_caller_organization_is_organization_scoped(self):
        from app.endpoint_inventory import Scope, classify

        _, (scope, _permission) = classify("GET", "/orgs/current")
        assert scope is Scope.ORGANIZATION

    @pytest.mark.parametrize(
        ("method", "path"),
        [("POST", "/auth/login"), ("POST", "/auth/signup")],
    )
    def test_the_routes_that_establish_a_credential_require_none(self, method, path):
        """A login that required authentication would make onboarding unreachable."""
        from app.endpoint_inventory import RouteClass, classify

        route_class, _ = classify(method, path)
        assert route_class is RouteClass.PUBLIC

    def test_every_documented_action_is_inventoried(self):
        """A contract row with no recorded authorization decision is a hole.

        `app/domain_guard.py` refuses a route absent from the inventory, so a
        documented action that is not inventoried would 500 rather than authorize —
        and the contract would be advertising an endpoint no client can call.
        """
        from app.endpoint_inventory import classify

        for method, path, _purpose in _generator().ONBOARDING_ACTIONS:
            classify(method, path)  # raises RouteNotInventoried if absent

    def test_every_documented_action_exists_in_the_served_schema(self):
        """A path that drifted would document an endpoint that 404s."""
        from app.main import app

        specification = app.openapi()
        for method, path, _purpose in _generator().ONBOARDING_ACTIONS:
            assert path in specification["paths"], f"{path} is no longer served"
            assert method.lower() in specification["paths"][path], (
                f"{method} {path} is no longer served"
            )
