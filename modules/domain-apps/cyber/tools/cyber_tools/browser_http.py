"""Compatibility import for the shared browser_http service."""

import sys
from agentcore_tools import browser_http as service

sys.modules[__name__] = service
