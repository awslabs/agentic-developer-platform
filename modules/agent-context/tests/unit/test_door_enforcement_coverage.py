"""Structural guards on the Door's authorisation surface (#5658).

Why this file exists
--------------------
Every ACL bypass fixed under #5658 had the same shape: a read path that reached
storage without an authorisation decision having been made, and returned what it
found. Per-verb tests cannot catch the *next* one, because the next one arrives
as a new verb, a new backend, or a new stamp site that nobody wrote a test for.

So these tests assert properties of the dispatch surface itself rather than the
behaviour of any one verb:

1. Every verb reachable through dispatch is classified in exactly one
   authorisation bucket. Adding a verb without deciding how it is authorised
   fails the build.
2. Every ACL-enforced verb actually refuses when the ACL store is down —
   verified by calling dispatch, not by reading the table that claims it.
3. The classification table matches the advertised tool catalogue in both
   directions, so a tool cannot be published without being classified, nor
   classified without existing.

Property 2 is the one that matters most: a table saying a verb is enforced is
worthless if the code does not consult the table. It is checked against the real
``_dispatch_tool``.
"""

from __future__ import annotations

import pytest

from door import server as server_mod
from door.server import (
    ACL_ENFORCED_VERBS,
    DISPATCH_VERBS,
    OWNER_SCOPED_VERBS,
    TOOLS,
    _dispatch_tool,
)

# Minimal arguments that satisfy each verb's required parameters. The values are
# irrelevant — these tests assert the request is refused BEFORE any backend is
# consulted, so a well-formed call that would otherwise do real work is exactly
# the right probe.
_MINIMAL_ARGS: dict[str, dict[str, object]] = {
    "search": {"query": "anything", "scope": "code"},
    "understand": {"target": "org/repo::thing"},
    "impact": {"target": "org/repo::thing"},
    "browse": {"action": "ls", "uri": "/"},
    "secure": {"action": "posture", "target": "org/repo"},
    "remember": {"action": "recall", "query": "anything"},
    "experience": {"action": "recall", "query": "anything"},
}


@pytest.fixture
def acl_store_down():
    """Force the Door into the "no ACL store" state for one test.

    This is the state a misconfigured or database-less deployment is actually in,
    and the state in which the pre-#5658 Door served unfiltered cross-tenant
    results. Restored afterwards so test order cannot leak it.
    """
    previous_store = server_mod.state.acl_store
    previous_error = server_mod.state.acl_store_error
    server_mod.state.acl_store = None
    server_mod.state.acl_store_error = "ConnectionError: forced by test"
    try:
        yield
    finally:
        server_mod.state.acl_store = previous_store
        server_mod.state.acl_store_error = previous_error


class TestVerbClassificationIsTotal:
    """Every dispatchable verb is classified, exactly once."""

    def test_no_verb_is_unclassified(self):
        """DISPATCH_VERBS is exactly the union of the two buckets.

        A verb present in dispatch but in neither bucket would be reachable with
        no stated authorisation model.
        """
        assert DISPATCH_VERBS == ACL_ENFORCED_VERBS | OWNER_SCOPED_VERBS

    def test_buckets_are_disjoint(self):
        """No verb is both ACL-enforced and owner-scoped.

        The two models answer different questions ("may this caller see this
        repo?" vs "is this the owner's own memory?"). A verb in both would leave
        it ambiguous which check is load-bearing, and therefore which one may be
        removed without consequence.
        """
        assert not (ACL_ENFORCED_VERBS & OWNER_SCOPED_VERBS)

    def test_every_advertised_tool_is_classified(self):
        """Every verb in the published catalogue is dispatchable and classified.

        Catches the realistic ordering of a mistake: a tool is added to TOOLS so
        agents can call it, and the authorisation table is updated later.
        """
        advertised = {t["name"] for t in TOOLS}
        unclassified = advertised - DISPATCH_VERBS
        assert not unclassified, f"advertised but unclassified verbs: {sorted(unclassified)}"

    def test_no_classified_verb_is_missing_from_the_catalogue(self):
        """And the converse: the table names no verb that does not exist.

        A stale entry is not a vulnerability, but it makes the table unreliable
        as a description of the attack surface, which is what the other tests
        here depend on.
        """
        advertised = {t["name"] for t in TOOLS}
        phantom = DISPATCH_VERBS - advertised
        assert not phantom, f"classified but not advertised: {sorted(phantom)}"

    def test_every_verb_has_a_probe(self):
        """This test file itself covers every verb.

        Without this, adding a verb would silently skip the refusal test below
        rather than fail it — the coverage guard needs its own coverage guard.
        """
        missing = DISPATCH_VERBS - set(_MINIMAL_ARGS)
        assert not missing, f"verbs with no probe arguments in this file: {sorted(missing)}"


class TestEnforcedVerbsRefuseWithoutACLStore:
    """The refusal is verified through real dispatch, not asserted from a table."""

    @pytest.mark.parametrize("verb", sorted(ACL_ENFORCED_VERBS))
    @pytest.mark.asyncio
    async def test_enforced_verb_refuses(self, verb, acl_store_down):
        """With no ACL store, every enforced verb returns the refusal envelope.

        Crucially it must NOT return a result envelope — an empty ``results``
        list would be indistinguishable from "authorised, nothing matched", and
        that conflation is what let the unconfigured Door look healthy while
        serving unfiltered reads.
        """
        result = await _dispatch_tool(
            verb,
            dict(_MINIMAL_ARGS[verb]),
            headers={"x-github-login": "attacker", "x-tenant-id": "tenant-b"},
            caller=None,
        )

        assert result.get("code") == "acl_store_unavailable", (
            f"verb {verb!r} did not refuse with the ACL-store-unavailable code: {result!r}"
        )
        # No payload key of any kind may accompany a refusal.
        for payload_key in ("results", "definitions", "callers", "entries", "findings"):
            assert payload_key not in result, (
                f"verb {verb!r} returned a {payload_key!r} payload while refusing: {result!r}"
            )

    @pytest.mark.asyncio
    async def test_unknown_verb_is_rejected(self, acl_store_down):
        """An unrecognised name is an error, never a pass-through.

        Checked with the store down so a future refactor cannot make the unknown
        path the one that skips the gate.
        """
        result = await _dispatch_tool("exfiltrate", {}, headers={}, caller=None)
        assert "error" in result
        assert "results" not in result
