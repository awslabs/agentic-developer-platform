"""Offline HTTP boundary for real release-shell tests; never contacts a service."""

import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
from unittest.mock import patch
from urllib.error import URLError
from uuid import uuid4

from modules.tools.platform.scripts.native_test_session import canonical


def main(arguments):
    if os.environ.get("ADP_TOOLS_RELEASE_QUALIFICATION") == "operational":
        from modules.tools.platform.tests.test_operational_release import Wire
        config = json.loads(Path(os.environ["ADP_TOOLS_OPERATIONAL_CONFIG_FILE"]).read_text())
        token = Path(os.environ["ADP_TOOLS_OPERATIONAL_LOGIN_FILE"]).read_text()
        wire = Wire(config, token, os.environ["FAKE_GATEWAY_URL"], os.environ.get("FAKE_OPERATIONAL_FAULT"))
        sys.argv = ["native_release", *arguments]
        try:
            with patch("urllib.request.OpenerDirector.open", side_effect=wire.open):
                runpy.run_module("modules.tools.platform.scripts.native_release", run_name="__main__")
        finally:
            Path(os.environ["FAKE_SMOKE_LOG"]).write_text(json.dumps(wire.calls))
        return
    scenario = os.environ.get("FAKE_SMOKE_SCENARIO", "success")
    calls = []
    execution = str(uuid4())
    search = {}
    capability = "adpt1." + str(uuid4()) + "." + "c" * 43
    fixture = json.loads(Path(os.environ["ADP_TOOLS_SMOKE_FIXTURE_FILE"]).read_text())
    receipt = json.loads(Path(os.environ["ADP_TOOLS_SMOKE_DELIVERY_FILE"]).read_text())
    discovered = 0

    def respond(request, **kwargs):
        nonlocal discovered
        calls.append(request.full_url)
        Path(os.environ["FAKE_SMOKE_LOG"]).write_text(json.dumps(calls))
        gateway = os.environ["FAKE_GATEWAY_URL"]
        gateway_digest = hashlib.sha256(gateway.encode()).hexdigest()
        status = 200
        if request.full_url.startswith(
            "https://pipeline.actions.githubusercontent.com/"
        ):
            assert request.get_header("Authorization") == "Bearer offline-oidc-request"
            payload = {"value": "github.oidc.signature"}
        elif request.full_url.endswith("/capabilities/exchange"):
            assert (
                request.get_header("Authorization") == "Bearer fixture.access.signature"
            )
            if scenario == "authentication-failure":
                raise URLError("private-auth-sentinel")
            payload = {
                "scope": "all_in",
                "all_in_admission": "required",
                "capability": capability,
            }
        else:
            assert (
                request.get_header("Authorization") == "Bearer fixture.access.signature"
            )
            assert request.get_header("X-adp-native-test-capability") == capability
            if request.full_url.endswith("/status"):
                started = receipt["run_started_at"]
                payload = {
                    "schema_version": "1.0",
                    "run_id": fixture["run_id"],
                    "run_started_at": started,
                    "cap_usd": "20",
                    "held_usd": "1.20",
                    "settled_usd": "0",
                    "funding_status": "held",
                    "accounting_status": "pending",
                    "governance_complete": False,
                    "executions": [
                        {
                            "execution_id": execution,
                            "original_operation_id": search["operation_id"],
                            "reservation_id": "tools-test-effect:" + "d" * 64,
                            "quoted_usd": "0.01",
                            "contract_sha256": fixture["contract_sha256"],
                            "request_sha256": search["sha256"],
                            "session_deadline_at": started + 300,
                            "cleanup_deadline_at": started + 600,
                            "cleanup_confirmed_at": started + 10,
                            "cleanup_status": "confirmed",
                            "cleanup_attempts_used": 1,
                            "cleanup_attempts_limit": 1,
                            "cleanup_evidence_sha256": "e" * 64,
                        }
                    ],
                }
                if scenario == "accounting-unknown":
                    payload["funding_status"] = "unknown"
                if scenario == "cleanup-late":
                    payload["executions"][0]["cleanup_confirmed_at"] = started + 601
            elif "/close?" in request.full_url:
                payload = {
                    "execution_id": execution,
                    "state": "closed",
                    "cleanup_status": "confirmed",
                }
                if scenario == "close-failure":
                    status = 503
            elif request.data:
                packet = json.loads(request.data)
                assert (
                    packet["tool"] == "websearch___search"
                    and packet["arguments"] == fixture["arguments"]
                )
                assert (
                    request.get_header("X-adp-expected-gateway-sha256")
                    == gateway_digest
                )
                search.update(
                    operation_id=request.get_header("X-adp-native-test-operation"),
                    sha256=hashlib.sha256(
                        canonical({"schema_version": "1.0", "action": "call", **packet})
                    ).hexdigest(),
                )
                payload = {
                    "request_id": packet["request_id"],
                    "execution_id": execution,
                    "receipt_id": "tools-test-effect:" + "d" * 64,
                    "status": "confirmed",
                    "result_status": "success",
                    "gateway_url_sha256": gateway_digest,
                }
                if scenario == "search-error":
                    payload["result_status"] = "error"
                if scenario == "search-pending":
                    status = 202
                if scenario == "unbound-search":
                    payload.pop("gateway_url_sha256")
            else:
                discovered += 1
                digest = hashlib.sha256(b"live-discovery-fixture").hexdigest()
                if scenario == "wrong-gateway":
                    gateway_digest = "f" * 64
                if scenario == "digest-mismatch" and discovered == 2:
                    digest = "f" * 64
                payload = {
                    "tools": [{"name": "websearch___search"}],
                    "discovery": {
                        "gateway_url_sha256": gateway_digest,
                        "tools_list_sha256": digest,
                    },
                }
                if scenario == "discovery-failure":
                    status = 503
                if scenario == "old-facade":
                    payload = {"tools": []}
        response = io.BytesIO(json.dumps(payload).encode())
        response.status = status
        return response

    sys.argv = ["native_release", *arguments]
    with patch("urllib.request.OpenerDirector.open", side_effect=respond):
        runpy.run_module(
            "modules.tools.platform.scripts.native_release", run_name="__main__"
        )
