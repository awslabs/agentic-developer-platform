"""Public composition entry point for approval-bound, journalled AWS networking.

The previous raw SDK helpers were never integrated and are deliberately removed:
provider mutations must go through Network with current operation authority.
"""

from .network_runtime import Network

__all__ = ["Network"]
