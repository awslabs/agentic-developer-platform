"""Compatibility import for the shared Browser runtime."""

import sys
from agentcore_tools.browser_runtime import browser_guard as runtime

sys.modules[__name__] = runtime
