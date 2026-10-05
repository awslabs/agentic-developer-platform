"""A03: shipped component configurations agree on their security boundaries."""

import importlib.util
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.ratelimit.config import RateLimitConfig
from src.ratelimit.service import RateLimitService

ROOT = Path(__file__).resolve().parents[4]
SUPERPLANE = ROOT / "modules/domain-apps/superplane"
DOOR = ROOT / "modules/agent-context"


def load_config(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_environment(monkeypatch):
    for name in (
        "SUPERPLANE_SECURITY_PROFILE",
        "DOMAIN_AUTH_ENFORCED",
        "CORS_ORIGINS",
        "JWT_SECRET_KEY",
        "DOOR_SECURITY_PROFILE",
        "DOOR_AUTH_ENABLED",
        "TENANT_SCOPE_ENABLED",
        "RATELIMIT_SECURITY_PROFILE",
        "RATELIMIT_BACKEND_TYPE",
        "RATELIMIT_ALLOW_MEMORY_BACKEND",
        "RATELIMIT_REDIS_URL",
        "BG_REDIS_URL",
        "TESTING",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def configs(clean_environment):
    superplane = load_config("a03_superplane_config", SUPERPLANE / "src/superplane-api/app/config.py")
    door = load_config("a03_door_config", DOOR / "door/config.py")
    return superplane, door


def test_absent_overrides_select_safe_defaults(configs):
    superplane, door = configs
    assert superplane.Settings().domain_auth_enforced is True
    assert superplane.Settings().superplane_security_profile == "production"
    assert superplane.Settings().cors_origins == []
    assert superplane.Settings().jwt_secret_key == ""
    assert door.ServerConfig().tenant_scope_enabled is True
    assert door.ServerConfig().door_verification_keys == "{}"
    assert door.ServerConfig().security_profile == "production"
    rate = RateLimitConfig(_env_file=None)
    assert rate.backend_type == "redis"
    assert rate.security_profile == "production"
    assert rate.allow_memory_backend is False
    with pytest.raises(RuntimeError, match="Shared rate limiting"):
        RateLimitService(config=rate)


@pytest.mark.parametrize("profile", ["production", "staging", "prod", ""])
def test_superplane_legacy_mode_refused_outside_explicit_development(configs, profile):
    superplane, _ = configs
    with pytest.raises(ValidationError):
        superplane.Settings(domain_auth_enforced=False, superplane_security_profile=profile)
    assert superplane.Settings(domain_auth_enforced=False, superplane_security_profile="development").domain_auth_enforced is False


@pytest.mark.parametrize("setting", ["TENANT_SCOPE_ENABLED"])
@pytest.mark.parametrize("value", ["false", "0", " no "])
def test_door_opt_out_requires_development(configs, monkeypatch, setting, value):
    _, door = configs
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError, match="requires DOOR_SECURITY_PROFILE=development"):
        door.ServerConfig()
    monkeypatch.setenv("DOOR_SECURITY_PROFILE", "development")
    assert door.ServerConfig().tenant_scope_enabled is False


@pytest.mark.parametrize("value", ["", "ture", "enabled", "True "])
def test_tenant_scope_typo_cannot_disable_isolation(configs, monkeypatch, value):
    _, door = configs
    monkeypatch.setenv("TENANT_SCOPE_ENABLED", value)
    assert door.ServerConfig().tenant_scope_enabled is True


def test_memory_opt_in_alone_cannot_disable_shared_counters(clean_environment):
    with pytest.raises(RuntimeError, match="Shared rate limiting"):
        RateLimitService(config=RateLimitConfig(backend_type="memory", allow_memory_backend=True, _env_file=None))
    from src.ratelimit.backends.in_memory import InMemoryBackend

    local = RateLimitService(
        config=RateLimitConfig(
            backend_type="memory",
            allow_memory_backend=True,
            security_profile="development",
            _env_file=None,
        )
    )
    assert isinstance(local._backend, InMemoryBackend)


def test_legacy_manifest_pins_production_and_requires_policy_inputs(configs):
    superplane, _ = configs
    docs = list(yaml.safe_load_all((SUPERPLANE / "src/superplane-api/deploy/deployment.yaml").read_text()))
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    env = {item["name"]: item for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["DOMAIN_AUTH_ENFORCED"]["value"] == str(superplane.Settings().domain_auth_enforced).lower()
    assert env["SUPERPLANE_SECURITY_PROFILE"]["value"] == "production"
    for name in ("COGNITO_ISSUER", "COGNITO_JWKS_URL", "DOMAIN_AUTH_ALLOWED_CLIENT_IDS"):
        assert env[name]["valueFrom"]["configMapKeyRef"]["optional"] is False
    assert env["JWT_SECRET_KEY"]["valueFrom"]["secretKeyRef"]["optional"] is False
    template = dict(
        line.split("=", 1)
        for line in (SUPERPLANE / "src/superplane-api/deploy/config.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert template["DOMAIN_AUTH_ENFORCED"] == "true"
    assert template["SUPERPLANE_SECURITY_PROFILE"] == "production"
    assert template["CORS_ORIGINS"] == "[]"
    assert template["JWT_SECRET_KEY"] == ""


def test_shipped_door_and_gateway_configs_pin_shared_security(configs):
    door = yaml.safe_load((DOOR / "manifests/agent-context-configmap.yaml").read_text())["data"]
    gateway = yaml.safe_load((ROOT / "modules/gateway/k8s/configmap.yaml").read_text())["data"]
    assert door["TENANT_SCOPE_ENABLED"] == "true"
    assert door["DOOR_SECURITY_PROFILE"] == "production"
    assert gateway["RATELIMIT_BACKEND_TYPE"] == "redis"
    assert gateway["RATELIMIT_SECURITY_PROFILE"] == "production"
