"""The installed Security worker has a registered, narrow internal identity."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_l5_worker_registry_matches_durable_delegation_service():
    registry = (ROOT / "modules/gateway/infra/l5-worker-registry.tf").read_text()
    security = (ROOT / "modules/L5-services/infra/modules/worker-runtime/iam.tf").read_text()
    gateway = (ROOT / "modules/gateway/infra/main.tf").read_text()
    assert 'agent_id          = { S = "l5-worker" }' in registry
    assert 'credential_scopes = { SS = ["l5:credential-session"] }' in registry
    assert "role_arn          = { S = module.l5_security[0].worker_role_arn }" in registry
    assert "iam_role_id       = { S = module.l5_security[0].worker_role_id }" in registry
    assert 'scope             = { S = "internal" }' in registry
    assert "Resource = var.gateway_api_arn" in security
    assert "Resource = var.rds_connect_arn" in security
    assert '"l5_services.runtime:factory"' in gateway
