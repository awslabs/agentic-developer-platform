"""Keep cancellation credentials private and bound to the original evaluator."""

from __future__ import annotations

import json
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request

from tests.e2e.cli_uplift import bundle, ports, runner, statestore


def binding(cfg, ctx, *, owner=False):
    evaluation_id = runner.check_evaluation_id(ctx["evaluation_id"])
    session = ctx["document"].get("product_actor_session" if owner else "session") or {}
    suffix = "product-admin" if owner else "admin"
    directory = bundle.REMOTE_DIR + ("/product-actor" if owner else "")
    instance_id = ctx["document"].get("instance_id")
    if (
        not re.fullmatch(r"i-[a-f0-9]{8,17}", str(instance_id))
        or session.get("created_username")
        != f"{evaluation_id}-{suffix}@adp-eval.example"
        or session.get("session_ref") != directory + "/session.json"
        or not all(
            isinstance(session.get(field), str) and session[field]
            for field in ("user_id", "adp_user_id", "org_id")
        )
        # The owned resource is the ADP `users` ROW, so ownership is matched on
        # the row id. `install_auth._onboard` records it as `<org>/<row id>`
        # because that is what the org-scoped identity route the sweep deletes
        # through accepts, while `session["user_id"]` is the Cognito SUBJECT the
        # gateway attributes the session to. Matching the subject against the
        # row-id manifest is what made run 39773345088 refuse a session that did
        # own its actor, and would also have accepted a session whose row id
        # belonged to somebody else.
        or not any(
            entry["kind"] == "adp_user"
            and entry["id"] == session["org_id"] + "/" + session["adp_user_id"]
            and entry["prefix"] == evaluation_id
            for entry in ctx["manifest"].outstanding()
        )
    ):
        raise ports.PortError(
            "Product recovery session has no original actor ownership"
        )
    return {
        "evaluation_id": evaluation_id,
        "instance_id": instance_id,
        "config_fingerprint": runner.fingerprint(cfg),
        # Both identities travel with the binding, each for exactly one purpose:
        # the subject is checked against the authenticated session, the row id is
        # what the run owns and cleans up.
        "user_id": session["user_id"],
        "adp_user_id": session["adp_user_id"],
        "org_id": session["org_id"],
        "created_username": session["created_username"],
    }


def key(evaluation_id, *, owner=False):
    name = "product-owner-session" if owner else "product-session"
    return (
        f"{statestore.PREFIX}/{runner.check_evaluation_id(evaluation_id)}/{name}.json"
    )


# The reviewed CLI configuration contract is a gateway BASE URL that MAY carry a
# path. `tests/e2e/cli_uplift/config.example.json` and the private
# pre-production bindings both publish `https://<host>/api`, CloudFront removes
# that external `/api` prefix, and every other consumer composes
# `base + <mounted app path>`: preflight reads
# `base + "/.well-known/cognito-config"` and `remote/common.api` sends
# `base + "/api/admin/identity/organizations/<org>"`, both observed answering
# 200 against the live target.
#
# This guard refused ANY path, so run adp-e2e-20261022-200315-8f1eb8 could not
# retain its recovery session at all: install_auth failed with "requires the
# reviewed Gateway origin" against a correctly configured deployment, PR03-C03
# failed and PR03-C01/C02 never ran. Discarding the base path instead would
# send `/auth/cli/admin-session` to a host root the gateway does not serve it
# from, so the reviewed base path is honoured here rather than dropped.
#
# Bounded, not merely permitted: every segment must begin with an alphanumeric,
# which still refuses `.`/`..` traversal, empty segments (`//`), percent-encoded
# separators, whitespace and control characters. The scheme, embedded
# credentials, a query and a fragment are refused exactly as before, and the
# redirect and proxy refusals below are unchanged.
BASE_PATH = re.compile(r"(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)*")


def base_origin(cfg):
    """The reviewed HTTPS gateway base URL, its reviewed base path included."""
    reviewed = str(cfg.get("gateway_url") or "").rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(reviewed)
        credentials = bool(parsed.username or parsed.password)
    except ValueError:
        raise ports.PortError(
            "Product recovery requires the reviewed Gateway origin"
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or credentials
        or parsed.query
        or parsed.fragment
        or not BASE_PATH.fullmatch(parsed.path)
        or any(character.isspace() or ord(character) < 32 for character in reviewed)
    ):
        raise ports.PortError("Product recovery requires the reviewed Gateway origin")
    return reviewed


def request(
    cfg, path, *, token=None, body=None, idempotency_key=None, expected_status=200
):
    origin = base_origin(cfg)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, new_url):
            return None

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    outgoing = urllib.request.Request(
        origin + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        with opener.open(outgoing, timeout=30) as response:
            raw = response.read(32769)
            if response.status != expected_status or len(raw) > 32768:
                raise ValueError("Invalid response")
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return 401, {}
        raise ports.PortError(
            "Product recovery authentication was unavailable"
        ) from None
    except Exception:
        raise ports.PortError(
            "Product recovery authentication was unavailable"
        ) from None


def verify(cfg, expected, token):
    status, identity = request(cfg, "/auth/cli/admin-session", token=token)
    if status == 401:
        return False
    if (
        not isinstance(identity, dict)
        or identity.get("verified") is not True
        or identity.get("role") != "platform_admin"
        # `/auth/cli/admin-session` answers with `TokenContext.user_id`, which is
        # derived from the Cognito subject, so the SUBJECT is the only identifier
        # this route can confirm. The ADP row id is deliberately not asserted
        # here: the route never returns it, and comparing it against this answer
        # would either always fail or force a consumer to overwrite one id with
        # the other.
        or any(
            identity.get(field) != expected[field] for field in ("user_id", "org_id")
        )
    ):
        raise ports.PortError(
            "Product recovery session attribution differs from its owner"
        )
    return True


def retain(cfg, ctx, store, ssm, *, owner=False):
    expected = binding(cfg, ctx, owner=owner)
    private_key = key(ctx["evaluation_id"], owner=owner)
    existing = store._get(private_key)
    if existing is not None:
        restore(cfg, ctx, store, owner=owner)
        return
    result, session = ssm.json_result(
        expected["instance_id"],
        [
            "set -eu",
            "runuser -l ec2-user -c "
            + shlex.quote(
                "cat "
                + shlex.quote(
                    bundle.REMOTE_DIR
                    + ("/product-actor" if owner else "")
                    + "/session.json"
                )
            ),
        ],
        purpose="session_handoff",
        timeout=120,
    )
    if (
        result.get("Status") != "Success"
        or not isinstance(session, dict)
        or any(
            session.get(field) != expected[field]
            # The on-instance session document must carry BOTH identities and
            # agree with the binding on each. A document that published only one
            # of them cannot be told apart from one that published the wrong one.
            for field in ("user_id", "adp_user_id", "org_id", "created_username")
        )
        or not all(
            isinstance(session.get(field), str) and session[field]
            for field in ("access_token", "refresh_token")
        )
        or not verify(cfg, expected, session["access_token"])
    ):
        raise ports.PortError("Product cancellation session could not be verified")
    retained = {
        "binding": expected,
        **{field: session[field] for field in ("access_token", "refresh_token")},
    }
    store._put(private_key, retained)
    if store._get(private_key) != retained:
        raise ports.PortError("Product cancellation session was not durably retained")


def restore(cfg, ctx, store, *, owner=False):
    expected = binding(cfg, ctx, owner=owner)
    private_key = key(ctx["evaluation_id"], owner=owner)
    retained = store._get(private_key)
    if (
        not isinstance(retained, dict)
        or retained.get("binding") != expected
        or not all(
            isinstance(retained.get(field), str) and retained[field]
            for field in ("access_token", "refresh_token")
        )
    ):
        raise ports.PortError("Product recovery has no matching private session")
    if not verify(cfg, expected, retained["access_token"]):
        status, tokens = request(
            cfg, "/auth/cli/refresh", body={"refresh_token": retained["refresh_token"]}
        )
        if (
            status != 200
            or not isinstance(tokens, dict)
            or not tokens.get("access_token")
        ):
            raise ports.PortError("Product recovery session could not be renewed")
        retained = {
            **retained,
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token") or retained["refresh_token"],
        }
        store._put(private_key, retained)
        if store._get(private_key) != retained or not verify(
            cfg, expected, retained["access_token"]
        ):
            raise ports.PortError("Renewed Product recovery session was not verified")
    return {
        **expected,
        "prefix": ctx["evaluation_id"],
        "access_token": retained["access_token"],
    }


def clear(ctx, store):
    store._put(key(ctx["evaluation_id"]), {"retired": True})
    store._put(key(ctx["evaluation_id"], owner=True), {"retired": True})
