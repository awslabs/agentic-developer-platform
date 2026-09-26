"""An assertion is reviewed only when the actual non-removable guard dominates it."""

import pytest
import verify_bandit_guarded_assertions as verifier

SOURCE = (
    "from .cli_delivery import EvidenceError, require\n"
    "def inspect_value(value):\n"
    "    require(isinstance(value, str), 'expected text')\n"
    "    assert isinstance(value, str)\n"
    "    return value\n"
)


def test_exact_immediately_preceding_guard():
    proof = verifier.guarded_assertion(SOURCE, 4)
    assert proof["guard_line"] == 3 and proof["function"] == "inspect_value"


def test_first_conjunction_enforces_assertion():
    source = SOURCE.replace("str), 'expected", "str) and value.strip(), 'expected")
    assert verifier.guarded_assertion(source, 4)["guard_line"] == 3


@pytest.mark.parametrize(
    "before,after,line",
    [
        ("require(isinstance(value, str),", "require(isinstance(value, int),", 4),
        ("str), 'expected", "str) or override, 'expected", 4),
        ("def inspect_value(value):", "def inspect_value(value, require):", 4),
        ("    assert", "    value = replacement\n    assert", 5),
        ("from .cli_delivery", "from .unreviewed_helper", 4),
        ("    assert", "    require = replacement\n    assert", 5),
        ("    require(isinstance(value, str), 'expected text')\n", "", 3),
        (
            "    require(isinstance(value, str), 'expected text')\n",
            "    if trusted:\n        require(isinstance(value, str), 'expected text')\n",
            5,
        ),
    ],
)
def test_similar_or_nondominating_guard_is_not_evidence(before, after, line):
    with pytest.raises(ValueError):
        verifier.guarded_assertion(SOURCE.replace(before, after), line)


def test_wrong_original_line_is_not_a_join():
    with pytest.raises(ValueError, match="original assertion"):
        verifier.guarded_assertion(SOURCE, 3)


HELPER = (
    "class EvidenceError(RuntimeError): pass\n"
    "def require(condition, message):\n"
    "    if not condition:\n"
    "        raise EvidenceError(message)\n"
)


def test_explicit_helper_is_nonremovable():
    verifier.verify_helper(HELPER)


@pytest.mark.parametrize(
    "helper",
    [
        "class EvidenceError(RuntimeError): pass\ndef require(condition, message):\n    assert condition, message\n",
        HELPER.replace("raise EvidenceError(message)", "return False"),
        HELPER.replace("if not condition", "if condition"),
        HELPER.replace("RuntimeError", "object"),
    ],
)
def test_helper_must_reject_independently_of_assertions(helper):
    with pytest.raises(ValueError):
        verifier.verify_helper(helper)
