"""API composition of immutable, separately admitted lifecycle controls."""

from app.models.lifecycle import WorkspaceLifecycleControlOperation  # noqa: F401
from workspace_provisioning.control_registry import (
    register_control_operation as register_control_operation,
)
