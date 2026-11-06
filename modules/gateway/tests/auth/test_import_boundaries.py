"""Narrow imports must work in fresh workers, independently of test-file order."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["src.admin.audit", "src.auth.dependencies", "src.auth.routes", "src.ratelimit.routes"])
def test_control_and_audit_modules_import_independently(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import importlib; importlib.import_module({module!r})"],
        env={**os.environ, "AWS_EC2_METADATA_DISABLED": "true"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_package_does_not_import_routes_for_a_schema():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from src.auth import TokenClaims; assert 'src.auth.routes' not in sys.modules"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_public_auth_exports_remain_available():
    import src.auth as auth
    from src.auth.auth_service import AuthService
    from src.auth.routes import router
    from src.auth.schemas import TokenClaims

    assert set(auth.__all__) == {
        "AuthService",
        "ServiceAccountService",
        "TenantResolver",
        "STSClient",
        "TokenManager",
        "router",
        "auth_middleware",
        "AuthenticationError",
        "AuthorizationError",
        "TokenValidationError",
        "STSClientError",
        "ServiceAccountCreate",
        "ServiceAccountUpdate",
        "ServiceAccountResponse",
        "TokenClaims",
        "TenantInfo",
    }
    assert auth.AuthService is AuthService
    assert auth.router is router
    assert auth.TokenClaims is TokenClaims
    assert all(getattr(auth, name) is not None for name in auth.__all__)
    with pytest.raises(AttributeError):
        getattr(auth, "missing_public_name")
