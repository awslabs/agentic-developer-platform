"""Test-only AWS replacement; real Chromium and production IPC/recorder code."""

import socket
import sys
from pathlib import Path

from native_network import CHROMIUM_ARGS, fixture_dns

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if "--worker" in sys.argv:
    from playwright.sync_api import BrowserType
    from bedrock_agentcore.tools.browser_client import BrowserClient
    from isolated_browser import worker

    socket.getaddrinfo = fixture_dns
    BrowserType.connect_over_cdp = lambda self, *a, **k: self.launch(headless=True, args=CHROMIUM_ARGS)
    BrowserClient.start = lambda self, **kwargs: "synthetic-session"
    BrowserClient.generate_ws_headers = lambda self: ("fixture", {})

    def stopped(self):
        if "--cleanup-receipt" in sys.argv:
            Path(sys.argv[sys.argv.index("--cleanup-receipt") + 1]).write_text(
                "synthetic-session"
            )

    BrowserClient.stop = stopped
    worker()
else:
    import isolated_browser
    import local_browser

    original = isolated_browser.ProcessActor
    cleanup_receipt = Path(sys.argv[2]) / "provider-closed"

    def actor(*args, **kwargs):
        kwargs["command"] = [
            sys.executable,
            __file__,
            "--worker",
            "--native",
            "--cleanup-receipt",
            str(cleanup_receipt),
        ]
        # Provider replacement: only the child's fake BrowserClient.stop writes
        # this receipt. An exit alone cannot prove the provider session closed.
        kwargs["stop"] = lambda session_id: cleanup_receipt.read_text() == session_id
        return original(*args, **kwargs)

    isolated_browser.ProcessActor = actor
    local_browser.serve(Path(sys.argv[2]))
