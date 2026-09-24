"""Reuse the real bootstrap-to-registration fixture as retirement's producer."""

from workspace_bootstrap.tests.conftest import (  # noqa: F401
    binding,
    principal,
    provider_identity,
    observed_cluster,
    expected_target,
)
from workspace_bootstrap.tests.test_authority_runtime_postgres import runtime  # noqa: F401
from workspace_bootstrap.tests.test_registry_postgres import (  # noqa: F401
    database,
    loop,
    schema_ddl,
    server,
)
