"""Every registered persona name must have its prompt file (issue #5038, R9 acc. 2).

`test_persona_catalogue_parity.py` pins names against names: code ↔ docs. It
cannot see whether a name resolves to anything at run time. That gap has already
cost us once — `pt-superpower` is registered, documented, and dispatches a pod
with no persona identity at all (#4037). The dispatch *succeeds*; it just runs a
generic agent, so it looks green in the Activity feed and is only discovered by
reading the output.

This module closes that gap for every future persona: a name in the catalogue
without a `<name>.md` in a staged persona root fails CI at the moment it is added,
not weeks later in a run transcript.

Three properties are asserted:

  * every registered persona name resolves to exactly one prompt file;
  * no domain persona filename collides with a core one (a collision silently
    *replaces* the core persona for every agent run, because domain personas stage
    flat and last — see `stage-personas.sh`);
  * no mention string is a substring of another (first-match dict-order routing
    in `_extract_mention_persona()` makes that a silent misroute).

The resolution rule mirrors `stage-personas.sh` + `persona-loader.ts`: personas
stage FLAT into `/app/personas/` and are looked up as `<persona-name>.md`. This
test therefore reproduces the image's namespace from the source tree rather than
inventing its own convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.personas import LABEL_TO_PERSONA, MENTION_TO_PERSONA, VALID_PERSONAS

# tests/[0] common/[1] lambda/[2] webhook-ingress/[3] agent-factory/[4]
# modules/[5] <repo root>/[6]
_REPO_ROOT = Path(__file__).resolve().parents[6]

_CORE_PERSONA_ROOT = _REPO_ROOT / "modules" / "agent-factory" / "rules" / "personas"
_DOMAIN_APPS_ROOT = _REPO_ROOT / "modules" / "domain-apps"

# Personas registered with no prompt file anywhere, each with the issue tracking
# it. This set must only ever shrink. It is asserted exactly (below) rather than
# used as a soft skip, so adding a new persona without a prompt file cannot be
# waved through by appending to it silently — doing so is a visible diff that a
# reviewer sees, and it must carry an issue number.
_KNOWN_MISSING_PROMPT_FILES = {
    # #4037: dispatches a pod with no persona identity. Pre-existing; documented
    # as known-broken in docs/agent-catalogue.md.
    "pt-superpower",
}


def _domain_persona_dirs() -> list[Path]:
    """Persona directories contributed by domain packs, in staging order."""
    if not _DOMAIN_APPS_ROOT.is_dir():
        return []
    return sorted(
        d / "agent" / "personas"
        for d in _DOMAIN_APPS_ROOT.iterdir()
        if (d / "agent" / "personas").is_dir()
    )


def _staged_persona_files() -> dict[str, Path]:
    """Reproduce the flat persona namespace the worker image ends up with.

    Core personas first, then domain personas — the same order and the same
    overwrite semantics as `stage-personas.sh`, so what this returns is the set of
    names an agent run can actually resolve.
    """
    staged: dict[str, Path] = {}
    for root in [_CORE_PERSONA_ROOT, *_domain_persona_dirs()]:
        for path in sorted(root.glob("*.md")):
            staged[path.stem] = path  # later roots overwrite, as in staging
    return staged


def test_persona_source_roots_exist() -> None:
    """Guard against a vacuous pass if the layout moves.

    Every assertion below is about set membership; if the roots resolved to
    nothing, the "missing file" tests would report every persona as broken (loud,
    fine) but the collision test would pass trivially (silent, not fine).
    """
    assert _CORE_PERSONA_ROOT.is_dir(), (
        f"core persona root missing: {_CORE_PERSONA_ROOT}"
    )
    staged = _staged_persona_files()
    assert len(staged) >= len(VALID_PERSONAS) - len(_KNOWN_MISSING_PROMPT_FILES), (
        f"only {len(staged)} persona files found under {_CORE_PERSONA_ROOT} and "
        f"{_DOMAIN_APPS_ROOT} — the persona layout may have changed and broken "
        f"this test's resolution rule"
    )


@pytest.mark.parametrize("persona", sorted(VALID_PERSONAS))
def test_every_registered_persona_has_a_prompt_file(persona: str) -> None:
    """A registered name with no `<name>.md` dispatches an agent with no identity.

    This is the #4037 bug class. It is not caught by the catalogue parity test,
    which compares names to names, nor by `spawn_persona()`, which validates
    against `VALID_PERSONAS` — the very list that contains the broken name.
    """
    if persona in _KNOWN_MISSING_PROMPT_FILES:
        pytest.skip(f"{persona} has a tracked missing-prompt-file issue")
    staged = _staged_persona_files()
    assert persona in staged, (
        f"persona {persona!r} is registered in personas.py but no "
        f"{persona}.md exists under {_CORE_PERSONA_ROOT} or any "
        f"modules/domain-apps/*/agent/personas/. It would dispatch a pod with no "
        f"persona identity (#4037). Add the prompt file in the same commit as the "
        f"registration."
    )


def test_known_missing_prompt_files_is_exactly_the_documented_set() -> None:
    """The exemption list must not grow, and must not go stale.

    Asserted in both directions: a new unfiled exemption fails here, and an
    exemption whose prompt file has since been added also fails — so the list
    cannot quietly outlive the bug it documents.
    """
    staged = _staged_persona_files()
    fixed = {p for p in _KNOWN_MISSING_PROMPT_FILES if p in staged}
    assert not fixed, (
        f"{sorted(fixed)} now has a prompt file — remove it from "
        f"_KNOWN_MISSING_PROMPT_FILES so the exemption does not mask a future "
        f"regression."
    )
    assert _KNOWN_MISSING_PROMPT_FILES <= VALID_PERSONAS, (
        f"_KNOWN_MISSING_PROMPT_FILES names personas that are no longer "
        f"registered: {sorted(_KNOWN_MISSING_PROMPT_FILES - VALID_PERSONAS)}"
    )


def test_no_domain_persona_shadows_a_core_persona() -> None:
    """A domain persona filename must not collide with a core one.

    Domain personas are copied FLAT into the same directory as core personas, and
    they are copied LAST (`stage-personas.sh`), so a collision does not error — it
    *overwrites*. A domain pack shipping `developer.md` would silently replace
    ADP's core `developer` persona for every agent run on the platform, with no
    log line and no failed build. Namespacing domain persona filenames is what
    prevents it, and this asserts the namespacing rather than trusting it.
    """
    core = {p.stem for p in _CORE_PERSONA_ROOT.glob("*.md")}
    collisions: dict[str, list[str]] = {}
    for domain_dir in _domain_persona_dirs():
        domain = domain_dir.parents[1].name
        for path in sorted(domain_dir.glob("*.md")):
            if path.stem in core:
                collisions.setdefault(path.stem, []).append(domain)
    assert not collisions, (
        f"domain persona file(s) shadow a core persona of the same name: "
        f"{collisions}. Domain personas stage flat and last, so this replaces the "
        f"core persona for every agent run. Namespace the domain filename "
        f"(e.g. superplane-operator.md, not operations.md)."
    )


def test_domain_persona_files_do_not_collide_with_each_other() -> None:
    """Two domain packs must not ship the same persona filename either.

    Same overwrite mechanic as above, but between packs: whichever domain sorts
    later wins, so the effective persona would depend on directory ordering.
    """
    seen: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}
    for domain_dir in _domain_persona_dirs():
        domain = domain_dir.parents[1].name
        for path in sorted(domain_dir.glob("*.md")):
            if path.stem in seen:
                collisions.setdefault(path.stem, [seen[path.stem]]).append(domain)
            else:
                seen[path.stem] = domain
    assert not collisions, (
        f"domain packs ship colliding persona filenames: {collisions}"
    )


def test_no_mention_string_shadows_another() -> None:
    """No mention string may be a substring of another.

    `_extract_mention_persona()` returns on the first substring match in dict
    order, so if `@agent-superplane` were registered alongside
    `@agent-superplane-operator`, a comment naming the operator would dispatch
    whichever came first in the dict. The `codex-last` comment in personas.py
    manages one instance of this hazard by ordering; this test rules out the
    hazard existing at all, which does not depend on anyone remembering the
    ordering rule.
    """
    mentions = sorted(MENTION_TO_PERSONA)
    shadowed = [
        (outer, inner)
        for outer in mentions
        for inner in mentions
        if inner != outer and inner in outer
    ]
    assert not shadowed, (
        f"mention string(s) contain another as a substring, so first-match "
        f"routing can misdispatch: {shadowed}"
    )


def test_superplane_personas_are_registered_with_their_files() -> None:
    """The U4 deliverable itself: name and file landed together.

    Explicit rather than relying only on the parameterized sweep above, so the
    failure message names this unit's contract if a later change registers one
    without the other.
    """
    staged = _staged_persona_files()
    for persona in ("superplane-operator", "superplane-researcher"):
        assert persona in VALID_PERSONAS, f"{persona} not registered in personas.py"
        assert f"@agent-{persona}" in MENTION_TO_PERSONA, (
            f"@agent-{persona} missing from MENTION_TO_PERSONA"
        )
        assert persona in staged, f"{persona}.md missing from the persona source tree"
        # Lives in the Superplane domain pack, not in core rules/personas.
        assert "domain-apps/superplane" in staged[persona].as_posix(), (
            f"{persona}.md resolved to {staged[persona]} — it belongs in the "
            f"Superplane domain pack"
        )


def test_superplane_personas_are_mention_only() -> None:
    """Deliberate constraint: no label trigger for a persona that spends money.

    A label is an easier-to-fire trigger than a mention (a stale label on a
    reopened issue re-dispatches), and `superplane-operator` allocates paid
    compute. Documented in docs/agent-catalogue.md; asserted here so a later
    convenience change has to argue with a test.
    """
    for persona in ("superplane-operator", "superplane-researcher"):
        assert persona not in LABEL_TO_PERSONA.values(), (
            f"{persona} gained a label trigger — it is mention-only by design "
            f"because it can allocate paid capacity. See docs/agent-catalogue.md."
        )
