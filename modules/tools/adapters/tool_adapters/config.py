"""Read bounded deployment-owned configuration without core/secret dependencies."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from adp_tools.adapter_contracts import AdapterRelease, WireModel
from pydantic import Field, model_validator

from tool_adapters.atlassian import READ_TOOLS, WRITE_TOOLS
from tool_adapters.atlassian_observe import canonical

MAX_CONFIGURATION_BYTES = 8 * 256 * 1024 + 64 * 1024


class ObservationBindings(WireModel):
    """Deployment-owned authority references, before provider schema discovery."""

    core_authorize_url: str = Field(
        pattern=r"^https://[a-zA-Z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9_-]+)?/internal/v1/tool-adapters/authorize$"
    )
    core_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    assertion_key_arn: str = Field(pattern=r"^arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[0-9a-f-]+$")
    edge_provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_workload: str = Field(pattern=r"^[A-Za-z0-9_-]+-atlassian-mcp-oauth-user$")
    identity_provider: str = Field(pattern=r"^[A-Za-z0-9_-]+-atlassian-mcp-user$")
    legacy_identity_workload: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+-oauth-user$")
    config_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    gateway_role_arn: str | None = Field(default=None, pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    reconciler_role_arn: str | None = Field(default=None, pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")


class ObservationConfiguration(ObservationBindings):
    subject_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sites_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    read_schemas_sha256: dict[str, str] | None = None
    read_schemas: dict[str, dict] | None = None
    write_enabled: bool = False
    write_schemas_sha256: dict[str, str] | None = None
    write_schemas: dict[str, dict] | None = None
    system_context_enabled: bool = False


class ObservationProfile(WireModel):
    """Prepared settings pinned to reviewed source, before image publication."""

    schema_version: Literal["1.0"]
    mode: Literal["observation"]
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    observation: ObservationConfiguration


class AdapterConfiguration(WireModel):
    schema_version: Literal["1.0"]
    mode: Literal["foundation", "observation"]
    release: AdapterRelease
    observation: ObservationConfiguration | None = None

    @model_validator(mode="after")
    def observation_requires_protected_configuration(self):
        if (self.mode == "observation") != (self.observation is not None):
            raise ValueError("Observation mode requires complete protected configuration")
        settings = self.observation
        if settings and settings.system_context_enabled and settings.read_schemas is None:
            raise ValueError("System context requires the configured MCP read route")
        if settings and (settings.read_schemas is not None or settings.read_schemas_sha256 is not None):
            if (
                settings.gateway_role_arn is None
                or not isinstance(settings.read_schemas, dict)
                or not isinstance(settings.read_schemas_sha256, dict)
                or set(settings.read_schemas) != set(READ_TOOLS)
                or set(settings.read_schemas_sha256) != set(READ_TOOLS)
                or any(
                    not isinstance(schema, dict)
                    or schema.get("type") != "object"
                    or not isinstance(schema.get("properties"), dict)
                    or hashlib.sha256(canonical(schema)).hexdigest() != settings.read_schemas_sha256[alias]
                    for alias, schema in settings.read_schemas.items()
                )
            ):
                raise ValueError("unverified read configuration")
        if settings and (
            settings.write_enabled or settings.write_schemas is not None or settings.write_schemas_sha256 is not None
        ):
            if (
                not settings.write_enabled
                or settings.read_schemas is None
                or settings.gateway_role_arn is None
                or not isinstance(settings.write_schemas, dict)
                or not isinstance(settings.write_schemas_sha256, dict)
                or set(settings.write_schemas) != set(WRITE_TOOLS)
                or set(settings.write_schemas_sha256) != set(WRITE_TOOLS)
                or any(
                    not isinstance(schema, dict)
                    or schema.get("type") != "object"
                    or not isinstance(schema.get("properties"), dict)
                    or hashlib.sha256(canonical(schema)).hexdigest() != settings.write_schemas_sha256[alias]
                    for alias, schema in settings.write_schemas.items()
                )
            ):
                raise ValueError("unverified write configuration")
        return self


class ConfigurationError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("Duplicate configuration field")
        result[key] = value
    return result


def load_configuration(path: str | None) -> AdapterConfiguration | None:
    if path is None:
        return None
    try:
        with Path(path).open("rb") as stream:
            content = stream.read(MAX_CONFIGURATION_BYTES + 1)
        if len(content) > MAX_CONFIGURATION_BYTES:
            raise ConfigurationError("Configuration exceeds bound")
        return AdapterConfiguration.model_validate(json.loads(content, object_pairs_hook=_unique_object))
    except (OSError, ValueError, TypeError):
        raise ConfigurationError("tool_adapter_configuration_invalid") from None
