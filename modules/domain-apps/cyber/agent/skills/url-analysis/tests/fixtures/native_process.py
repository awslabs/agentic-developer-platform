"""Test-only AWS replacement; real Chromium and production IPC/recorder code."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if "--worker" in sys.argv:
    from playwright.sync_api import BrowserType
    from bedrock_agentcore.tools.browser_client import BrowserClient
    from isolated_browser import worker

    BrowserType.connect_over_cdp = lambda self, *a, **k: self.launch(headless=True)
    BrowserClient.start = lambda self, **kwargs: "synthetic-session"
    BrowserClient.generate_ws_headers = lambda self: ("fixture", {})
    BrowserClient.stop = lambda self: None
    worker()
else:
    import isolated_browser
    import local_browser

    original = isolated_browser.ProcessActor

    def actor(*args, **kwargs):
        kwargs["command"] = [sys.executable, __file__, "--worker", "--native"]
        return original(*args, **kwargs)

    isolated_browser.ProcessActor = actor
    local_browser.serve(Path(sys.argv[2]))
