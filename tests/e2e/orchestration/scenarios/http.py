"""Bounded authenticated HTTP. No ambient GitHub credential or mutation retry."""

import json
import urllib.error
import urllib.request
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
