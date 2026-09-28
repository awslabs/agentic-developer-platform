"""Compatibility import for the shared task_browser service."""

import sys
from agentcore_tools import task_browser as service

sys.modules[__name__] = service
