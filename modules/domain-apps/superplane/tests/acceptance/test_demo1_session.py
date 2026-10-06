"""Private requester session import, without a browser or external calls."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_demo1_live import inputs

from superplane_acceptance.demo1_evidence import EvidenceError
from superplane_acceptance.demo1_session import (
    browser_state_parts,
    restore_browser_session,
)


@pytest.mark.parametrize("legacy", [False, True])
def test_session_import_keeps_tokens_out_of_persistent_storage(legacy):
    _, authority, value = inputs()
    origin = value["origins"][0]
    if not legacy:
        origin["sessionStorage"] = origin.pop("localStorage")
    origin.setdefault("localStorage", []).append({"name": "receipt", "value": "saved"})
    original = deepcopy(value)
    state, session = browser_state_parts(value, authority["origin"])
    assert session["cognito_access_token"]
    assert state["origins"][0]["localStorage"] == [
        {"name": "receipt", "value": "saved"}
    ]
    assert "sessionStorage" not in state["origins"][0]
    assert value == original


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate",
        "ambiguous",
        "malformed",
        "empty",
        "oversized",
        "foreign-origin",
        "foreign-cookie",
    ],
)
def test_invalid_session_is_refused_before_browser_effects(failure):
    _, authority, value = inputs()
    origin = value["origins"][0]
    token = origin["localStorage"][0]
    if failure == "duplicate":
        origin["localStorage"].append(dict(token))
    elif failure == "ambiguous":
        origin["sessionStorage"] = [dict(token)]
    elif failure == "malformed":
        token["value"] = {"nested": "private"}
    elif failure == "empty":
        token["value"] = ""
    elif failure == "oversized":
        token["value"] = "private" * 10000
    elif failure == "foreign-origin":
        origin["origin"] = "https://foreign.invalid"
    else:
        value["cookies"] = [{"domain": "foreign.invalid"}]
    with pytest.raises(EvidenceError) as caught:
        browser_state_parts(value, authority["origin"])
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("failure", [None, "navigation", "evaluate", "origin"])
def test_session_bootstrap_is_local_one_shot_and_always_unrouted(failure):
    page = Mock(url="about:blank")
    origin = "https://example.invalid"
    bootstrap = origin + "/.well-known/adp-demo1-session"

    def navigate(url, **options):
        assert url == bootstrap and options["timeout"] == 1000
        if failure == "navigation":
            raise RuntimeError("unavailable")
        page.url = origin if failure == "origin" else url

    page.goto.side_effect = navigate
    if failure == "evaluate":
        page.evaluate.side_effect = RuntimeError("unavailable")
    session = {"cognito_access_token": "synthetic-requester"}
    if failure:
        with pytest.raises((RuntimeError, EvidenceError)):
            restore_browser_session(page, origin, session, timeout=1000)
    else:
        restore_browser_session(page, origin, session, timeout=1000)
        page.evaluate.assert_called_once()
        assert page.evaluate.call_args.args[1] == session
    handler = page.route.call_args.args[1]
    route = SimpleNamespace(fulfill=Mock())
    handler(route)
    route.fulfill.assert_called_once_with(
        status=200, content_type="text/html", body="<!doctype html>"
    )
    page.unroute.assert_called_once_with(bootstrap, handler)


def test_session_bootstrap_cannot_replace_an_existing_browser_page():
    page = Mock(url="https://foreign.invalid")
    with pytest.raises(EvidenceError, match="fresh browser"):
        restore_browser_session(page, "https://example.invalid", {}, timeout=1000)
    page.route.assert_not_called()
