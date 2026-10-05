"""Ingestion and the Door must derive ACLs by the same rule (#5658).

``images/ingestion/repo_acl.py`` carries a copy of
``door/acl.py::derive_acl_from_github`` because the ingestion image is a separate
Docker build context. The function is pinned AST-identical to the Door's copy.

Why AST rather than bytes: the two files have different module context (imports,
logger name), so byte equality is not achievable and demanding it would force a
fake. The AST of the *function* is exactly the shared part — the rule for deciding
who may read a repository.

Why this is worth pinning at all: the Door FILTERS reads by ``allowed_principals``
and ingestion STAMPS it. If the two drift, the system does not fail visibly. It
produces a consistent-looking result computed from two different rules — e.g.
ingestion counts a "triage" collaborator as allowed while the Door's rule never
would, or vice versa, and repos become either invisible or over-shared with nothing
in any log to say why. Divergence here is silent by construction, so a test is the
only thing that surfaces it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_MODULE_ROOT = Path(__file__).resolve().parents[2]
_DOOR_ACL = _MODULE_ROOT / "door" / "acl.py"
_INGESTION_ACL = _MODULE_ROOT / "images" / "ingestion" / "repo_acl.py"

_INGESTION_DIR = str(_MODULE_ROOT / "images" / "ingestion")
if _INGESTION_DIR not in sys.path:
    sys.path.insert(0, _INGESTION_DIR)

import repo_acl  # noqa: E402  (sys.path must be set first — see test_s3_prefix_routing.py)

_SHARED_FUNCTION = "derive_acl_from_github"


def _function_ast(path: Path, name: str) -> ast.FunctionDef | None:
    """Return the named top-level function's AST node, or None."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


class TestDeriveFunctionIsShared:
    def test_both_copies_define_the_function(self):
        assert _function_ast(_DOOR_ACL, _SHARED_FUNCTION) is not None, (
            f"{_SHARED_FUNCTION} missing from door/acl.py"
        )
        assert _function_ast(_INGESTION_ACL, _SHARED_FUNCTION) is not None, (
            f"{_SHARED_FUNCTION} missing from images/ingestion/repo_acl.py"
        )

    def test_the_two_copies_are_ast_identical(self):
        """Same logic, modulo formatting and comments."""
        door_fn = _function_ast(_DOOR_ACL, _SHARED_FUNCTION)
        ingestion_fn = _function_ast(_INGESTION_ACL, _SHARED_FUNCTION)
        if door_fn is None or ingestion_fn is None:
            pytest.fail(f"{_SHARED_FUNCTION} missing from one of the two files")

        assert ast.dump(ingestion_fn) == ast.dump(door_fn), (
            "derive_acl_from_github has diverged between door/acl.py and "
            "images/ingestion/repo_acl.py. The Door filters reads by "
            "allowed_principals and ingestion stamps it; two different rules "
            "produce silently wrong visibility. Re-sync the copy."
        )

    def test_public_sentinel_agrees(self):
        """Both sides must mean the same thing by "public".

        A mismatch here is total: ingestion would stamp a value the Door never
        treats as public, making every public repo unreadable — or worse, the
        reverse.
        """
        from door.acl import PUBLIC_SENTINEL as door_sentinel

        assert repo_acl.PUBLIC_SENTINEL == door_sentinel == "*"


class TestResolveFailsClosed:
    """The wrapper that replaced the unconditional ``["*"]`` never re-introduces it."""

    def test_no_token_denies_rather_than_publishing(self):
        """Without a token we cannot tell public from private, so we deny.

        The alternative — assuming public — is what the original code did by
        writing ``["*"]`` unconditionally.
        """
        result = repo_acl.resolve_allowed_principals(
            "org/private-repo", token="", token_path="/nonexistent/path"
        )
        assert result == []
        assert repo_acl.PUBLIC_SENTINEL not in result

    def test_unexpected_error_propagates_instead_of_degrading(self, monkeypatch):
        """An error that escapes derivation aborts; it does not become a default.

        ``derive_acl_from_github`` handles API failures internally and returns ``[]``,
        so anything escaping it is a programming error. Two safe responses exist —
        propagate (repo not ingested) or return ``[]`` (ingested, unreadable). This
        pins the one we chose, because the unsafe third option, swallowing the error
        into ``["*"]``, is exactly the shape a future "make ingestion more resilient"
        change would take.
        """

        def exploding_derive(repo_full_name, github_token, **kwargs):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(repo_acl, "derive_acl_from_github", exploding_derive)

        with pytest.raises(RuntimeError):
            repo_acl.resolve_allowed_principals("org/private-repo", token="t0ken")

    def test_empty_derivation_is_passed_through_unchanged(self, monkeypatch):
        """An empty derived ACL stays empty; it is not "helpfully" widened."""
        monkeypatch.setattr(
            repo_acl, "derive_acl_from_github", lambda *a, **k: []
        )
        assert repo_acl.resolve_allowed_principals("org/private", token="t0ken") == []

    def test_public_repo_gets_the_sentinel(self, monkeypatch):
        """The legitimate public case still works — otherwise this is unusable."""
        monkeypatch.setattr(
            repo_acl, "derive_acl_from_github", lambda *a, **k: [repo_acl.PUBLIC_SENTINEL]
        )
        assert repo_acl.resolve_allowed_principals("org/public", token="t0ken") == ["*"]

    def test_private_repo_gets_its_derived_principals(self, monkeypatch):
        """And the private case carries the derived logins/teams through verbatim."""
        derived = ["alice", "org/platform-team"]
        monkeypatch.setattr(repo_acl, "derive_acl_from_github", lambda *a, **k: list(derived))
        assert repo_acl.resolve_allowed_principals("org/private", token="t0ken") == derived

    def test_token_is_never_logged(self, monkeypatch, caplog):
        """A token must not reach the logs on any path, including the failure ones."""
        secret = "ghs_EXAMPLE_NOT_A_REAL_TOKEN_0000"
        monkeypatch.setattr(repo_acl, "derive_acl_from_github", lambda *a, **k: [])

        with caplog.at_level("DEBUG"):
            repo_acl.resolve_allowed_principals("org/private", token=secret)

        assert secret not in caplog.text


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeGitHub:
    """Routes GET by path suffix so the real derivation logic runs unmodified."""

    def __init__(self, *, repo=None, collaborators=None, teams=None):
        self.repo = repo
        self.collaborators = collaborators if collaborators is not None else []
        self.teams = teams if teams is not None else []
        self.paths: list[str] = []
        self.auth_headers: list[str] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.paths.append(url)
        self.auth_headers.append((headers or {}).get("Authorization", ""))
        page = (params or {}).get("page", 1)
        if url.endswith("/collaborators"):
            return _FakeResponse(200, self.collaborators if page == 1 else [])
        if url.endswith("/teams"):
            return _FakeResponse(200, self.teams if page == 1 else [])
        if self.repo is None:
            return _FakeResponse(404, {})
        if isinstance(self.repo, int):
            return _FakeResponse(self.repo, {})
        return _FakeResponse(200, self.repo)


@pytest.fixture
def fake_github(monkeypatch):
    """Install a fake `requests` module for repo_acl's function-local import."""

    def _install(**kwargs):
        gh = _FakeGitHub(**kwargs)
        import types

        stub = types.ModuleType("requests")
        stub.get = gh.get
        monkeypatch.setitem(sys.modules, "requests", stub)
        return gh

    return _install


class TestVendoredDerivationBehaviour:
    """The copied logic, exercised end to end. AST identity proves it matches the
    Door's copy; these prove the logic itself is right, so a drift fix cannot
    re-sync both copies onto a broken rule."""

    def test_public_repo_yields_the_sentinel(self, fake_github):
        fake_github(repo={"visibility": "public"})
        assert repo_acl.derive_acl_from_github("org/pub", "t0ken") == ["*"]

    def test_private_repo_never_yields_the_sentinel(self, fake_github):
        """The core defect: a private repo must not be stamped public."""
        gh = fake_github(
            repo={"visibility": "private"},
            collaborators=[{"login": "Alice", "permissions": {"push": True}}],
            teams=[{"slug": "platform", "permission": "maintain"}],
        )
        acl = repo_acl.derive_acl_from_github("org/priv", "t0ken")

        assert repo_acl.PUBLIC_SENTINEL not in acl
        assert acl == ["alice", "org/platform"], "logins and team slugs, lowercased"
        assert any(p.endswith("/collaborators") for p in gh.paths)
        assert any(p.endswith("/teams") for p in gh.paths)

    def test_read_only_principals_are_excluded(self, fake_github):
        """Only push+ access grants a principal; pull-only does not."""
        fake_github(
            repo={"visibility": "private"},
            collaborators=[
                {"login": "reader", "permissions": {"pull": True}},
                {"login": "writer", "permissions": {"push": True}},
            ],
            teams=[
                {"slug": "triage-only", "permission": "triage"},
                {"slug": "writers", "permission": "push"},
            ],
        )
        acl = repo_acl.derive_acl_from_github("org/priv", "t0ken")
        assert acl == ["writer", "org/writers"]

    def test_missing_visibility_field_is_treated_as_private(self, fake_github):
        """An API response without `visibility` must not default to public."""
        fake_github(repo={}, collaborators=[], teams=[])
        acl = repo_acl.derive_acl_from_github("org/unknown", "t0ken")
        assert repo_acl.PUBLIC_SENTINEL not in acl

    @pytest.mark.parametrize("status", [401, 403, 404, 500])
    def test_repo_lookup_failure_denies(self, fake_github, status):
        """Any non-200 on the visibility check denies rather than guessing."""
        fake_github(repo=status)
        assert repo_acl.derive_acl_from_github("org/priv", "t0ken") == []

    def test_transport_exception_denies(self, monkeypatch):
        import types

        stub = types.ModuleType("requests")

        def boom(*a, **k):
            raise OSError("connection reset")

        stub.get = boom
        monkeypatch.setitem(sys.modules, "requests", stub)
        assert repo_acl.derive_acl_from_github("org/priv", "t0ken") == []

    def test_resolver_and_derivation_agree_on_a_private_repo(self, fake_github):
        """End to end through the wrapper the stamping site actually calls."""
        fake_github(
            repo={"visibility": "private"},
            collaborators=[{"login": "bob", "permissions": {"admin": True}}],
        )
        acl = repo_acl.resolve_allowed_principals("org/priv", token="t0ken")
        assert acl == ["bob"]
        assert repo_acl.PUBLIC_SENTINEL not in acl


class TestStampingSiteUsesTheDerivation:
    """The unconditional ``["*"]`` must be gone from the stamping site.

    Asserted against the source text because the surrounding function needs a live
    DeepWiki, S3 and DB to execute. A source assertion is weaker than a behavioural
    one, and is used only because the alternative here is no coverage at all.
    """

    def test_ingest_repo_no_longer_hardcodes_the_public_sentinel(self):
        source = (_MODULE_ROOT / "images" / "ingestion" / "ingest-repo.py").read_text()
        assert 'allowed_principals = ["*"]' not in source, (
            "ingest-repo.py still stamps the public sentinel unconditionally — "
            "every private repo it ingests is published readable to everyone"
        )

    def test_ingest_repo_calls_the_resolver(self):
        source = (_MODULE_ROOT / "images" / "ingestion" / "ingest-repo.py").read_text()
        assert "resolve_allowed_principals(" in source
        assert "from repo_acl import resolve_allowed_principals" in source
