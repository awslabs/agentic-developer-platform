"""Compatibility import for the shared websearch service."""

import sys
from agentcore_tools import websearch as service

sys.modules[__name__] = service
