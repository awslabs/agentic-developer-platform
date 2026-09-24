#!/usr/bin/env python3
"""Emit the shared onboarding API contract from this app's real schemas.

Issue #5535 (Superplane W6), EPIC #4910. Consumed by #5730 (onboarding UI/CLI).

WHY THIS IS GENERATED RATHER THAN WRITTEN
-----------------------------------------
#5730 builds a UI and a CLI against these endpoints and cannot invent routes,
request models or error shapes. A hand-written contract is a second source of
truth that drifts the first time a route changes, and the drift is silent — the
document keeps describing the old shape and the client keeps failing against the
new one, with nothing failing in between to say which is right.

So every column here is read from the running application:

* method, path, request/response model names and status codes come from
  ``app.main.app.openapi()`` — the same schema FastAPI serves at ``/openapi.json``;
* the permission and scope come from ``app.endpoint_inventory.classify()``, which
  is the table the app-wide guard actually enforces (``app/domain_guard.py``) and
  which ``tests/test_auth.py`` already requires to cover every mounted route.

``tests/test_onboarding_contract.py`` regenerates and compares, so a route,
permission or model that changes without the document being regenerated fails CI
rather than shipping a contract that lies.

USAGE
-----
    python scripts/generate-onboarding-contract.py            # write the document
    python scripts/generate-onboarding-contract.py --check    # exit 1 if stale
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# The app refuses to import without these. Set before importing so this script can
# run in a bare checkout: both are non-secret placeholders and nothing here opens a
# connection or signs a token. `setdefault`, so a real environment wins.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/contract-render")
os.environ.setdefault("JWT_SECRET_KEY", "contract-render-only-not-a-credential")

REPO_RELATIVE_OUTPUT = "docs/onboarding-api-contract.md"

# The onboarding journey, in the order a new installation performs it. Ordered
# deliberately: the sequence is itself part of the contract, because it is what
# makes a zero-workspace start possible. Organization-administrator authority
# exists before any workspace grant does, so steps 1-3 must not require a
# workspace, and a client that assumes "fetch a workspace, then render" cannot
# onboard anybody.
ONBOARDING_ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("POST", "/auth/login", "Establish a session. Cannot require a credential."),
    ("GET", "/orgs/current", "Resolve the caller's organization, server-side."),
    ("GET", "/workspaces", "List workspaces. MUST succeed with zero workspaces."),
    ("POST", "/workspaces", "Create the first workspace."),
    ("GET", "/workspaces/{workspace_id}", "Read one workspace's state."),
    ("DELETE", "/workspaces/{workspace_id}", "Tear a workspace down."),
    ("GET", "/users", "List organization members."),
    ("POST", "/users/invite", "Invite a member."),
    ("GET", "/accounts", "List registered provider accounts."),
    ("POST", "/accounts", "Register a provider account."),
    ("GET", "/vault/credentials", "List credential references (never values)."),
    ("POST", "/vault/credentials", "Register a credential reference."),
    (
        "POST",
        "/workspaces/{workspace_id}/provider-connections",
        "Attach a provider connection to a workspace.",
    ),
)

PREAMBLE = """<!-- GENERATED FILE — DO NOT EDIT BY HAND.
     Regenerate: python scripts/generate-onboarding-contract.py
     Source of truth: app.main.app.openapi() + app.endpoint_inventory.classify()
     Verified by: tests/test_onboarding_contract.py -->

# Superplane onboarding API contract

Issue #5535, EPIC #4910. Shared with **#5730** (onboarding UI and CLI).

Every row below is read from this service's own OpenAPI schema and from
`app/endpoint_inventory.py`, the table the app-wide authorization guard actually
enforces. Nothing here is hand-maintained, so it cannot drift from the app
without `tests/test_onboarding_contract.py` failing.

## The ordering that makes a zero-workspace start possible

A control plane must come up with **no workspaces registered at all**, let a
verified organization administrator sign in, and list an empty set. Therefore:

1. Organization-administrator authority exists **before** any workspace grant
   does. Steps 1-3 in the table resolve entirely from organization scope.
2. `GET /workspaces` returning `[]` is a **success**, not an error state. A client
   that treats an empty list as a failure, or that fetches a workspace before
   rendering, cannot onboard anybody.
3. Creating the first workspace therefore cannot require a ready workspace as a
   prerequisite. `POST /workspaces` is `organization`-scoped for this reason.

## How authority is resolved

The caller's organization and principal are resolved **server-side** from the
authenticated ADP identity plus that identity's current grants. Request fields
never confer authority: a body key that asserts an identity
(`org_id`, `workspace_id`, `principal`, `role`, ... — plus the `x-`, `adp_`,
`auth_` and `caller_` prefixes) is **refused**, and it is refused even when its
value matches the caller's real organization. See
`app/services/provisioning.py:FORBIDDEN_PARAMETER_KEYS`.

## Error mapping

| Status | Meaning | Client action |
|--------|---------|---------------|
| `401` | No or invalid authentication. | Re-authenticate. |
| `403` | Authenticated, but **refused** — a grant is missing, or evidence could not be established. | Do not retry; the caller lacks authority. |
| `404` | Not found, or not visible to this caller. | Do not retry. |
| `409` | Conflict with existing state. | Reconcile, then retry. |
| `422` | Request failed validation. The rejected **value is never echoed** (`app/main.py` scrubs it), so the body names the field, not the input. | Fix the request. |
| `429` | Rate or quota limit. | Back off and retry. |
| `503` | **Unavailable** — a required integration is not configured or not reachable. | Retry; surface as a platform fault, not as the user's error. |

`403` and `503` are deliberately distinct and must not be collapsed by a client.
A refusal is an authorization answer; unavailability is an outage. Rendering one
as the other sends an operator to debug permissions on a healthy system, or to
debug an outage that is really a missing grant.

## Receipts

A mutating onboarding action returns the created or updated resource, carrying
the identifier a client must persist to follow the work. Provisioning is
**asynchronous**: `POST /workspaces` returns `201` with the workspace record, and
that `201` means *the operation was accepted*, not that infrastructure exists.
A client must poll `GET /workspaces/{workspace_id}` for state rather than
treating the `201` as completion.

Note the state vocabulary is three-valued, not two: an operation may be
`succeeded`, `failed`/`cancelled`, or **inconclusive** (`pending`, `running`,
`unknown`). `unknown` is **not** a failure — a client that renders it as one
tells a user their workspace is gone when it may be running and billable.

## The onboarding surface

"""


def _model(operation: dict, key: str) -> str:
    if key == "request":
        schema = (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
    else:
        schema = {}
    reference = schema.get("$ref", "")
    return f"`{reference.rsplit('/', 1)[-1]}`" if reference else "—"


def _success_model(operation: dict) -> str:
    for code, body in sorted(operation.get("responses", {}).items()):
        if not code.startswith("2"):
            continue
        schema = (
            body.get("content", {}).get("application/json", {}).get("schema", {})
        )
        reference = schema.get("$ref", "")
        if reference:
            return f"`{reference.rsplit('/', 1)[-1]}`"
        if schema.get("type") == "array":
            item = schema.get("items", {}).get("$ref", "")
            if item:
                return f"`{item.rsplit('/', 1)[-1]}[]`"
        return "inline"
    return "—"


def render() -> str:
    from app.endpoint_inventory import RouteClass, classify
    from app.main import app

    specification = app.openapi()
    rows: list[str] = [
        "| # | Method | Path | Scope | Permission | Request | Success | Codes |",
        "|---|--------|------|-------|------------|---------|---------|-------|",
    ]
    notes: list[str] = []

    for index, (method, path, purpose) in enumerate(ONBOARDING_ACTIONS, start=1):
        operation = specification["paths"][path][method.lower()]
        route_class, detail = classify(method, path)

        if route_class is RouteClass.DOMAIN:
            scope, permission = detail
            scope_cell = f"`{scope.value}`"
            permission_cell = f"`{permission.value}`"
        else:
            scope_cell = f"_{route_class.value}_"
            permission_cell = "—"

        codes = ", ".join(f"`{code}`" for code in sorted(operation["responses"]))
        rows.append(
            f"| {index} | `{method}` | `{path}` | {scope_cell} | {permission_cell} "
            f"| {_model(operation, 'request')} | {_success_model(operation)} "
            f"| {codes} |"
        )
        notes.append(f"{index}. `{method} {path}` — {purpose}")

    document = [PREAMBLE, "\n".join(rows), "", "### What each step is for", ""]
    document.append("\n".join(notes))
    document.append(
        "\n> The `Codes` column lists only what the schema declares. Every "
        "authenticated route can additionally answer `401`, `403` and `429` "
        "through the app-wide guard and the rate limiter, which are middleware "
        "and so are not enumerated per route in the schema.\n"
    )
    return "\n".join(document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the checked-in document differs from what the app implies",
    )
    arguments = parser.parse_args(argv)

    component = Path(__file__).resolve().parents[1]
    output = component / REPO_RELATIVE_OUTPUT
    rendered = render()

    if arguments.check:
        if not output.exists():
            print(f"{output} does not exist; run this script without --check")
            return 1
        if output.read_text() != rendered:
            print(
                f"{output} is stale. Regenerate: "
                "python scripts/generate-onboarding-contract.py"
            )
            return 1
        print(f"{output} matches the application's schemas")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
