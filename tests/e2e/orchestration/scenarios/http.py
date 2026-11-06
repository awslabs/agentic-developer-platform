"""Bounded authenticated HTTP. No ambient GitHub credential or mutation retry."""

import json
import urllib.error
import urllib.request
from urllib.parse import quote
import time
from datetime import UTC, datetime

from tests.e2e.orchestration.config import resolve_secret_ref, ResolvedConnection


class Unsupported(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Unsupported("credentialed redirects are refused")


class Client:
    live = True

    def __init__(self, config, manifest):
        self.config, self.manifest = config, manifest
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, *, actor="owner", body=None, github=False):
        if method != "GET" and time.monotonic() >= getattr(
            self, "deadline", float("inf")
        ):
            raise Unsupported("qualification mutation deadline reached")
        if not path.startswith("/") or path.startswith("//") or ".." in path:
            raise ValueError("invalid service path")
        name = "github" if github else actor
        reference = self.config.secret_refs.get(name)
        if not reference:
            raise Unsupported(f"scoped {name} credential reference unavailable")
        value = resolve_secret_ref(reference)
        origin = (
            "https://api.github.com"
            if github
            else self.manifest.api_origin.rstrip("/") + "/api"
        )
        headers = {
            "Authorization": "Bearer " + value,
            "Accept": "application/vnd.github+json" if github else "application/json",
        }
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            origin + path, data=data, method=method, headers=headers
        )
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as exc:
            # Never include request headers, secret value or unbounded error body.
            return exc.code, {"http_status": exc.code}
        with response:
            payload = response.read(8 * 1024 * 1024 + 1)
            if len(payload) > 8 * 1024 * 1024:
                raise ValueError("service evidence too large")
            return response.status, json.loads(payload) if payload else {}

    def get(self, path, *, actor="owner", github=False):
        status, data = self.request("GET", path, actor=actor, github=github)
        if status != 200:
            raise Unsupported(f"read {path} returned HTTP {status}")
        return data

    def pages(self, path, *, key=None):
        result = []
        for page in range(1, 11):
            data = self.get(
                path + ("&" if "?" in path else "?") + f"per_page=100&page={page}",
                github=True,
            )
            rows = data if key is None else data[key]
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise Unsupported("provider evidence pagination limit reached")

    def resolve_connection(self, connection_ref):
        actor = self.get("/auth/me")
        if (
            actor.get("org_id") != self.config.org_ref
            or actor.get("user_id") != self.config.identity_ref
        ):
            raise Unsupported("owner identity or tenant differs from approved fixture")
        rows = self.get("/auth/credentials")
        matches = [
            r
            for r in rows
            if r["id"] == connection_ref
            and r["service"] == "aws"
            and r["credential_type"] == "aws_role"
        ]
        if len(matches) != 1:
            return None
        row = matches[0]
        scopes = row.get("scopes") or {}
        connections = self.get(
            f"/admin/organizations/{self.config.org_ref}/connections/github"
        )["connections"]
        orgs = {r["github_org_login"] for r in connections if r["routable"]}
        if len(orgs) != 1:
            raise Unsupported(
                "repository organization connection is ambiguous or unroutable"
            )
        return ResolvedConnection(
            connection_ref=connection_ref,
            account_id=scopes.get("account_id", ""),
            org=next(iter(orgs)),
            active=scopes.get("status") == "verified"
            and (
                not row.get("expires_at")
                or datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
                > datetime.now(UTC)
            ),
        )

    def verify_product_epic(self):
        """Read the Product and all three provider targets before publication."""
        fixture = self.config.product_epic
        if fixture is None:
            return
        actor = self.get("/auth/me")
        if (actor.get("org_id"), actor.get("user_id")) != (
            self.config.org_ref,
            self.config.identity_ref,
        ):
            raise Unsupported(
                "Product owner identity or tenant differs from approved fixture"
            )
        cursor = None
        for _ in range(10):
            path = "/v1/products?limit=100"
            if cursor is not None:
                path += "&cursor=" + quote(cursor, safe="")
            try:
                page = self.get(path)
            except Unsupported:
                raise Unsupported(
                    "Product is missing or inaccessible to the selected owner"
                ) from None
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise Unsupported("Product access could not be verified")
            product = next(
                (row for row in page["items"] if row.get("id") == fixture.product_id),
                None,
            )
            if product is not None:
                if product.get("lifecycle_status") == "retired":
                    raise Unsupported(
                        "Product is retired; an accessible active Product is required"
                    )
                break
            cursor = page.get("next_cursor")
            if not cursor:
                raise Unsupported(
                    "Product is missing or inaccessible to the selected owner"
                )
        else:
            raise Unsupported(
                "Product access exceeds the bounded listing; narrow the owner scope"
            )

        connections = self.get("/admin/connections")
        if (connections.get("platform_verification") or {}).get(
            "app_permissions_match"
        ) is not True:
            raise Unsupported(
                "GitHub App issues-write permission cannot be verified for native linking"
            )
        targets = (fixture.epic, *fixture.stories)
        for target in targets:
            matches = [
                entry
                for entry in connections.get("connections", [])
                if entry.get("installation_id") == target.installation_id
                and (
                    (
                        entry.get("tenant_id") == self.config.org_ref
                        and entry.get("is_active_tenant") is True
                    )
                    or (
                        entry.get("tenant_id") is None
                        and entry.get("is_active_tenant") is None
                    )
                )
                and entry.get("revocation_pending") is False
            ]
            if (
                len(matches) != 1
                or (matches[0].get("verification") or {}).get("repositories_live")
                is not True
                or target.repository not in matches[0].get("repositories", [])
            ):
                raise Unsupported(
                    f"repository connection {target.connection_ref!r} cannot verify live access to {target.repository}"
                )
            repository = self.get("/repos/" + target.repository, github=True)
            if (
                repository.get("id") != target.repository_id
                or repository.get("full_name") != target.repository
            ):
                raise Unsupported(
                    f"repository target identity differs from reviewed config: {target.repository}"
                )
            if (
                repository.get("has_issues") is not True
                or repository.get("permissions", {}).get("push") is not True
            ):
                raise Unsupported(
                    f"issues-write access for native linking cannot be verified: {target.repository}"
                )
        epic = fixture.epic
        try:
            issue = self.get(
                f"/repos/{epic.repository}/issues/{fixture.epic_issue_number}",
                github=True,
            )
        except Unsupported:
            raise Unsupported(
                "planning epic issue is missing or inaccessible"
            ) from None
        if (
            issue.get("number") != fixture.epic_issue_number
            or type(issue.get("id")) is not int
            or "pull_request" in issue
        ):
            raise Unsupported(
                "planning epic issue is missing, inaccessible or not an issue"
            )
