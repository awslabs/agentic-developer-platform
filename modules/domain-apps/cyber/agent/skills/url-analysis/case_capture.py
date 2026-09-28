"""Compatibility import for the shared Browser runtime."""

import sys
from agentcore_tools.browser_runtime import case_capture as runtime

sys.modules[__name__] = runtime
