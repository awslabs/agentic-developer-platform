import io
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

import pytest

from browser_broker import BrowserBrokerHandler
from browser_client import BrowserBrokerError, investigation_request
from investigation_browser import InvestigationError


def test_session_capability_routes_only_to_fixed_broker_port():
    with patch("browser_client._request", return_value={}) as call:
        investigation_request(
            "step", {"session_token": "10.0.1.9~private-capability", "action": "root"}
        )
    args = call.call_args.args
    assert args[2] == "http://10.0.1.9:8765"
    assert args[1]["session_token"] == "private-capability"
    for owner in ("169.254.169.254", "127.0.0.1", "8.8.8.8", "10.0.1.9:443"):
        with pytest.raises(ValueError):
            investigation_request("close", {"session_token": owner + "~secret"})


def test_capacity_and_liveness_are_separate_and_client_preserves_retry_advice():
    class Full:
        def capacity(self):
            return {"accepting_starts": False, "active_sessions": 4, "max_sessions": 4}

        def start(self, payload):
            raise InvestigationError(
                "Capacity is busy",
                code="capacity_busy",
                retry_after=5,
                browser_start_unattempted=True,
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), BrowserBrokerHandler)
    server.investigation_manager = Full()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(endpoint + "/healthz") as response:
            assert response.status == 200
        with pytest.raises(HTTPError) as raised:
            urlopen(endpoint + "/readyz")
        assert raised.value.code == 503
        with pytest.raises(BrowserBrokerError) as raised:
            investigation_request(
                "start", {"url": "https://public.test"}, broker_url=endpoint
            )
        assert raised.value.code == "capacity_busy"
        assert raised.value.retry_after == 5
        assert raised.value.browser_start_unattempted
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
