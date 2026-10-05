"""Compatibility import for the shared Browser runtime."""

import sys
from agentcore_tools.browser_runtime import native_browser as runtime

sys.modules[__name__] = runtime
