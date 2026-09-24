"""Offline Chromium fixture for the real process/JSON supervision path."""

import socket
import sys
import os
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_guard
from test_domain_investigation import FixtureTransport


class FixtureClient:
    def __init__(self, region):
        pass

    def start(self, **kwargs):
        return "isolated-fixture-session"

    def generate_ws_headers(self):
        return "fixture", {}

    def stop(self):
        pass


for name in (
    "bedrock_agentcore",
    "bedrock_agentcore.tools",
    "bedrock_agentcore.tools.browser_client",
):
    sys.modules[name] = ModuleType(name)
sys.modules["bedrock_agentcore.tools.browser_client"].BrowserClient = FixtureClient

resolve = socket.getaddrinfo


def resolver(host, *args, **kwargs):
    if str(host).endswith(".test"):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))]
    return resolve(host, *args, **kwargs)


socket.getaddrinfo = resolver
original_open = browser_guard.open_guarded_browser


def fixture_open(url, playwright, **kwargs):
    browser = playwright.chromium.launch(headless=True)
    return original_open(
        url,
        SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=lambda *a, **k: browser)
        ),
        transport=FixtureTransport(),
        **kwargs
    )


browser_guard.open_guarded_browser = fixture_open

if "--stall-screenshot" in sys.argv or "--crash-screenshot" in sys.argv:

    def interrupted_screenshot(self, **kwargs):
        if "--crash-screenshot" in sys.argv:
            os._exit(7)
        time.sleep(90)

    browser_guard.GuardedBrowserSession.screenshot = interrupted_screenshot

from isolated_browser import worker

worker()
