"""CLI device-authorization requests.

Web-based CLI login ("bg-cognito-auth.sh login --web"): the CLI creates a
pending request, the signed-in browser user approves it by short user_code,
and the CLI redeems it by (hashed) device_code to receive tokens minted on
the CLI-specific Cognito app client. Rows are short-lived (minutes) and
single-use.

Only a SHA-256 hash of the device_code is stored: the plaintext is the
bearer capability the CLI polls with, so a DB read must not be enough to
redeem someone else's login.
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, new_uuid, utcnow


class CliAuthRequest(Base):
    """One pending/approved/denied CLI login attempt.

    Lifecycle: pending → approved (browser) → consumed (CLI poll mints
    tokens exactly once), or pending → denied, or expiry in any state.
    """

    __tablename__ = "cli_auth_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # Short human-checkable code shown in the terminal and in the approval
    # page URL. Unique among live rows; uniqueness enforced app-side by the
    # expiry window + alphabet size, and by index for lookups.
    user_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # SHA-256 hex of the device_code the CLI holds. Never the plaintext.
    device_code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # Set on approval: the Cognito username/sub of the browser user who
    # approved — the identity the minted tokens will belong to.
    approved_username: Mapped[str | None] = mapped_column(String(255))
    approved_sub: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
