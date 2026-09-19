"""Read U1's authenticated feature API; this is not teardown/live acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4

from .cli_delivery import TARGETS, EvidenceError, require

REQUIRED_FIELDS = (
    "chat",
    "knowledge",
    "indexing",
    "connections",
    "credentials",
    "system_dashboard",
    "logs",
    "gitlab",
    "orchestration_engine",
    "budget_spend",
    "agent_control",
    "superplane",
)
FIXTURE = Path(__file__).parents[1] / "tests/fixtures/features.json"
# Reviewed backend-default fixture from #5290; changing it requires review.
FIXTURE_SHA256 = "462aa23947ed246e08cf63cfb87d2edb6b7de322516bd746c1f97825b53be2e3"
MAX_BYTES = 65_536
TOKEN_VARIABLE = "SUPERPLANE_LIVE_ADP_TOKEN"


def settings(environment) -> dict:
    __tracebackhide__ = True
    names = (
        "SUPERPLANE_LIVE_ENVIRONMENT",
        "SUPERPLANE_LIVE_FEATURES_EVIDENCE_FILE",
    )
    require(
        all(environment.get(name) for name in names),
        "BLOCKED: set SUPERPLANE_LIVE_ENVIRONMENT and "
        "SUPERPLANE_LIVE_FEATURES_EVIDENCE_FILE",
    )
    target, output = (environment[name] for name in names)
    require(target in TARGETS, "BLOCKED: target is not in the reviewed registry")
    path = Path(output)
    require(
        path.is_absolute() and path.parent.is_dir() and not os.path.lexists(path),
        "BLOCKED: evidence needs an absolute new filename in an existing directory",
    )
    # Never retain a token or the rest of the process environment in config/evidence.
    return {"environment": target, "evidence_file": output, **TARGETS[target]}


def parse_json(content: bytes) -> dict:
    __tracebackhide__ = True

    def pairs(items):
        __tracebackhide__ = True
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON field in feature evidence")
            result[key] = value
        return result

    def constant(_value):
        __tracebackhide__ = True
        raise EvidenceError("Non-JSON numeric constant in feature evidence")

    try:
        value = json.loads(content, object_pairs_hook=pairs, parse_constant=constant)
    except EvidenceError as exc:
        # Callback failures otherwise retain json.decoder frames containing the body.
        raise EvidenceError(str(exc)) from None
    except (ValueError, UnicodeError, RecursionError):
        raise EvidenceError("Malformed feature JSON; response withheld") from None
    require(type(value) is dict, "Feature response must be a JSON object")
    require(type(value.get("features")) is dict, "Missing feature object")
    return value["features"]


def fixture_fields() -> tuple[str, ...]:
    __tracebackhide__ = True
    try:
        content = FIXTURE.read_bytes()
    except OSError:
        raise EvidenceError("BLOCKED: reviewed features fixture unavailable") from None
    require(
        hashlib.sha256(content).hexdigest() == FIXTURE_SHA256,
        "Reviewed features fixture changed; review provenance before updating its pin",
    )
    return tuple(parse_json(content))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        __tracebackhide__ = True
        # Never follow a Location or print it: it may contain sensitive data.
        raise EvidenceError("Feature request redirected; credential forwarding refused")


class HttpsFeatures:
    def __call__(self, url: str) -> bytes:
        __tracebackhide__ = True
        # Recheck at the credential boundary even if a caller bypassed settings().
        allowed = [t["origin"] + "/api/features?observation=" for t in TARGETS.values()]
        require(
            any(
                url.startswith(prefix)
                and re.fullmatch(r"[0-9a-f]{32}", url[len(prefix) :]) is not None
                for prefix in allowed
            ),
            "Unreviewed feature endpoint; request refused",
        )
        token = os.environ.get(TOKEN_VARIABLE, "")
        require(
            1 <= len(token) <= 16_384 and all(32 < ord(c) < 127 for c in token),
            "BLOCKED: an existing valid SUPERPLANE_LIVE_ADP_TOKEN is required",
        )
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/json",
                "Cache-Control": "no-cache, no-store",
            },
        )
        started = datetime.now(timezone.utc)
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect()
            )
            with opener.open(request, timeout=30) as response:
                require(
                    response.status == 200, "Feature endpoint did not return HTTP 200"
                )
                require(
                    response.geturl() == url, "Feature endpoint changed during request"
                )
                require(
                    response.headers.get_content_type() == "application/json",
                    "Feature endpoint did not return application/json",
                )
                # A nonce and no-cache request are not enough if a proxy ignores them.
                date = parsedate_to_datetime(response.headers.get("Date", ""))
                require(
                    date.tzinfo is not None
                    and abs((started - date).total_seconds()) <= 120,
                    "Feature response timestamp is stale or invalid",
                )
                require(
                    response.headers.get("Age", "0").strip() == "0",
                    "Cached feature response cannot establish the current flag",
                )
                content = response.read(MAX_BYTES + 1)
        except EvidenceError as exc:
            # Detach urllib/callback frames, which can retain a refused Location.
            raise EvidenceError(str(exc)) from None
        except Exception:
            # urllib errors can include request/response data; suppress their chain.
            raise EvidenceError("BLOCKED: authenticated feature read failed") from None
        require(len(content) <= MAX_BYTES, "Feature response exceeds the size limit")
        return content


def observe(config: dict, fetch=None) -> dict:
    __tracebackhide__ = True
    selected = TARGETS.get(config.get("environment"))
    require(
        selected is not None and all(config.get(k) == v for k, v in selected.items()),
        "Selected target differs from the reviewed environment registry",
    )
    expected = fixture_fields()
    transport = HttpsFeatures() if fetch is None else fetch
    live = type(transport) is HttpsFeatures
    started = datetime.now(timezone.utc).isoformat()
    content = transport(config["origin"] + "/api/features?observation=" + uuid4().hex)
    require(
        type(content) is bytes and len(content) <= MAX_BYTES, "Invalid feature response"
    )
    fields = parse_json(content)
    require(
        all(type(fields.get(key)) is bool for key in REQUIRED_FIELDS),
        "A required feature field is absent or non-boolean",
    )
    require(
        fields["superplane"] is False, "Superplane is enabled; default-off check failed"
    )
    require(set(expected) <= fields.keys(), "Live response is missing a fixture field")
    return {
        "schema_version": 1,
        "scope": "U1 feature API only",
        "evidence_kind": "live" if live else "offline-fixture",
        "status": "observed" if live else "matched",
        "started_at": started,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "target": {"environment": config["environment"], **selected},
        "endpoint": config["origin"] + "/api/features",
        "http_status": 200 if live else None,
        "response_sha256": hashlib.sha256(content).hexdigest(),
        "response_bytes": len(content),
        "fixture_sha256": FIXTURE_SHA256,
        "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checks": {
            "required_boolean_fields": list(REQUIRED_FIELDS),
            "superplane_disabled": True,
            "fixture_fields_present": list(expected),
        },
        "u1_acceptance": "incomplete",
        "not_established": [
            "unset deployment configuration",
            "browser gating or enabled behavior",
            "deployed source revision or AWS/cluster identity",
            "deploy/undeploy resource cleanup",
        ],
    }


def run_live(environment) -> dict:
    """No fixture injection; publish a complete observation without overwriting files."""
    __tracebackhide__ = True
    config = settings(environment)
    report = observe(config)
    require(
        report["evidence_kind"] == "live",
        "Fixture evidence cannot be published as live",
    )
    output = Path(config["evidence_file"])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=output.parent, prefix=".u1-observation-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(report, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # An exclusive atomic link refuses an existing file/symlink, including races.
        os.link(temporary, output)
    except OSError:
        raise EvidenceError(
            "Feature evidence could not be published to a new file"
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return report
