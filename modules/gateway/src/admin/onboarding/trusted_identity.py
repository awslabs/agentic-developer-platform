"""Complete immutable GitHub identity used by onboarding (#5666 / A11).

A platform-written login is only a mutable routing hint. The authenticated
subject must resolve to one Cognito broker record with a numeric GitHub username;
provider membership responses must confirm that same immutable ID before writes.
Editable profile attributes never establish identity. Missing proof suppresses
new membership grants without removing existing native/bootstrap access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Written ONLY by the platform, after a completed provider handshake:
#   - lambda/github-auth-broker/cognito_provisioner.py (AdminCreateUser /
#     AdminUpdateUserAttributes following the GitHub OAuth exchange)
#   - src/admin/cognito_service.py (administrative provisioning)
# Absent from every app client's write_attributes, so a user cannot set it.
TRUSTED_LOGIN_ATTRIBUTE: Final[str] = "custom:github_username"

# Mirrors write_attributes on the PUBLIC (SPA) and CLI app clients in
# infra/modules/cognito/main.tf. These are attributes a signed-in user may set
# about THEMSELVES, so none of them may feed identity or role resolution.
SELF_WRITABLE_ATTRIBUTES: Final[frozenset[str]] = frozenset({"email", "name"})

# The invariant this module exists to hold, asserted at import so a future edit
# that adds a self-writable attribute to the trusted position cannot start.
assert TRUSTED_LOGIN_ATTRIBUTE not in SELF_WRITABLE_ATTRIBUTES, (
    f"{TRUSTED_LOGIN_ATTRIBUTE} is self-writable; a user-editable attribute must never drive membership or role resolution"
)


@dataclass(frozen=True)
class TrustedGitHubIdentity:
    """A GitHub identity the platform itself established, or the absence of one.

    ``linked`` means the resolver obtained both identity halves from the same
    subject-bound Cognito broker record. Membership responses still have to
    confirm the numeric ID before it can grant organization access.

    ``login`` and ``numeric_id`` are empty strings when ``linked`` is False, so a
    caller that forgets to check ``linked`` still cannot accidentally match a real
    GitHub account — it matches nothing.
    """

    login: str = ""
    numeric_id: str = ""
    linked: bool = False

    @property
    def complete(self) -> bool:
        """Both membership and identity writes require an immutable numeric ID."""
        return self.linked and bool(self.login) and self.numeric_id.isascii() and self.numeric_id.isdecimal()


NO_LINKED_IDENTITY: Final[TrustedGitHubIdentity] = TrustedGitHubIdentity()


def trusted_login_from_attributes(attributes: dict[str, str]) -> str:
    """Pull the platform-written GitHub login out of Cognito attributes.

    The single place the attribute name is read, so there is no second source of
    the login. Returns ``""`` when the platform has not written one — which is
    NOT the same as "fall back to something else", and deliberately offers the
    caller nothing to fall back to.
    """
    return (attributes.get(TRUSTED_LOGIN_ATTRIBUTE) or "").strip()
