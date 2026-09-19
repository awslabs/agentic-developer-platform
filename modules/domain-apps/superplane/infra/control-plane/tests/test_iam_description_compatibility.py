"""Keep IAM role descriptions inside AWS IAM's accepted character set.

AWS validates role descriptions more narrowly than Terraform validates strings.  In
particular, typographic punctuation such as an em dash reaches the provider successfully
but makes ``CreateRole`` fail during apply.  Exercise the literal descriptions in this
module before another live apply discovers the mismatch.
"""

from __future__ import annotations

import re
from pathlib import Path

CONTROL_PLANE = Path(__file__).resolve().parents[1]
IAM_ROLE_DESCRIPTION = re.compile(
    r'resource\s+"aws_iam_role"\s+"(?P<name>[^"]+)"\s*\{.*?'
    r'^\s*description\s*=\s*"(?P<description>[^"]*)"',
    re.MULTILINE | re.DOTALL,
)


def _unsupported_iam_description_characters(description: str) -> set[str]:
    """Return characters excluded by IAM's role-description API constraint."""
    return {
        character
        for character in description
        if not (
            character in "\t\n\r"
            or "\u0020" <= character <= "\u007e"
            or "\u00a1" <= character <= "\u00ff"
        )
    }


def test_iam_role_descriptions_use_aws_supported_characters() -> None:
    descriptions: list[tuple[Path, str, str]] = []
    for path in sorted(CONTROL_PLANE.glob("*.tf")):
        for match in IAM_ROLE_DESCRIPTION.finditer(path.read_text(encoding="utf-8")):
            descriptions.append((path, match["name"], match["description"]))

    assert descriptions, "expected at least one aws_iam_role description to validate"
    for path, role_name, description in descriptions:
        unsupported = _unsupported_iam_description_characters(description)
        assert not unsupported, (
            f"{path.name}: aws_iam_role.{role_name} description contains characters IAM "
            f"rejects: {sorted(unsupported)!r}"
        )


def test_guard_rejects_typographic_em_dash() -> None:
    assert _unsupported_iam_description_characters("separate \u2014 see irsa.tf") == {
        "\u2014"
    }
