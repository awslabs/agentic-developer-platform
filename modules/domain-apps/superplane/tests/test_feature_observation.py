"""Offline regressions for U1 API evidence; fixtures never establish live acceptance."""

import json
import os
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from pathlib import Path

import pytest

from superplane_acceptance import features as f


@pytest.fixture
def config(tmp_path):
    return f.settings(
        {
            "SUPERPLANE_LIVE_ENVIRONMENT": "embark1/dev",
            "SUPERPLANE_LIVE_FEATURES_EVIDENCE_FILE": str(
                tmp_path / "observation.json"
            ),
        }
    )


@pytest.fixture
def payload():
    return json.loads(f.FIXTURE.read_bytes())


def test_current_values_may_differ_from_defaults_and_gain_fields(config, payload):
    payload["features"]["chat"] = False
    payload["features"]["future_feature"] = True
    report = f.observe(config, lambda _: json.dumps(payload).encode())
    assert report["evidence_kind"] == "offline-fixture"
    assert report["status"] == "matched"
    assert report["u1_acceptance"] == "incomplete"
    assert report["http_status"] is None
    assert report["checks"]["superplane_disabled"] is True
    assert not Path(config["evidence_file"]).exists()


@pytest.mark.parametrize("field", f.REQUIRED_FIELDS)
@pytest.mark.parametrize("invalid", [None, "false", 0, [], {}])
def test_required_fields_must_be_booleans(config, payload, field, invalid):
    payload["features"][field] = invalid
    with pytest.raises(f.EvidenceError, match="absent or non-boolean"):
        f.observe(config, lambda _: json.dumps(payload).encode())


@pytest.mark.parametrize("field", ["superplane", "new_ui"])
def test_required_and_fixture_subset_keys_cannot_disappear(config, payload, field):
    del payload["features"][field]
    with pytest.raises(f.EvidenceError):
        f.observe(config, lambda _: json.dumps(payload).encode())


def test_enabled_superplane_fails_default_off_observation(config, payload):
    payload["features"]["superplane"] = True
    with pytest.raises(f.EvidenceError, match="Superplane is enabled"):
        f.observe(config, lambda _: json.dumps(payload).encode())


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"{}",
        b'{"features": false}',
        b'{"features":{},"features":{}}',
        b'{"features":{"superplane":true,"superplane":false}}',
        b'{"features":{"superplane":NaN}}',
        b"<html>secret error response</html>",
        b"\xff",
        b" " * (f.MAX_BYTES + 1),
    ],
)
def test_malformed_or_oversized_responses_fail_without_echoing_body(config, body):
    with pytest.raises(f.EvidenceError) as error:
        f.observe(config, lambda _: body)
    assert "secret error response" not in str(error.value)


def test_extra_response_values_are_not_recorded(config, payload):
    payload["private"] = "secret-body-sentinel"
    payload["features"]["unknown_secret_key"] = "secret-body-sentinel"
    report = f.observe(config, lambda _: json.dumps(payload).encode())
    assert "secret-body-sentinel" not in json.dumps(report)
    assert "unknown_secret_key" not in json.dumps(report)


@pytest.mark.parametrize("key", ["origin", "account", "region", "environment"])
def test_target_mismatch_refuses_before_transport(config, key):
    config[key] = "foreign-target"
    calls = []
    with pytest.raises(f.EvidenceError, match="reviewed environment"):
        f.observe(config, lambda url: calls.append(url))
    assert calls == []


def test_modified_fixture_refuses_before_transport(config, tmp_path, monkeypatch):
    changed = tmp_path / "fixture.json"
    changed.write_text('{"features":{"superplane":false}}')
    monkeypatch.setattr(f, "FIXTURE", changed)
    calls = []
    with pytest.raises(f.EvidenceError, match="fixture changed"):
        f.observe(config, lambda url: calls.append(url))
    assert calls == []


class Response:
    status = 200

    def __init__(self, url, body):
        self.url, self.body = url, body
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["Date"] = format_datetime(datetime.now(timezone.utc), usegmt=True)
        self.limit = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        self.limit = limit
        return self.body[:limit]


@pytest.fixture
def boundary(config, monkeypatch):
    url = config["origin"] + "/api/features?observation=" + "a" * 32
    response = Response(url, f.FIXTURE.read_bytes())
    calls, handlers = [], []

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return response

    def build(*args):
        handlers.extend(args)
        return Opener()

    monkeypatch.setattr(f.urllib.request, "build_opener", build)
    monkeypatch.setenv(f.TOKEN_VARIABLE, "test-only-token-sentinel")
    return url, response, calls, handlers


def test_http_boundary_get_only_bounded_direct_and_redirect_protected(boundary):
    url, response, calls, handlers = boundary
    assert f.HttpsFeatures()(url) == f.FIXTURE.read_bytes()
    request, timeout = calls[0]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.get_header("Authorization") == "Bearer test-only-token-sentinel"
    assert request.get_header("Cache-control") == "no-cache, no-store"
    assert timeout == 30
    assert response.limit == f.MAX_BYTES + 1
    assert any(isinstance(h, f.NoRedirect) for h in handlers)
    assert any(
        isinstance(h, f.urllib.request.ProxyHandler) and not h.proxies for h in handlers
    )


@pytest.mark.parametrize("header", ["Date", "Age", "Content-Type"])
def test_stale_cached_or_non_json_reads_are_refused(boundary, header):
    url, response, _, _ = boundary
    values = {
        "Date": format_datetime(
            datetime.now(timezone.utc) - timedelta(minutes=10), usegmt=True
        ),
        "Age": "1",
        "Content-Type": "text/html",
    }
    del response.headers[header]
    response.headers[header] = values[header]
    with pytest.raises(f.EvidenceError):
        f.HttpsFeatures()(url)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_denied_or_failed_http_is_not_an_observation(boundary, status):
    url, response, _, _ = boundary
    response.status = status
    with pytest.raises(f.EvidenceError, match="HTTP 200"):
        f.HttpsFeatures()(url)


def test_transport_exception_and_redirect_do_not_expose_secrets(boundary, monkeypatch):
    url, _, _, _ = boundary

    def failure(*args):
        raise urllib.error.URLError("secret-error-sentinel")

    monkeypatch.setattr(f.urllib.request, "build_opener", failure)
    with pytest.raises(f.EvidenceError) as error:
        f.HttpsFeatures()(url)
    assert "secret-error-sentinel" not in str(error.value)
    assert error.value.__suppress_context__
    with pytest.raises(f.EvidenceError) as redirected:
        f.NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://foreign/secret"
        )
    assert "https://foreign/secret" not in str(redirected.value)


@pytest.mark.parametrize(
    "token", ["", "abc\r\nInjected: yes", "with space", "x" * 16_385]
)
def test_missing_or_invalid_token_refuses_before_network(boundary, monkeypatch, token):
    url, _, calls, _ = boundary
    monkeypatch.setenv(f.TOKEN_VARIABLE, token)
    with pytest.raises(f.EvidenceError, match="TOKEN is required"):
        f.HttpsFeatures()(url)
    assert calls == []


def test_foreign_endpoint_refuses_before_network(boundary):
    _, _, calls, _ = boundary
    with pytest.raises(f.EvidenceError, match="Unreviewed"):
        f.HttpsFeatures()(
            "https://foreign.example/api/features?observation=" + "a" * 32
        )
    assert calls == []


@pytest.mark.parametrize("suffix", ["&extra=" + "b" * 32, "#fragment", "z"])
def test_endpoint_cannot_be_extended(boundary, suffix):
    url, _, calls, _ = boundary
    with pytest.raises(f.EvidenceError, match="Unreviewed"):
        f.HttpsFeatures()(url + suffix)
    assert calls == []


@pytest.mark.parametrize("nonce", ["a" * 15 + "=" + "b" * 16, "a" * 31 + "=", "=" * 32])
def test_nonce_must_be_entirely_hex_before_transport(boundary, nonce):
    url, _, calls, _ = boundary
    with pytest.raises(f.EvidenceError, match="Unreviewed"):
        f.HttpsFeatures()(url[:-32] + nonce)
    assert calls == []


def test_missing_timestamp_and_changed_response_endpoint_are_refused(boundary):
    url, response, _, _ = boundary
    response.url = "https://foreign.example/"
    with pytest.raises(f.EvidenceError, match="endpoint changed"):
        f.HttpsFeatures()(url)
    response.url = url
    del response.headers["Date"]
    with pytest.raises(f.EvidenceError, match="authenticated feature read failed"):
        f.HttpsFeatures()(url)


def test_network_body_is_bounded(boundary):
    url, response, _, _ = boundary
    response.body = b"x" * (f.MAX_BYTES + 2)
    with pytest.raises(f.EvidenceError, match="size limit"):
        f.HttpsFeatures()(url)
    assert response.limit == f.MAX_BYTES + 1


def test_existing_evidence_is_untouched_and_no_read_occurs(config, monkeypatch):
    output = Path(config["evidence_file"])
    output.write_text("old-evidence")
    calls = []
    monkeypatch.setattr(f, "observe", lambda _: calls.append(True))
    with pytest.raises(f.EvidenceError, match="new filename"):
        f.run_live(
            {
                "SUPERPLANE_LIVE_ENVIRONMENT": config["environment"],
                "SUPERPLANE_LIVE_FEATURES_EVIDENCE_FILE": str(output),
            }
        )
    assert output.read_text() == "old-evidence"
    assert calls == []


def test_fixture_observation_cannot_be_published_as_live(config, monkeypatch):
    report = f.observe(config, lambda _: f.FIXTURE.read_bytes())
    monkeypatch.setattr(f, "observe", lambda _: report)
    with pytest.raises(f.EvidenceError, match="cannot be published"):
        f.run_live(
            {
                "SUPERPLANE_LIVE_ENVIRONMENT": config["environment"],
                "SUPERPLANE_LIVE_FEATURES_EVIDENCE_FILE": config["evidence_file"],
            }
        )
    assert not Path(config["evidence_file"]).exists()


def test_explicit_live_command_without_inputs_fails_instead_of_skipping():
    root = Path(__file__).parents[4]
    environment = {
        k: v for k, v in os.environ.items() if not k.startswith("SUPERPLANE_LIVE_")
    }
    environment[f.TOKEN_VARIABLE] = "test-token-that-must-not-appear-in-locals"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(Path(__file__).parent / "acceptance/test_u1_features_live.py"),
            "-q",
            "--tb=short",
            "--showlocals",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 1
    assert "BLOCKED: set SUPERPLANE_LIVE_ENVIRONMENT" in result.stdout
    assert "1 failed" in result.stdout
    assert "skipped" not in result.stdout
    assert environment[f.TOKEN_VARIABLE] not in result.stdout + result.stderr
