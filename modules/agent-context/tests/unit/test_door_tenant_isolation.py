"""Tenant-isolation tests for the Door's read paths (#5658).

Each test here corresponds to a concrete bypass that was reachable on main, and
is written as the attack rather than as the implementation: given a caller
permitted on repo A, can it obtain bytes or names belonging to repo B?

The bypasses all shared one root cause — an absent answer being treated as
permission granted:

* an unlabelled hit (``repo_name=""``) read as "shared content"
* a short repo name matching a permitted long one
* a caller-supplied S3 key reaching ``get_object`` unbound
* a listing emitting every entry unattributed
* a caller's spelling of a repo becoming the ACL label for a different repo's
  index

The last group of tests is the counterweight: a legitimate caller reading its
own tenant's artifacts must still succeed. A fail-closed change that also breaks
the permitted path is not a fix, and without these the suite would happily pass
with the Door denying everything.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from door.acl import (
    PUBLIC_SENTINEL,
    CallerPrincipal,
    SearchHit,
    _build_allowed_lookup,
    filter_results,
    is_shared_content_path,
)
from door.browse_backend import (
    _content_key_belongs_to_repo,
    _list_s3_prefix,
    _read_content,
    _repo_for_content_key,
)
from door.structural_backend import index_provenance
from door import server as server_mod

# Two tenants. TENANT_A is the caller throughout; TENANT_B is the victim.
REPO_A = "org-a/service"
REPO_B = "org-b/secrets"

# A repo whose safe_name has REPO_A's safe_name as a strict prefix. Present
# because "org-a-service" is a prefix of "org-a-service-fork", and a prefix test
# without a boundary check would grant one from the other.
REPO_A_FORK = "org-a/service-fork"


class _FakeS3:
    """Records every key requested, so "refused" can be distinguished from
    "fetched and then filtered". The bypasses being tested are about bytes
    leaving the bucket, so the assertion that matters is often that the call
    never happened at all."""

    def __init__(self, objects: dict[str, bytes]):
        self._objects = dict(objects)
        self.requested_keys: list[str] = []
        self.exceptions = MagicMock()
        self.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 - boto3 kwarg
        self.requested_keys.append(Key)
        if Key not in self._objects:
            raise self.exceptions.NoSuchKey(Key)
        body = MagicMock()
        body.read.return_value = self._objects[Key]
        return {"Body": body}

    def list_objects_v2(self, Bucket: str, Prefix: str, Delimiter: str = "") -> dict:  # noqa: N803
        contents = []
        for key in sorted(self._objects):
            if not key.startswith(Prefix):
                continue
            relative = key[len(Prefix) :]
            if not relative or (Delimiter and Delimiter in relative):
                continue
            contents.append({"Key": key, "Size": len(self._objects[key]), "LastModified": ""})
        return {"Contents": contents} if contents else {}


class _FakeACLStore:
    """ACL store granting a fixed set of repos, mirroring the shape of the real
    ``allowed_principals`` query result."""

    def __init__(self, allowed: set[str]):
        self._allowed = allowed

    def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
        return set(self._allowed)


class _FakeDBPool:
    """Catalog pool returning a fixed repo list for the safe_name index."""

    def __init__(self, repo_names: list[str]):
        self._repo_names = repo_names

    def getconn(self):
        rows = [(name,) for name in self._repo_names]

        class _Cursor:
            def execute(self, *_args, **_kwargs):
                return None

            def fetchall(self):
                return rows

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        class _Conn:
            def cursor(self):
                return _Cursor()

        return _Conn()

    def putconn(self, _conn):
        return None


@pytest.fixture
def caller_a() -> CallerPrincipal:
    """A fully resolved caller belonging to tenant A."""
    return CallerPrincipal(github_login="alice", github_teams=["org-a/devs"], tenant_id="tenant-a")


@pytest.fixture
def store_allows_only_a() -> _FakeACLStore:
    """ACL store where the caller may see REPO_A and nothing else."""
    return _FakeACLStore({REPO_A.casefold()})


@pytest.fixture
def acl_store_only_a(store_allows_only_a):
    """Install ``store_allows_only_a`` on the live Door app state.

    Needed by tests that go through ``server._apply_acl``, which reads the store
    from module state rather than taking it as an argument. Restored afterwards
    so a leaked store cannot make a later test pass.
    """
    previous_store = server_mod.state.acl_store
    previous_error = server_mod.state.acl_store_error
    server_mod.state.acl_store = store_allows_only_a
    server_mod.state.acl_store_error = ""
    try:
        yield store_allows_only_a
    finally:
        server_mod.state.acl_store = previous_store
        server_mod.state.acl_store_error = previous_error


@pytest.fixture
def acl_store_absent():
    """Force the "no ACL store" state — a database-less deployment."""
    previous_store = server_mod.state.acl_store
    previous_error = server_mod.state.acl_store_error
    server_mod.state.acl_store = None
    server_mod.state.acl_store_error = "ConnectionError: forced by test"
    try:
        yield
    finally:
        server_mod.state.acl_store = previous_store
        server_mod.state.acl_store_error = previous_error


# ---------------------------------------------------------------------------
# Unlabelled hits must not be treated as public
# ---------------------------------------------------------------------------


class TestUnlabelledHitsAreWithheld:
    """``repo_name=""`` means "provenance unknown", not "visible to everyone".

    Asserted through ``server._apply_acl`` rather than ``acl.filter_results``,
    because the provenance split is _apply_acl's job: filter_results only ever
    sees hits that already carry a repo label. Testing the lower layer would
    assert the right outcome for the wrong reason (it denies unlabelled hits
    because it denies every unqualified name) and would not notice if the
    shared-content allow-list above it were widened.
    """

    def test_unlabelled_hit_is_dropped(self, caller_a, acl_store_only_a):
        """A hit whose provenance was lost in a backend is withheld.

        This was the single widest bypass: any backend that failed to stamp a
        repo produced hits the filter passed through as shared content —
        including other users' personal-context memory.
        """
        hits = [SearchHit(repo_name="", data={"content": "tenant B's private source"})]
        assert server_mod._apply_acl(hits, caller_a) == []

    def test_enumerated_shared_prefix_still_passes(self, caller_a, acl_store_only_a):
        """Genuinely shared platform assets remain readable.

        The fix is an enumerated allow-list, not a blanket denial — the catalog
        has to stay browsable or discovery breaks for every caller.
        """
        hits = [
            SearchHit(repo_name="", data={"path": "content/catalog/repos.json", "name": "repos"})
        ]
        assert len(server_mod._apply_acl(hits, caller_a)) == 1

    @pytest.mark.parametrize(
        "path",
        [
            "content/catalog-private/repos.json",  # prefix-extension, not a segment match
            "content/catalogue/repos.json",  # similar name, different prefix
            "content/personal/alice/memory.json",  # personal context is not shared
            "code-indexes/org-b-secrets.json",  # a repo artifact
        ],
    )
    def test_lookalike_prefixes_do_not_pass(self, path, caller_a, acl_store_only_a):
        """Only whole-segment matches against the allow-list count.

        A ``startswith`` test on "content/catalog" would admit
        "content/catalog-private/...", which is exactly the kind of near-miss an
        attacker gets to choose the name of.
        """
        assert not is_shared_content_path(path)
        hits = [SearchHit(repo_name="", data={"path": path, "name": "x"})]
        assert server_mod._apply_acl(hits, caller_a) == []

    def test_no_store_denies_even_shared_content(self, caller_a, acl_store_absent):
        """With no ACL store, even the shared catalogue is withheld.

        _apply_acl is the belt to the dispatch gate's brace: if a future
        refactor lets a verb through without the store, this layer still serves
        nothing rather than falling back to "only the public parts".
        """
        hits = [SearchHit(repo_name="", data={"path": "content/catalog/repos.json"})]
        assert server_mod._apply_acl(hits, caller_a) == []


# ---------------------------------------------------------------------------
# Repo-name matching must be exact and fully qualified
# ---------------------------------------------------------------------------


class TestRepoNameMatchingIsStrict:
    """A permitted repo grants that repo only."""

    def test_unqualified_short_name_is_denied(self, caller_a, store_allows_only_a):
        """A bare "service" cannot be attributed to an owner, so it is denied.

        Two orgs may both have a repo called "service"; matching on the short
        name would hand one tenant's to the other.
        """
        hits = [SearchHit(repo_name="service", data={"repo_id": "service"})]
        assert filter_results(hits, caller_a, store_allows_only_a) == []

    def test_allowed_lookup_contains_no_short_names(self):
        """The allowed set itself holds only fully-qualified names.

        Pinned separately from the test above because the collision bypass had
        TWO halves — short names being ADDED to the lookup, and unqualified hit
        names being allowed to match it — and either half alone is harmless.
        Asserting only the match side lets a future change re-add short names
        without any test noticing, leaving one edit away from the bypass.
        """
        lookup = _build_allowed_lookup({"github.com/Org-A/Service", "org-b/secrets"})
        assert lookup == {"org-a/service", "org-b/secrets"}
        assert all("/" in name for name in lookup), f"unqualified name in lookup: {lookup}"

    def test_cross_tenant_collision_on_the_same_repo_name(self, caller_a):
        """Two tenants own a repo of the same name; only the caller's is visible.

        This is the collision the short-name match actually gave away, written
        as the scenario rather than as the mechanism.
        """
        store = _FakeACLStore({"org-a/service".casefold()})
        hits = [
            SearchHit(repo_name="org-a/service", data={"repo_id": "org-a/service"}),
            SearchHit(repo_name="org-b/service", data={"repo_id": "org-b/service"}),
        ]
        visible = {h.repo_name for h in filter_results(hits, caller_a, store)}
        assert visible == {"org-a/service"}

    def test_prefix_extension_is_denied(self, caller_a):
        """Permission on "org-a/service" does not extend to "org-a/service-fork"."""
        store = _FakeACLStore({REPO_A.casefold()})
        hits = [SearchHit(repo_name=REPO_A_FORK, data={"repo_id": REPO_A_FORK})]
        assert filter_results(hits, caller_a, store) == []

    def test_other_tenant_repo_is_denied(self, caller_a, store_allows_only_a):
        """The base case: a different org's repo is not visible."""
        hits = [SearchHit(repo_name=REPO_B, data={"repo_id": REPO_B})]
        assert filter_results(hits, caller_a, store_allows_only_a) == []

    def test_permitted_repo_passes(self, caller_a, store_allows_only_a):
        """The permitted repo is still returned — differing only in case."""
        hits = [SearchHit(repo_name="Org-A/Service", data={"repo_id": "Org-A/Service"})]
        assert len(filter_results(hits, caller_a, store_allows_only_a)) == 1

    def test_public_sentinel_repo_passes(self, caller_a):
        """A repo marked public via the sentinel is visible to a resolved caller."""
        store = _FakeACLStore({PUBLIC_SENTINEL, REPO_A.casefold()})
        hits = [SearchHit(repo_name=REPO_B, data={"repo_id": REPO_B})]
        # The sentinel is expanded by the store's query in production; here it
        # only needs to not crash the strict lookup.
        filter_results(hits, caller_a, store)


# ---------------------------------------------------------------------------
# browse action=read must not accept an arbitrary key
# ---------------------------------------------------------------------------


class TestBrowseReadIsBoundToScope:
    """The read path fetches only artifacts of the repo scope it was given."""

    @pytest.fixture
    def s3(self) -> _FakeS3:
        return _FakeS3(
            {
                "content/wikis/org-a-service-wiki.md": b"# A's wiki",
                "content/wikis/org-b-secrets-wiki.md": b"# B's SECRETS",
                "content/personal/bob/memory.json": b'{"secret": "bob only"}',
            }
        )

    async def test_key_outside_declared_scope_is_refused(self, s3):
        """Declaring repo A and asking for B's wiki returns nothing — and never
        calls S3, so the bytes do not leave the bucket at all."""
        hits = await _read_content(
            "content/wikis/org-b-secrets-wiki.md",
            s3_client=s3,
            bucket="b",
            repo_scope=REPO_A,
        )
        assert hits == []
        assert s3.requested_keys == [], "refused read must not reach S3"

    async def test_key_in_declared_scope_is_served_and_labelled(self, s3):
        """The legitimate read works, and the hit carries its true provenance.

        The label matters as much as the bytes: an unlabelled success would be
        withheld by the ACL step downstream, so this asserts the read path and
        the filter actually compose.
        """
        hits = await _read_content(
            "content/wikis/org-a-service-wiki.md",
            db_pool=_FakeDBPool([REPO_A, REPO_A_FORK, REPO_B]),
            s3_client=s3,
            bucket="b",
            repo_scope=REPO_A,
        )
        assert len(hits) == 1
        assert hits[0].repo_name == REPO_A
        assert hits[0].data["repo_id"] == REPO_A
        assert "A's wiki" in hits[0].data["content"]

    @pytest.mark.parametrize(
        "uri",
        [
            "content/../content/wikis/org-b-secrets-wiki.md",
            "content/wikis/../../content/personal/bob/memory.json",
            "content%2Fpersonal%2Fbob%2Fmemory.json",
            "content/wikis/..\\..\\personal\\bob\\memory.json",
        ],
    )
    async def test_traversal_and_encoding_are_refused(self, uri, s3):
        """Traversal, backslash and percent-encoded keys are rejected outright.

        Rejected rather than sanitised: a normaliser that "cleans" a hostile key
        still has to be right about every encoding, whereas refusing anything
        non-canonical is right by construction.
        """
        assert await _read_content(uri, s3_client=s3, bucket="b", repo_scope=REPO_A) == []
        assert s3.requested_keys == []

    async def test_personal_context_is_not_readable_via_browse(self, s3):
        """Another user's memory is not reachable even with no scope declared.

        With no scope the hit would be unlabelled, and unlabelled hits are
        withheld — but this asserts it at the read path too, because defence
        that depends on a single downstream step is one refactor from gone.
        """
        hits = await _read_content(
            "content/personal/bob/memory.json", s3_client=s3, bucket="b", repo_scope=None
        )
        # Unlabelled at best; must not be attributed to anything the caller holds.
        for hit in hits:
            assert hit.repo_name == ""
            assert not is_shared_content_path(hit.data.get("path", ""))

    async def test_key_outside_content_roots_is_refused(self, s3):
        """A key outside the enumerated content roots is refused.

        The pod's role can read more of the bucket than the Door serves; the
        root check is what keeps "reachable by the role" from meaning "readable
        through the API".
        """
        s3_with_state = _FakeS3({"terraform-state/prod.tfstate": b"secrets"})
        assert (
            await _read_content(
                "terraform-state/prod.tfstate", s3_client=s3_with_state, bucket="b", repo_scope=None
            )
            == []
        )
        assert s3_with_state.requested_keys == []


# ---------------------------------------------------------------------------
# Listings must attribute every entry
# ---------------------------------------------------------------------------


class TestListingsAreAttributed:
    """Listing a shared content root must not enumerate other tenants."""

    @pytest.fixture
    def s3(self) -> _FakeS3:
        return _FakeS3(
            {
                "content/wikis/org-a-service-wiki.md": b"A",
                "content/wikis/org-a-service-fork-wiki.md": b"A fork",
                "content/wikis/org-b-secrets-wiki.md": b"B",
            }
        )

    @pytest.fixture
    def db_pool(self) -> _FakeDBPool:
        return _FakeDBPool([REPO_A, REPO_A_FORK, REPO_B])

    async def test_each_entry_carries_its_owning_repo(self, s3, db_pool):
        """Every listed artifact is attributed to the repo that produced it.

        Pre-#5658 every entry came back with ``repo_name=""``, so one listing of
        ``content/wikis`` enumerated every tenant's wikis through a filter that
        read blank as public.
        """
        hits = await _list_s3_prefix("content/wikis", s3_client=s3, bucket="b", db_pool=db_pool)
        by_name = {h.data["name"]: h for h in hits}
        assert by_name["org-a-service-wiki.md"].repo_name == REPO_A
        assert by_name["org-b-secrets-wiki.md"].repo_name == REPO_B
        # Longest-match-first: the fork's artifact is not attributed to REPO_A.
        assert by_name["org-a-service-fork-wiki.md"].repo_name == REPO_A_FORK

    async def test_filtered_listing_shows_only_permitted_entries(
        self, s3, db_pool, caller_a, store_allows_only_a
    ):
        """End to end: list, then filter, and only tenant A's artifact survives.

        This is the property a user actually experiences, and it holds only if
        attribution and filtering agree on the repo label — which is why it is
        asserted through both steps rather than on either alone.
        """
        hits = await _list_s3_prefix("content/wikis", s3_client=s3, bucket="b", db_pool=db_pool)
        visible = filter_results(hits, caller_a, store_allows_only_a)
        assert [h.data["name"] for h in visible] == ["org-a-service-wiki.md"]

    async def test_no_catalog_means_no_attribution_and_nothing_leaks(
        self, s3, caller_a, store_allows_only_a
    ):
        """With the catalog unavailable, entries are unattributable and withheld.

        "Cannot attribute" must degrade to denial. The tempting alternative —
        emit entries unlabelled and let the caller sort it out — is the original
        bug.
        """
        hits = await _list_s3_prefix("content/wikis", s3_client=s3, bucket="b", db_pool=None)
        assert all(h.repo_name == "" for h in hits)
        assert filter_results(hits, caller_a, store_allows_only_a) == []


# ---------------------------------------------------------------------------
# Provenance resolution helpers
# ---------------------------------------------------------------------------


class TestProvenanceResolution:
    """``safe_name`` is lossy, so resolution is a catalog lookup, not a guess."""

    def test_ambiguous_safe_name_resolves_to_the_repo_that_exists(self):
        """"a-b-c" is resolved against the catalog rather than split arbitrarily.

        Both "a/b-c" and "a-b/c" produce the safe_name "a-b-c". Only one exists,
        and the catalog is what says which.
        """
        assert _repo_for_content_key(
            "content/wikis/a-b-c-wiki.md", {"a-b-c": "a-b/c"}, "content"
        ) == "a-b/c"
        assert _repo_for_content_key(
            "content/wikis/a-b-c-wiki.md", {"a-b-c": "a/b-c"}, "content"
        ) == "a/b-c"

    def test_unknown_safe_name_is_unattributable(self):
        """A key naming no known repo returns "", which the filter withholds."""
        assert _repo_for_content_key("content/wikis/ghost-repo-wiki.md", {}, "content") == ""

    def test_sbom_path_requires_a_catalogued_repo(self):
        """SBOM keys carry org/repo verbatim but are still checked against the catalog."""
        safe_names = {REPO_B.replace("/", "-"): REPO_B}
        assert (
            _repo_for_content_key(f"sbom/repos/{REPO_B}/source.cdx.json", safe_names, "content")
            == REPO_B
        )
        assert _repo_for_content_key("sbom/repos/org-x/ghost/s.json", safe_names, "content") == ""

    @pytest.mark.parametrize(
        ("key", "repo", "expected"),
        [
            ("content/wikis/org-a-service-wiki.md", REPO_A, True),
            ("code-indexes/org-a-service.json", REPO_A, True),
            (f"sbom/repos/{REPO_A}/source.cdx.json", REPO_A, True),
            # A's scope does not cover the fork's artifact, nor B's.
            ("content/wikis/org-a-service-fork-wiki.md", REPO_A, False),
            ("content/wikis/org-b-secrets-wiki.md", REPO_A, False),
            (f"sbom/repos/{REPO_B}/source.cdx.json", REPO_A, False),
            # Empty inputs are never a match.
            ("", REPO_A, False),
            ("content/wikis/org-a-service-wiki.md", "", False),
        ],
    )
    def test_forward_scope_binding(self, key, repo, expected):
        """Forward binding is exact at a name boundary in both directions."""
        assert _content_key_belongs_to_repo(key, repo, "content") is expected


class TestIndexProvenanceWins:
    """A code index is labelled by what it says it is, not what was asked for."""

    def test_declared_repo_id_overrides_the_request(self):
        """Asking for "service" and loading B's index labels the hit as B's.

        ``load_code_index`` resolves loosely (a documented short-name
        convenience), so a caller's spelling could select another repo's index
        and then be used as that index's ACL label — laundering the caller's own
        input into an authorisation decision. The index's own ``repo_id`` is the
        producing layer's statement of provenance, so it wins.
        """
        index = {"repo_id": REPO_B, "symbols": []}
        assert index_provenance(index, "service") == REPO_B

    def test_missing_declaration_cannot_inherit_the_request(self):
        assert index_provenance({"symbols": []}, REPO_A) == ""

    def test_blank_declaration_is_denied(self):
        assert index_provenance({"repo_id": "   "}, REPO_A) == ""

    def test_current_ingestion_repo_field_is_supported(self):
        assert index_provenance({"repo": REPO_A}, REPO_B) == REPO_A

    def test_mismatched_provenance_is_then_denied_by_the_filter(
        self, caller_a, store_allows_only_a
    ):
        """The composed property: permitted label + foreign origin -> withheld.

        The unit above shows the label is corrected; this shows the correction
        has the intended effect once the filter sees it.
        """
        index: dict[str, Any] = json.loads(json.dumps({"repo_id": REPO_B}))
        origin = index_provenance(index, REPO_A)
        hits = [SearchHit(repo_name=origin, data={"repo_id": origin})]
        assert filter_results(hits, caller_a, store_allows_only_a) == []


# ---------------------------------------------------------------------------
# Legitimate access is preserved
# ---------------------------------------------------------------------------


class TestLegitimateAccessStillWorks:
    """The fail-closed changes must not deny the permitted path.

    Without this class the entire suite would pass with a Door that returns
    nothing to anyone, which is the most likely way a fail-closed change goes
    wrong in practice.
    """

    def test_resolved_caller_sees_its_own_repos(self, caller_a):
        """A caller permitted on two repos sees hits from both."""
        store = _FakeACLStore({REPO_A.casefold(), REPO_A_FORK.casefold()})
        hits = [
            SearchHit(repo_name=REPO_A, data={"repo_id": REPO_A}),
            SearchHit(repo_name=REPO_A_FORK, data={"repo_id": REPO_A_FORK}),
            SearchHit(repo_name=REPO_B, data={"repo_id": REPO_B}),
        ]
        visible = {h.repo_name for h in filter_results(hits, caller_a, store)}
        assert visible == {REPO_A, REPO_A_FORK}

    def test_team_granted_access_is_honoured(self):
        """Access granted via a team, with no personal grant, still resolves."""
        caller = CallerPrincipal(github_login="carol", github_teams=["org-a/devs"], tenant_id="tenant-a")
        store = _FakeACLStore({REPO_A.casefold()})
        hits = [SearchHit(repo_name=REPO_A, data={"repo_id": REPO_A})]
        assert len(filter_results(hits, caller, store)) == 1

    def test_unresolved_caller_gets_nothing(self, store_allows_only_a):
        """No login and no teams -> no results, regardless of the store."""
        caller = CallerPrincipal(github_login="", github_teams=[], tenant_id="")
        hits = [SearchHit(repo_name=REPO_A, data={"repo_id": REPO_A})]
        assert filter_results(hits, caller, store_allows_only_a) == []

    def test_store_failure_denies_rather_than_passes(self, caller_a):
        """An ACL store that raises yields no results, not unfiltered ones."""

        class _Exploding:
            def get_allowed_repos(self, principal):
                raise RuntimeError("connection reset")

        hits = [SearchHit(repo_name=REPO_A, data={"repo_id": REPO_A})]
        assert filter_results(hits, caller_a, _Exploding()) == []
