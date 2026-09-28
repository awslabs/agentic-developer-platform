"""Compatibility import for the shared code_interpreter service."""

import sys
from agentcore_tools import code_interpreter as service

sys.modules[__name__] = service
