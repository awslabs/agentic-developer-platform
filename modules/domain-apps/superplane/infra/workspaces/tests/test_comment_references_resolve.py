"""Comments that cite a test file must cite one that exists — Issue #5532 (w6-09), AC-01.

## The defect this caught

Four comments in this module pointed at test files that were never written:

  * `versions.tf` and `outputs.tf` cited `tests/test_backend_state_key.py`
  * `main.tf` cited `tests/test_target_binding.py` for the target-mismatch case
  * `tests/backend.tftest.hcl` cited `target_binding.tftest.hcl`

Two of those turned out to be mislabelled citations of tests that DO exist: the target
mismatch is covered by the run block
"a_named_account_that_disagrees_with_the_credentials_is_refused" in
`no_inherited_defaults.tftest.hcl`, so those comments were repointed.

The third was a genuinely missing test, and the distinction matters. `versions.tf`'s claim
was that the backend block "stays variable-free" — a property `terraform test` structurally
CANNOT check, because Terraform resolves the backend at `init`, before any test runs, and it
never enters the plan graph. Repointing that comment at `backend.tftest.hcl` would have cited
a file incapable of supporting it. `test_workspace_backend_state_key.py` was written instead
(named for this module, because `test_backend_state_key.py` is the control plane's and two
same-named test files cannot both be collected).

This module's comments are load-bearing: they are how a reviewer decides a safety property is
enforced rather than merely intended, and "see tests/x.py" is the evidence. A pointer to a
nonexistent file reads as proof while providing none — and the reader who goes looking either
concludes the check is missing when it is not, or trusts the citation and never looks.

Fixing the four by hand fixes today. This test is what stops the fifth, since the same
pressure applies every time a test file is renamed or a suite reorganised.

## What is checked, and the deliberate limit

Every `*.tf` file and `*.tftest.hcl` file is scanned for things that look like references to
a Python or Terraform test file, and each must resolve to a real path — tried relative to
the citing file's directory and to the module root, since both conventions are used and both
are legitimate.

It does NOT check that the cited file tests what the comment claims. No offline check can.
Naming a real file that proves something else is still possible; this only removes the
cheaper failure of naming nothing at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
SUPERPLANE_INFRA = Path(__file__).resolve().parents[2]

# Reference shapes used in this module's prose, e.g. `tests/backend.tftest.hcl`,
# `../scripts/workspace_ownership.py`, `../../control-plane/tests/test_platform_isolation.py`.
REFERENCE = re.compile(r"[\w./-]*(?:tests?/)?[\w-]+\.(?:tftest\.hcl|py)\b")

# Referenced paths that intentionally do not resolve on disk.
#
# Empty, and that is the point: every citation in this module currently resolves. An entry
# here needs a comment saying why a reference to a nonexistent file is correct, which is a
# high enough bar that the honest move is usually to fix the reference instead.
KNOWN_UNRESOLVABLE: frozenset[str] = frozenset()


def _source_files() -> list[Path]:
    return sorted([*MODULE.glob("*.tf"), *MODULE.glob("tests/*.tftest.hcl")])


def _citations() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for path in _source_files():
        for line in path.read_text().splitlines():
            # Comments only. A `source = "./mod"` or a filename in a heredoc payload is not
            # a claim about evidence, and this test is about claims.
            stripped = line.strip()
            if not stripped.startswith("#"):
                continue
            for match in REFERENCE.findall(stripped):
                if match not in KNOWN_UNRESOLVABLE:
                    found.append((path, match))
    return found


CITATIONS = _citations()


def test_the_scan_found_citations() -> None:
    """Premise check: zero citations would make the test below vacuously green."""
    assert len(CITATIONS) >= 8, (
        f"found only {len(CITATIONS)} test-file citations in this module's comments, which "
        f"suggests the scanner stopped matching rather than that the comments stopped "
        f"citing. This module documents its safety properties by pointing at the tests "
        f"that enforce them; there should be many."
    )


@pytest.mark.parametrize(
    ("citing_file", "reference"),
    CITATIONS,
    ids=[f"{p.name}->{r}" for p, r in CITATIONS],
)
def test_every_cited_test_file_exists(citing_file: Path, reference: str) -> None:
    """A comment's "see tests/x" must resolve, or it is evidence of nothing."""
    candidates = [
        (citing_file.parent / reference).resolve(),
        (MODULE / reference).resolve(),
        (MODULE / "tests" / Path(reference).name).resolve(),
    ]
    if any(c.exists() for c in candidates):
        return

    # Prose often names a file in short form after giving its path once — "widening
    # `domain_ownership.py`" a line below "`../scripts/domain_ownership.py`". A bare
    # basename with no directory part is accepted if it resolves anywhere under the sibling
    # infra modules, since that is a back-reference to a path the comment already gave.
    #
    # A reference WITH a directory part is not given that latitude, and that restriction was
    # itself found by a mutation test. The unrestricted version was vacuous: reintroducing
    # `tests/test_backend_state_key.py` in outputs.tf was NOT caught, because a file by that
    # name really exists — in ../control-plane/tests/. So this module's comment resolved
    # against a SIBLING MODULE's test, which cannot assert anything about this module's
    # backend block. "A file by this name exists somewhere" is not evidence; "the file this
    # path names exists" is.
    #
    # Relative paths in this module's comments are written from the MODULE ROOT, so
    # `../scripts/...` and `../account-factory/...` resolve via the second candidate above.
    if "/" not in reference and any(SUPERPLANE_INFRA.rglob(reference)):
        return

    raise AssertionError(
        f"{citing_file.name} cites `{reference}`, which does not exist.\n\n"
        f"Tried:\n"
        + "".join(f"  - {c}\n" for c in candidates)
        + (
            f"  - any file named {reference} under {SUPERPLANE_INFRA}\n"
            if "/" not in reference
            else f"  - (no basename fallback: `{reference}` has a directory part, so it "
            f"must resolve exactly — a same-named file in another module is not "
            f"evidence about this one)\n"
        )
        + "\nThis module's comments are how a reviewer decides a safety property is "
        "enforced rather than merely intended, so a pointer to a missing file reads as "
        "proof while providing none.\n\n"
        "Either repoint it at the file that really covers the property — naming the run "
        "block too, if it is a `.tftest.hcl` — or write the test. If the citation is "
        "genuinely meant to name something outside this module, add it to "
        "KNOWN_UNRESOLVABLE with the reason."
    )
