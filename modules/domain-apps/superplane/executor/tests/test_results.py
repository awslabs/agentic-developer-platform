import json

import pytest
from harness_jobs.identity import OperationRefused
from superplane_executor.results import result_text


@pytest.mark.parametrize(
    "message",
    [
        "broken",
        "[]",
        '{"text":"without version"}',
        '{"superplane_result_version":true,"text":"wrong version"}',
        json.dumps({"superplane_result_version": 1, "text": "x" * 4096}),
        json.dumps({"superplane_result_version": 1, "text": "\x00"}),
    ],
)
def test_invalid_or_potentially_truncated_results_are_refused(message):
    with pytest.raises(OperationRefused):
        result_text(message)


def test_result_is_plain_text_with_secret_patterns_redacted():
    content, changed = result_text(
        json.dumps(
            {
                "superplane_result_version": 1,
                "text": '<script>plain text</script>\n"token": "secret value"\nBearer credential\n'
                "-----BEGIN PRIVATE KEY-----\ntruncated-key",
            }
        )
    )
    assert changed
    assert "<script>plain text</script>" in content
    assert (
        "secret value" not in content
        and "credential" not in content
        and "truncated-key" not in content
    )
    assert result_text("") is None


@pytest.mark.parametrize(
    "text",
    [
        "authorization: Bearer sensitive-value",
        '"authorization": "Bearer sensitive-value"',
        "access_token=sensitive-value",
        "Bearer sensitive-value",
    ],
)
def test_bearer_value_cannot_survive_header_prefix_redaction(text):
    content, changed = result_text(
        json.dumps({"superplane_result_version": 1, "text": text})
    )
    assert changed and "sensitive-value" not in content


def test_bidi_controls_are_removed_without_destroying_line_breaks():
    assert result_text(
        json.dumps({"superplane_result_version": 1, "text": "value\u202e\n\t0.95"})
    ) == ("value\n\t0.95", True)
