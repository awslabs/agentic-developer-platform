"""Use only verified instance sessions for Product HTTP requests."""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

import common


class ProductResponse:
    def __init__(self, status, headers, body):
        common.require(
            len(body) <= 65536, "Product API response exceeds the evaluator limit"
        )
        self.status_code = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


# The reviewed gateway BASE URL this run is configured with already carries the
# external `/api` prefix CloudFront strips (`config.example.json` and the
# private pre-production bindings both publish `https://<host>/api`), and the
# product_onboarding routes are mounted at `/v1/products/...`. Every path below
# is therefore a mounted app path appended to the base unchanged, exactly like
# the `/api/admin/identity/...` and `/admin/organizations/...` paths
# `common.api` sends with the same base.
#
# Refusing the base path outright refused the configured target itself, and
# inserting a second `/api` on the way out addressed
# `<base>/api/api/v1/products`, which nothing serves. Segments must begin with
# an alphanumeric, so traversal, empty segments, percent-encoded separators and
# control characters in the base are still refused, together with a non-HTTPS
# scheme, embedded credentials, a query and a fragment.
BASE_PATH = re.compile(r"(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)*")


class ProductClient:
    def __init__(self, origin, token):
        parsed = urllib.parse.urlsplit(origin)
        common.require(
            parsed.scheme == "https"
            and parsed.netloc
            and not parsed.username
            and not parsed.password
            and BASE_PATH.fullmatch(parsed.path.rstrip("/")) is not None
            and not parsed.query
            and not parsed.fragment,
            "Product API requires the reviewed HTTPS Gateway origin",
        )
        self.origin = origin.rstrip("/")
        self.token = token

    def request(self, method, path, *, json_body=None, headers=None):
        detail = re.fullmatch(
            r"/v1/products/[A-Za-z0-9_-]+/projects/[A-Za-z0-9_-]+", path
        )
        history = re.fullmatch(
            r"/v1/products/[A-Za-z0-9_-]+/projects/[A-Za-z0-9_-]+/(?:changes|revisions/[1-9][0-9]*)",
            path,
        )
        common.require(
            (method == "POST" and path == "/v1/products" and json_body is not None)
            or (method == "PATCH" and detail is not None and json_body is not None)
            or (
                method == "GET"
                and json_body is None
                and (
                    path == "/v1/products" or detail is not None or history is not None
                )
            ),
            "Product driver requested a route outside its API scope",
        )
        common.require(
            isinstance(headers, dict) or headers is None,
            "Product driver supplied invalid request headers",
        )
        common.require(
            all(
                key.lower() in {"idempotency-key", "if-profile-revision"}
                and isinstance(value, str)
                and "\r" not in value
                and "\n" not in value
                for key, value in (headers or {}).items()
            ),
            "Product driver supplied headers outside its API scope",
        )
        request = urllib.request.Request(
            self.origin + path,
            data=json.dumps(json_body).encode() if json_body is not None else None,
            method=method,
            headers={
                **(headers or {}),
                "Authorization": "Bearer " + self.token,
                **(
                    {"Content-Type": "application/json"}
                    if json_body is not None
                    else {}
                ),
            },
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        try:
            with opener.open(request, timeout=60) as response:
                return ProductResponse(
                    response.status, response.headers, response.read(65537)
                )
        except urllib.error.HTTPError as error:
            return ProductResponse(error.code, error.headers, error.read(65537))
        except urllib.error.URLError:
            raise OSError("Product API response was not observed") from None

    def post(self, path, *, json, headers):
        return self.request("POST", path, json_body=json, headers=headers)

    def patch(self, path, *, json, headers):
        return self.request("PATCH", path, json_body=json, headers=headers)

    def get(self, path):
        return self.request("GET", path)


def local_owner(config, *, allow_foreign=False):
    work_dir = Path(config.get("work_dir", ""))
    actor_dir = work_dir / "product-actor"
    actor_file = actor_dir / common.SESSION_VAULT
    bootstrap_file = work_dir / common.SESSION_VAULT
    common.require(
        work_dir.is_absolute()
        and work_dir == work_dir.resolve()
        and work_dir.is_dir()
        and work_dir.stat().st_uid == os.getuid()
        and stat.S_IMODE(work_dir.stat().st_mode) == 0o700
        and config.get("session_ref") == str(bootstrap_file)
        and bootstrap_file.is_file()
        and not bootstrap_file.is_symlink()
        and bootstrap_file.stat().st_uid == os.getuid()
        and stat.S_IMODE(bootstrap_file.stat().st_mode) == 0o600
        and config.get("product_actor_session_ref") == str(actor_file)
        and actor_dir.is_dir()
        and not actor_dir.is_symlink()
        and actor_dir.stat().st_uid == os.getuid()
        and stat.S_IMODE(actor_dir.stat().st_mode) == 0o700
        and actor_file.is_file()
        and not actor_file.is_symlink()
        and actor_file.stat().st_uid == os.getuid()
        and stat.S_IMODE(actor_file.stat().st_mode) == 0o600,
        "Product actor needs its own private session, separate from the bootstrap administrator",
    )
    bootstrap = common.load_session(config)
    session = common.load_session(
        {**config, "work_dir": str(actor_dir), "session_ref": str(actor_file)}
    )
    run_id = config.get("evaluation_id")
    common.require(
        isinstance(run_id, str)
        and session.get("created_username")
        == f"{run_id}-product-admin@adp-eval.example"
        and isinstance(session.get("user_id"), str)
        and bool(session["user_id"])
        and isinstance(session.get("org_id"), str)
        and bool(session["org_id"])
        and session["user_id"] != bootstrap.get("user_id")
        and session["org_id"] != bootstrap.get("org_id")
        and session["access_token"] != bootstrap.get("access_token"),
        "Product owner must be a fresh, run-created instance actor",
    )
    status, attribution = common.api(
        config, "/auth/cli/admin-session", session["access_token"], expect=(200,)
    )
    common.require(
        status == 200
        and isinstance(attribution, dict)
        and attribution.get("verified") is True
        and attribution.get("role") == "platform_admin"
        and attribution.get("user_id") == session["user_id"]
        and attribution.get("org_id") == session["org_id"],
        "Product owner no longer matches the authenticated instance session",
    )
    status, workspace = common.api(
        config,
        f"/api/admin/identity/organizations/{urllib.parse.quote(session['org_id'], safe='')}",
        session["access_token"],
        expect=(200,),
    )
    common.require(
        status == 200
        and isinstance(workspace, dict)
        and workspace.get("id") == session["org_id"]
        and workspace.get("name") == f"eval-regression-{run_id}",
        "Product actor does not own this run's evaluation workspace",
    )
    client = ProductClient(config["gateway_url"], session["access_token"])
    foreign_client = None
    if allow_foreign:
        common.require(
            bootstrap.get("created_username") == f"{run_id}-admin@adp-eval.example"
            and isinstance(bootstrap.get("access_token"), str)
            and bool(bootstrap["access_token"])
            and isinstance(bootstrap.get("user_id"), str)
            and bool(bootstrap["user_id"])
            and isinstance(bootstrap.get("org_id"), str)
            and bootstrap["org_id"] == config.get("other_tenant_id")
            and bootstrap["org_id"] != session["org_id"],
            "Foreign Product actor must be the run-created bootstrap tenant",
        )
        foreign_status, foreign_identity = common.api(
            config, "/auth/cli/admin-session", bootstrap["access_token"], expect=(200,)
        )
        common.require(
            foreign_status == 200
            and isinstance(foreign_identity, dict)
            and foreign_identity.get("verified") is True
            and foreign_identity.get("role") == "platform_admin"
            and foreign_identity.get("user_id") == bootstrap["user_id"]
            and foreign_identity.get("org_id") == bootstrap["org_id"],
            "Foreign Product actor is not the authenticated bootstrap session",
        )
        foreign_client = ProductClient(config["gateway_url"], bootstrap["access_token"])

    @contextlib.contextmanager
    def client_factory(actor):
        common.require(
            actor == session["access_token"]
            or (foreign_client is not None and actor == bootstrap["access_token"]),
            "Product actor differs from instance session",
        )
        yield client if actor == session["access_token"] else foreign_client

    return session, client_factory
