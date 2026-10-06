import hashlib
import json
from pathlib import Path

import pytest
import rfc8785

from src.agentauth.chat_model_json import canonical_model_json

VECTORS = json.loads(Path(__file__).with_name("chat_model_digest_vectors.json").read_text())


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_javascript_wire_numbers_match_shared_client_digests(vector):
    wire = vector.get("wireInput", json.dumps(vector["input"]))
    request = {
        "messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bounded_tool", "input": json.loads(wire)}]}],
        "max_tokens": 16,
    }
    assert hashlib.sha256(canonical_model_json(request)).hexdigest() == vector["digest"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (2**53 + 1, b"9007199254740992"),
        (2**64, b"18446744073709552000"),
        (-(2**64), b"-18446744073709552000"),
        (10**21, b"1e+21"),
        (True, b"true"),
        ("9007199254740993", b'"9007199254740993"'),
    ],
)
def test_only_numeric_values_follow_javascript_number_interpretation(value, expected):
    assert canonical_model_json(value) == expected


@pytest.mark.parametrize("value", [10**400, -(10**400), float("inf"), float("-inf"), float("nan"), "\ud800"])
def test_nonfinite_numbers_and_invalid_unicode_remain_refused(value):
    with pytest.raises(rfc8785.CanonicalizationError):
        canonical_model_json({"nested": [value]})
