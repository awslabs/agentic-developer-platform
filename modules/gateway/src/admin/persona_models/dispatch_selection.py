"""Compatibility imports; agent launch owns configuration resolution."""

from src.agentauth.launch_configuration import (
    apply_dispatch_selection,
    mapping_enabled,
    select_for_dispatch,
)

__all__ = ["apply_dispatch_selection", "mapping_enabled", "select_for_dispatch"]
