"""The graph address grammar: `flow/epic/wave/node`, declared exactly once.

Extracted from `proposal.py` by #5128, and the extraction is forced rather than
cosmetic. `execution_policy.py` constrains the keys of its per-evaluation
acceptance map to real graph addresses, and `proposal.py` carries an
`ExecutionPolicy` field — so with the pattern living in `proposal.py` the two
modules would import each other.

The alternative was a second copy of the regex in `execution_policy.py`, which
R-N2a forbids outright: a vocabulary declared twice is a requirement violation,
not a style preference, and the existing run-status sets drifted three ways inside
a set whose own comment claimed drift was impossible. A duplicated *address
grammar* would drift the same way and fail worse — two modules disagreeing about
what a valid address is means a policy entry that can never match the node it
names, with nothing to report the mismatch.

`proposal.py` re-exports `ADDRESS_PATTERN`, so every existing importer keeps
working and this move is invisible to them.
"""

from __future__ import annotations

import re

__all__ = ["ADDRESS_PATTERN", "split_address"]


# A graph address is `flow/epic/wave/node` — exactly four non-empty segments
# (D-R13). Segments allow word characters, dots and hyphens: enough for slugs and
# issue refs, and deliberately not `/`, which would let one segment forge two and
# make a three-segment address parse as four.
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
ADDRESS_PATTERN = re.compile(rf"^{_SEGMENT}/{_SEGMENT}/{_SEGMENT}/{_SEGMENT}$")


def split_address(address: str) -> tuple[str, str, str, str]:
    """Split a validated graph address into its four segments.

    Raises:
        ValueError: If `address` is not of the form `flow/epic/wave/node`. Callers
            that have already run `validate_proposal` cannot hit this; the raise
            exists so a caller that skipped validation fails loudly here rather
            than writing a malformed address to the store.
    """
    if not ADDRESS_PATTERN.match(address):
        raise ValueError(f"not a graph address of the form 'flow/epic/wave/node': {address!r}")
    flow, epic, wave, node = address.split("/")
    return flow, epic, wave, node
