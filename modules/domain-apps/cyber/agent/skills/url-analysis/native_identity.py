"""Compatibility import for the shared Browser subprocess identity."""

import sys
from agentcore_tools.browser_runtime import native_identity as runtime

sys.modules[__name__] = runtime
