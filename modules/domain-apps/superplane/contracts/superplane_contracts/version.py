"""Version discipline for the observation contracts.

Issue #5043 (U8), EPIC #4910.

Every submission carries the contract version it was written against, and a
receiver that does not implement that version refuses the submission rather than
interpreting the parts it recognizes. That is the whole rule, and it exists
because the alternative failure is silent: a sender adds a field, a receiver
ignores it, and the observation is *accepted* while meaning something different
at each end. Nothing in the system would report that as an error — the fleet
surface would simply be subtly wrong, which is the one outcome a health surface
cannot survive.

Refusing is therefore the safe direction here even though it drops data. A
dropped observation is visible (the submitter gets a refusal and can be alerted
on); a misread one is not.

The version lives in **both** the transport header and the payload, and
`check_version` requires them to agree. Duplication is intentional: the header
lets a receiver reject before parsing a body, and the payload copy survives being
logged, replayed from a queue or written to an event store, where the header does
not. A mismatch between them means something rewrote one of the two, so it is
refused rather than resolved by preferring either.
"""

from __future__ import annotations

from dataclasses import dataclass

# The version this package defines. A breaking change to any wire shape in this
# package increments this and adds the new value to SUPPORTED_VERSIONS; the old
# value stays supported for as long as a deployed submitter still sends it.
CONTRACT_VERSION = "v1"

# Versions a receiver implementing this package accepts. A frozenset rather than
# a comparison against CONTRACT_VERSION because supporting two versions during a
# sender rollout is normal, and an ordering comparison would silently accept a
# future version this code has never seen.
SUPPORTED_VERSIONS: frozenset[str] = frozenset({CONTRACT_VERSION})

# Wire key and header carrying the version. Named here so a submitter, a receiver
# and the JSON schema all read the same constant instead of three string literals
# that can drift apart.
VERSION_FIELD = "contract_version"
VERSION_HEADER = "x-superplane-contract-version"


@dataclass(frozen=True)
class VersionCheck:
    """Outcome of the version check.

    `reason` is a stable, caller-safe string. It names what was wrong with the
    version and nothing else — it never echoes payload content back, so a
    refusal cannot be used as a read primitive against the receiver.
    """

    accepted: bool
    version: str | None = None
    reason: str = ""


def check_version(header_value: object, payload_value: object) -> VersionCheck:
    """Decide whether a submission's contract version is acceptable.

    Fail-closed at every branch: absent, blank, unknown and disagreeing versions
    all refuse. There is no branch that accepts a submission because a version
    was missing and something plausible could be assumed — an assumed version is
    exactly the silent-disagreement failure this module exists to prevent.
    """
    header = header_value.strip() if isinstance(header_value, str) else ""
    payload = payload_value.strip() if isinstance(payload_value, str) else ""

    if not header and not payload:
        return VersionCheck(accepted=False, reason="missing contract version")
    if not header:
        return VersionCheck(
            accepted=False, reason=f"missing contract version header: {VERSION_HEADER}"
        )
    if not payload:
        return VersionCheck(
            accepted=False, reason=f"missing contract version field: {VERSION_FIELD}"
        )
    if header != payload:
        # Deliberately does not say which one is "right". Preferring either would
        # let whichever surface is easier to tamper with decide the version.
        return VersionCheck(accepted=False, reason="contract version mismatch")
    if header not in SUPPORTED_VERSIONS:
        return VersionCheck(
            accepted=False, reason=f"unsupported contract version: {header}"
        )

    return VersionCheck(accepted=True, version=header)
