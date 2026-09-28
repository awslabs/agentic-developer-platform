"""
Regression tests for the ingest Lambda's GitHub App token cache (issue #4071).

The bug: `_token_cache` was a single module-level `{"token", "expires_at"}` pair
keyed on nothing, and `_get_installation_token()` returned the cached token
*before* it ever read the `org` argument. A warm Lambda container therefore
handed the org that happened to warm it — org A — its token to a later request
for org B, for up to 3000 seconds. That token grants read/write on org A's
repositories, so org B's caller acts as org A.

These tests assert the OUTCOME (which org's token comes back), not the cache's
internal plumbing. They fail on pre-fix code.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys

import pytest

HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest"
)

# installation id -> the token GitHub would mint for it
INSTALLATION_BY_ORG = {"org-a": 111, "org-b": 222}
TOKEN_BY_INSTALLATION = {111: "ghs_TOKEN_FOR_ORG_A", 222: "ghs_TOKEN_FOR_ORG_B"}


@pytest.fixture(autouse=True)
def _patch_sys_path():
    original = sys.path.copy()
    sys.path.insert(0, HANDLER_DIR)
    yield
    sys.path = original


@pytest.fixture
def dispatch(monkeypatch):
    """A freshly-imported github_dispatch module with a cold token cache.

    Importing fresh per test is what makes 'warm container' explicit: within one
    test the module-level cache persists across calls, exactly as it does in a
    warm Lambda execution environment.
    """
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    monkeypatch.setenv("GH_APP_SECRET_PREFIX", "adp/test-org/gh-app-ops")

    sys.modules.pop("github_dispatch", None)
    module = importlib.import_module("github_dispatch")

    # Secrets Manager: return App id + private key without touching AWS.
    class _FakeSecrets:
        def get_secret_value(self, SecretId: str):  # noqa: N803 - boto3 kwarg name
            if SecretId.endswith("-id"):
                return {"SecretString": "900001"}
            return {"SecretString": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END..."}

    monkeypatch.setattr(module, "_get_secrets", lambda: _FakeSecrets())
    monkeypatch.setattr(module, "_create_jwt", lambda app_id, key: "fake.jwt")

    # Installation discovery already filters by org correctly — keep it honest.
    monkeypatch.setattr(
        module,
        "_get_installation_id",
        lambda jwt_token, org: INSTALLATION_BY_ORG.get(org.lower()),
    )

    # Record every mint so we can assert which installation was charged.
    minted: list[int] = []

    def _fake_urlopen(req):
        url = req.full_url
        installation_id = int(url.split("/app/installations/")[1].split("/")[0])
        minted.append(installation_id)
        payload = json.dumps({"token": TOKEN_BY_INSTALLATION[installation_id]})

        class _Resp:
            def __enter__(self_inner):
                return io.BytesIO(payload.encode())

            def __exit__(self_inner, *exc):
                return False

        return _Resp()

    monkeypatch.setattr(module.urllib.request, "urlopen", _fake_urlopen)

    module._minted = minted  # test-visible handle
    yield module
    sys.modules.pop("github_dispatch", None)


class TestPerOrgTokenIsolation:
    def test_org_b_does_not_receive_org_a_cached_token(self, dispatch):
        """The core cross-tenant defect: warm container, two orgs, two tokens."""
        token_a = dispatch._get_installation_token("org-a")
        token_b = dispatch._get_installation_token("org-b")

        assert token_a == "ghs_TOKEN_FOR_ORG_A"
        assert token_b == "ghs_TOKEN_FOR_ORG_B"
        assert token_b != token_a

    def test_each_org_is_minted_against_its_own_installation(self, dispatch):
        dispatch._get_installation_token("org-a")
        dispatch._get_installation_token("org-b")

        assert dispatch._minted == [111, 222]

    def test_org_case_is_normalized_to_one_cache_entry(self, dispatch):
        first = dispatch._get_installation_token("org-a")
        second = dispatch._get_installation_token("Org-A")

        assert first == second
        # Second call served from cache — no extra mint.
        assert dispatch._minted == [111]

    def test_repeat_call_for_same_org_is_served_from_cache(self, dispatch):
        dispatch._get_installation_token("org-a")
        dispatch._get_installation_token("org-a")

        assert dispatch._minted == [111]

    def test_interleaved_requests_never_cross_tenants(self, dispatch):
        """A→B→A→B ordering must return the right token every time."""
        sequence = ["org-a", "org-b", "org-a", "org-b"]
        tokens = [dispatch._get_installation_token(org) for org in sequence]

        assert tokens == [
            "ghs_TOKEN_FOR_ORG_A",
            "ghs_TOKEN_FOR_ORG_B",
            "ghs_TOKEN_FOR_ORG_A",
            "ghs_TOKEN_FOR_ORG_B",
        ]


class TestCacheExpiryAndBounds:
    def test_expired_entry_is_re_minted(self, dispatch, monkeypatch):
        dispatch._get_installation_token("org-a")
        assert dispatch._minted == [111]

        # Jump past the 3000s TTL.
        real_time = dispatch.time.time
        monkeypatch.setattr(dispatch.time, "time", lambda: real_time() + 4000)

        token = dispatch._get_installation_token("org-a")
        assert token == "ghs_TOKEN_FOR_ORG_A"
        assert dispatch._minted == [111, 111], "expired entry should have been re-minted"

    def test_cache_is_bounded(self, dispatch, monkeypatch):
        """The cache is a warm-container global — it must not grow unbounded."""
        cap = dispatch._TOKEN_CACHE_MAX_ENTRIES

        # Every org resolves to installation 111 for this test; we only care
        # about how many distinct keys the cache retains.
        monkeypatch.setattr(dispatch, "_get_installation_id", lambda jwt, org: 111)

        for i in range(cap + 10):
            dispatch._get_installation_token(f"org-{i}")

        assert len(dispatch._token_cache) <= cap

    def test_empty_org_is_refused(self, dispatch):
        assert dispatch._get_installation_token("") is None
        assert dispatch._minted == []
