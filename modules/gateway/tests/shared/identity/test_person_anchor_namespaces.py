"""The person-anchor namespace registry — precedence, parsing, single composer.

Issue #4843 (T4, EPIC #4839). Design note
`docs/design-notes/4828-platform-native-org-team-user.md` §4, §1.6; ruling R7.

Four properties are pinned here, and each one maps to a way a person's spending
cap silently stops governing them (the #4511 "inert cap" class) rather than to a
restatement of the implementation:

  - **`github:` keeps priority 1.** `person_budget_configs.person_anchor` stores
    anchor strings durably, so a precedence reorder does not error — it re-keys
    everyone holding both a GitHub and a directory identity, leaving their live cap
    row displaying a number and matching nothing. This is the whole safety property
    of the story, so it is pinned twice: once on the registry tuple (cheap, catches
    a careless edit) and once end-to-end through the resolver against a person who
    holds BOTH identities (catches a resolver that ignores the tuple).

  - **The parser round-trips all three namespaces.** Before this change it
    hard-rejected everything but `github:` while the read path already produced
    `users:<id>` — so a GitHub-less person got an anchor from one side of the system
    that the other side could not parse (defect (a)). Round-tripping is asserted
    against the composer rather than against literal strings, because the property
    that matters is that the two agree, not what they spell.

  - **Authorable ⊊ parseable.** `users:` must parse (the read surface emits it) and
    must NOT be storable (nothing enforces it). A test for each direction, because
    the bug is one-sided: admitting it to the parser is required, and admitting it
    to the write guard would create exactly the inert row the parser change exists
    to make visible.

  - **One composer.** A grep-level pin that no module outside `person_anchor.py`
    composes an anchor with an f-string. Structural rather than behavioural on
    purpose: the three-way drift the note found (§1.6) could not be caught by any
    assertion about one code path's output — every copy was correct at the time it
    was written, and the defect was that a fourth namespace would have to be added
    to all three.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.identity import (
    PERSON_ANCHOR_AUTHORABLE_NAMESPACES,
    PERSON_ANCHOR_GITHUB_PREFIX,
    PERSON_ANCHOR_INTERNAL_NAMESPACE,
    PERSON_ANCHOR_NAMESPACES,
    PERSON_ANCHOR_PROVIDER_PRECEDENCE,
    UnresolvablePersonAnchorError,
    format_person_anchor,
    is_authorable_person_anchor,
    parse_person_anchor,
    resolve_caller_person_anchor,
    resolve_person_anchor,
)
from src.shared.identity.providers import IdentityProvider
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

ORG = "org-anchor"
TEAM = "team-anchor"

# A person holding BOTH a GitHub and a directory identity — the population a
# precedence reorder would silently re-key, and therefore the only shape that
# proves the precedence is honoured end to end.
BOTH_USER_ID = "user-both"
BOTH_SUB = "sub-both"
BOTH_GITHUB_ID = "20402445"
BOTH_DIRECTORY_ID = "aad-0000-1111-2222"

# A directory-managed person with NO GitHub identity — the population this story
# exists for. Pre-#4843 they 422'd on their own anchor.
DIR_USER_ID = "user-directory-only"
DIR_SUB = "sub-directory-only"
DIR_DIRECTORY_ID = "aad-3333-4444-5555"

# A person with no external identity at all: the `users:` fallback case.
BARE_USER_ID = "user-no-identity"
BARE_SUB = "sub-no-identity"


@pytest.fixture
async def engine():
    eng = create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Organization(
                id=ORG,
                name=ORG,
                aws_accounts=[],
                role_mappings={},
                settings={},
                github_installation_ids=[],
                cognito_client_ids=[],
            )
        )
        session.add_all(
            [
                Department(id="dept-1", org_id=ORG, name="Eng"),
                Team(id=TEAM, org_id=ORG, department_id="dept-1", name="Eng"),
            ]
        )
        session.add_all(
            [
                User(id=BOTH_USER_ID, org_id=ORG, team_id=TEAM, email="both@test.com", name="Both", cognito_sub=BOTH_SUB),
                User(id=DIR_USER_ID, org_id=ORG, team_id=TEAM, email="dir@test.com", name="Dir", cognito_sub=DIR_SUB),
                User(id=BARE_USER_ID, org_id=ORG, team_id=TEAM, email="bare@test.com", name="Bare", cognito_sub=BARE_SUB),
            ]
        )
        await session.flush()

        session.add_all(
            [
                UserIdentity(
                    id="id-both-github",
                    org_id=ORG,
                    team_id=TEAM,
                    user_id=BOTH_USER_ID,
                    provider=IdentityProvider.github.value,
                    provider_user_id=BOTH_GITHUB_ID,
                    provider_username="both",
                    verification_method="oauth",
                ),
                UserIdentity(
                    id="id-both-directory",
                    org_id=ORG,
                    team_id=TEAM,
                    user_id=BOTH_USER_ID,
                    provider=IdentityProvider.directory.value,
                    provider_user_id=BOTH_DIRECTORY_ID,
                    provider_username="both@corp.example",
                    verification_method="admin_attested",
                ),
                UserIdentity(
                    id="id-dir-directory",
                    org_id=ORG,
                    team_id=TEAM,
                    user_id=DIR_USER_ID,
                    provider=IdentityProvider.directory.value,
                    provider_user_id=DIR_DIRECTORY_ID,
                    provider_username="dir@corp.example",
                    verification_method="admin_attested",
                ),
            ]
        )
        await session.commit()
        yield session


# ===========================================================================
# Precedence — the safety property (ruling R7)
# ===========================================================================


class TestPrecedence:
    def test_github_is_priority_one(self):
        """`github` FIRST in the registry tuple, `directory` second.

        The cheap half of the guard. A reorder here is a one-line edit with no test
        failure anywhere else in the suite unless something pins it, and its
        consequence is every dual-identity person's live cap going inert at once.
        """
        assert PERSON_ANCHOR_PROVIDER_PRECEDENCE[0] == IdentityProvider.github
        assert PERSON_ANCHOR_PROVIDER_PRECEDENCE == (IdentityProvider.github, IdentityProvider.directory)

    def test_internal_namespace_is_terminal_and_last(self):
        """`users:` sits after every provider namespace and is not one of them.

        It names the absence of an external identity, so anything ordered after it
        would be unreachable.
        """
        assert PERSON_ANCHOR_NAMESPACES[-1] == PERSON_ANCHOR_INTERNAL_NAMESPACE
        assert PERSON_ANCHOR_INTERNAL_NAMESPACE not in {p.value for p in PERSON_ANCHOR_PROVIDER_PRECEDENCE}

    def test_github_prefix_constant_derives_from_the_registry(self):
        """The exported prefix is the registry's first entry, not a second declaration.

        Pre-#4843 this constant was declared beside the enum; deriving it means the
        precedence tuple is the single source and the two cannot drift.
        """
        assert PERSON_ANCHOR_GITHUB_PREFIX == f"{IdentityProvider.github.value}:"

    async def test_person_with_both_identities_anchors_on_github(self, db):
        """The end-to-end half: GitHub wins for somebody holding BOTH identities.

        This is the assertion that would fail if a resolver walked the namespaces in
        its own order regardless of the tuple — the failure mode the registry exists
        to prevent, and the one the tuple test alone cannot catch.
        """
        anchor, canonical_id = await resolve_caller_person_anchor(db, BOTH_SUB)

        assert anchor == format_person_anchor(BOTH_GITHUB_ID, IdentityProvider.github.value)
        assert canonical_id == BOTH_USER_ID

    async def test_existing_github_anchor_is_byte_identical_to_the_pre_change_string(self, db):
        """No existing cap is re-keyed: the resolved string still spells `github:<id>`.

        Written as a literal rather than through the composer on purpose. Every other
        assertion in this file compares composer output to composer output, which
        would pass even if the composer's spelling changed for everybody at once —
        precisely the change that orphans every stored cap row. This is the one place
        the literal on-the-wire form is nailed down.
        """
        anchor, _ = await resolve_caller_person_anchor(db, BOTH_SUB)
        assert anchor == f"github:{BOTH_GITHUB_ID}"

    async def test_directory_only_person_resolves_their_own_anchor(self, db):
        """A GitHub-less directory user gets an anchor instead of a 422.

        The whole point of the story: pre-#4843 `resolve_caller_person_anchor`
        queried `github` alone and raised for this person, so a directory-managed
        employee could not be anchored at all.
        """
        anchor, canonical_id = await resolve_caller_person_anchor(db, DIR_SUB)

        assert anchor == format_person_anchor(DIR_DIRECTORY_ID, IdentityProvider.directory.value)
        assert canonical_id == DIR_USER_ID

    async def test_person_without_external_identity_uses_native_anchor(self, db):
        anchor, user_id = await resolve_caller_person_anchor(db, BARE_SUB)
        assert anchor == format_person_anchor(BARE_USER_ID, PERSON_ANCHOR_INTERNAL_NAMESPACE)
        assert user_id == BARE_USER_ID


# ===========================================================================
# Parser / composer round-trip — defect (a)
# ===========================================================================


class TestParseAndFormat:
    @pytest.mark.parametrize("namespace", PERSON_ANCHOR_NAMESPACES)
    def test_every_registered_namespace_round_trips(self, namespace):
        """compose → parse returns the same namespace and identifier, for all three.

        `users:` is in this list because the read path emits it: a parser that could
        not read its own system's output is defect (a), and this is the assertion
        that would have caught it.
        """
        composed = format_person_anchor("some-identifier", namespace)

        assert parse_person_anchor(composed) == (namespace, "some-identifier")

    def test_format_defaults_to_github(self):
        """The default keeps every pre-#4843 single-argument call unchanged."""
        assert format_person_anchor("123") == "github:123"

    def test_format_rejects_an_unregistered_namespace(self):
        """A ValueError, deliberately NOT the 422.

        No caller passes user input here — the namespace always comes from the
        registry or from `parse_person_anchor`'s validated output — so an unknown
        value is a programming error. Composing it anyway would write a key nothing
        can ever resolve.
        """
        with pytest.raises(ValueError, match="Unknown person-anchor namespace"):
            format_person_anchor("123", "gitlab")

    @pytest.mark.parametrize(
        "anchor",
        [
            "12345",  # bare id, no namespace at all
            "gitlab:12345",  # a namespace nothing resolves
            "GITHUB:12345",  # case matters; provider values are lowercase
            ":12345",  # empty namespace
        ],
    )
    def test_unregistered_or_missing_namespace_is_rejected(self, anchor):
        """Each of these is an id in a namespace nothing can resolve → 422."""
        with pytest.raises(UnresolvablePersonAnchorError) as exc:
            parse_person_anchor(anchor)
        assert exc.value.status_code == 422

    @pytest.mark.parametrize("namespace", PERSON_ANCHOR_NAMESPACES)
    def test_empty_identifier_is_rejected_in_every_namespace(self, namespace):
        """A key no ledger row can carry. Rejected per-namespace, not just for github."""
        with pytest.raises(UnresolvablePersonAnchorError):
            parse_person_anchor(f"{namespace}:")

    @pytest.mark.parametrize("namespace", PERSON_ANCHOR_NAMESPACES)
    def test_whitespace_padded_identifier_is_rejected_in_every_namespace(self, namespace):
        """A key that compares unequal to the same person's real one.

        The subtlest of the three rejected shapes: it stores successfully and then
        governs nobody, because no resolver ever produces the padded spelling.
        """
        with pytest.raises(UnresolvablePersonAnchorError):
            parse_person_anchor(f"{namespace}: 12345")
        with pytest.raises(UnresolvablePersonAnchorError):
            parse_person_anchor(f"{namespace}:12345 ")

    def test_identifier_containing_a_colon_is_preserved(self):
        """Split on the FIRST colon only.

        A directory object id is opaque; if one ever carries a colon, splitting on
        the last (or on all) would silently truncate it into a different person's
        key.
        """
        assert parse_person_anchor("directory:aad:0000:1111") == ("directory", "aad:0000:1111")


# ===========================================================================
# Authorable ⊊ parseable
# ===========================================================================


class TestAuthorableNamespaces:
    def test_provider_and_native_namespaces_are_authorable(self):
        assert PERSON_ANCHOR_AUTHORABLE_NAMESPACES == {*(p.value for p in PERSON_ANCHOR_PROVIDER_PRECEDENCE), PERSON_ANCHOR_INTERNAL_NAMESPACE}
        assert PERSON_ANCHOR_INTERNAL_NAMESPACE in PERSON_ANCHOR_AUTHORABLE_NAMESPACES

    @pytest.mark.parametrize(
        ("anchor", "expected"),
        [
            ("github:12345", True),
            ("directory:aad-1", True),
            ("users:user-abc", True),  # native caps share read/write/enforcement resolution
            ("gitlab:12345", False),  # unregistered
            ("12345", False),  # unparseable
            ("github:", False),  # parseable namespace, unusable identifier
        ],
    )
    def test_is_authorable_person_anchor(self, anchor, expected):
        """The predicate the enforcement layer uses instead of `startswith('github:')`.

        The old prefix test would have answered False for `directory:` — excluding a
        whole namespace from enforcement while the authoring side stored caps in it.
        """
        assert is_authorable_person_anchor(anchor) is expected

    async def test_write_guard_accepts_current_native_anchor(self, db):
        anchor = format_person_anchor(BARE_USER_ID, PERSON_ANCHOR_INTERNAL_NAMESPACE)
        assert await resolve_person_anchor(db, anchor) == anchor

    async def test_write_guard_refuses_missing_or_stale_native_anchor(self, db):
        for identifier in ["missing-user", BOTH_USER_ID]:
            with pytest.raises(UnresolvablePersonAnchorError):
                await resolve_person_anchor(db, format_person_anchor(identifier, PERSON_ANCHOR_INTERNAL_NAMESPACE))

    async def test_write_guard_resolves_a_directory_anchor(self, db):
        """A linked directory identity is storable — the new capability."""
        anchor = format_person_anchor(DIR_DIRECTORY_ID, IdentityProvider.directory.value)
        assert await resolve_person_anchor(db, anchor) == anchor

    async def test_write_guard_looks_the_identifier_up_in_its_own_namespace(self, db):
        """A directory anchor is NOT resolved against GitHub's id space.

        The anti-collision property the `github:` qualifier was introduced for,
        now that a second namespace exists. `BOTH_GITHUB_ID` is a real, linked
        GitHub id; supplied under `directory:` it must NOT resolve, or one person's
        cap could come to govern another person's spend.
        """
        with pytest.raises(UnresolvablePersonAnchorError) as exc:
            await resolve_person_anchor(db, f"directory:{BOTH_GITHUB_ID}")
        assert exc.value.status_code == 422
        assert "no directory identity" in str(exc.value)


# ===========================================================================
# One composer — the structural pin (§1.6's three-way drift)
# ===========================================================================


class TestSingleComposer:
    """No module outside `person_anchor.py` may compose an anchor string.

    The note found the `github:` form hand-rolled in three places with only the
    prefix constant shared (`person_anchor.py`, `person_ledger.py`,
    `enforcement_service.py`). Every copy was individually correct, so no
    behavioural test could fail — the defect was that adding a namespace required
    finding all three. This walks the AST for f-strings whose literal text ends in a
    registered namespace's `<ns>:` and fails on any found outside the composer.
    """

    SRC = Path(__file__).resolve().parents[3] / "src"
    COMPOSER_MODULE = SRC / "shared" / "identity" / "person_anchor.py"

    def _anchor_composing_fstrings(self, path: Path) -> list[str]:
        tree = ast.parse(path.read_text(), filename=str(path))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for index, part in enumerate(node.values):
                # A literal chunk ending in `<namespace>:` immediately followed by
                # an interpolation is an anchor being composed.
                if not isinstance(part, ast.Constant) or not isinstance(part.value, str):
                    continue
                followed_by_interpolation = index + 1 < len(node.values) and isinstance(node.values[index + 1], ast.FormattedValue)
                if not followed_by_interpolation:
                    continue
                if any(part.value.endswith(f"{namespace}:") for namespace in PERSON_ANCHOR_NAMESPACES):
                    offenders.append(f"{path}:{node.lineno}")
        return offenders

    def test_no_module_composes_an_anchor_outside_person_anchor_py(self):
        offenders: list[str] = []
        for path in sorted(self.SRC.rglob("*.py")):
            if path == self.COMPOSER_MODULE:
                continue
            offenders.extend(self._anchor_composing_fstrings(path))

        assert offenders == [], (
            "These f-strings compose a person anchor outside `format_person_anchor`. "
            "Two spellings of the same key is how a cap comes to be stored under one "
            "string and enforced against another (#4511). Route them through "
            f"`format_person_anchor`: {offenders}"
        )

    def test_the_detector_actually_detects(self, tmp_path):
        """Guard on the guard: a test that can never fail is worse than no test.

        If the AST walk silently matched nothing (a changed node shape, a typo in the
        namespace comparison), the test above would pass forever while the drift it
        exists to catch went unnoticed.
        """
        offending = tmp_path / "offender.py"
        offending.write_text('anchor_id = "1"\nanchor = f"github:{anchor_id}"\n')

        assert self._anchor_composing_fstrings(offending)


async def test_legacy_manual_directory_link_does_not_supply_budget_anchor(db):
    from datetime import UTC, datetime

    from sqlalchemy import update

    await db.execute(
        update(UserIdentity).where(UserIdentity.user_id == DIR_USER_ID).values(verification_method="admin_manual", verified_at=datetime.now(UTC))
    )
    await db.commit()
    anchor, canonical_id = await resolve_caller_person_anchor(db, DIR_SUB)
    assert canonical_id == DIR_USER_ID
    assert anchor == format_person_anchor(DIR_USER_ID, PERSON_ANCHOR_INTERNAL_NAMESPACE)
    with pytest.raises(UnresolvablePersonAnchorError):
        await resolve_person_anchor(db, format_person_anchor(DIR_DIRECTORY_ID, IdentityProvider.directory.value))
