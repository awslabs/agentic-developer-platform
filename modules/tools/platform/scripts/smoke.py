"""Compatibility exports for target metadata; legacy login smoke is retired."""

if __name__ == "__main__":
    raise SystemExit(
        "Legacy smoke is retired; use native_release with original owner approval and dedicated access login"
    )

from modules.tools.platform.scripts.native_test_session import NoRedirects  # noqa: F401
from modules.tools.platform.scripts.target_metadata import (  # noqa: F401
    target_revision,
    target_revision_values,
)


def main():
    raise ValueError(
        "Legacy smoke is retired; use native_release with original owner approval and dedicated access login"
    )
