"""Public runtime configuration fails closed on partial or unreadable authority."""

import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = runpy.run_path(str(Path(__file__).parents[2] / "scripts/native-credential-config.py"))
ACCOUNT = "123456789012"
REGION = "us-east-1"
CONFIG = {
    "enabled": True,
    "issuer": "https://gateway.example.test",
    "approval_origin": "https://gateway.example.test",
    "key_id": "adp-native-private-v1",
    "key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
}


def test_enabled_runtime_matches_exact_key_and_origins():
    assert MODULE["render"](CONFIG, ACCOUNT, REGION).split() == [
        "true",
        CONFIG["issuer"],
        CONFIG["approval_origin"],
        CONFIG["key_id"],
        CONFIG["key_arn"],
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "true"),
        ("enabled", 1),
        ("issuer", "http://gateway.example.test"),
        ("issuer", "https://user@gateway.example.test"),
        ("issuer", "https://gateway.example.test/api"),
        ("approval_origin", "https://gateway.example.test/"),
        ("approval_origin", "https://gateway.example.test?x=y"),
        ("key_id", "substitution$(command)"),
        ("key_arn", CONFIG["key_arn"].replace(ACCOUNT, "999999999999")),
        ("key_arn", CONFIG["key_arn"].replace(REGION, "us-west-2")),
        ("key_arn", "alias/native"),
    ],
)
def test_invalid_authority_never_reaches_shell_or_configmap(field, value):
    with pytest.raises(ValueError):
        MODULE["render"]({**CONFIG, field: value}, ACCOUNT, REGION)


@pytest.mark.parametrize("missing", list(CONFIG))
def test_partial_configuration_is_not_disabled(missing):
    with pytest.raises(ValueError):
        MODULE["render"]({k: v for k, v in CONFIG.items() if k != missing}, ACCOUNT, REGION)


@pytest.mark.parametrize("error,disabled", [("ParameterNotFound", True), ("AccessDeniedException", False), ("ExpiredTokenException", False)])
def test_only_missing_configuration_is_optional(monkeypatch, capsys, error, disabled):
    monkeypatch.setattr(sys, "argv", ["native-config", "dev", REGION, ACCOUNT])
    monkeypatch.setattr(MODULE["subprocess"], "run", lambda *a, **kw: SimpleNamespace(returncode=1, stderr=f"({error})", stdout=""))
    if disabled:
        MODULE["main"]()
        assert capsys.readouterr().out == "false disabled disabled disabled disabled\n"
    else:
        with pytest.raises(SystemExit, match="deployment refused"):
            MODULE["main"]()
        assert capsys.readouterr().out == ""


@pytest.mark.parametrize("raw", [json.dumps(CONFIG), '{"enabled":true,"enabled":false}', '{"private":"CANARY_SECRET"}'])
def test_ssm_json_is_validated_without_echoing_invalid_content(monkeypatch, capsys, raw):
    monkeypatch.setattr(sys, "argv", ["native-config", "dev", REGION, ACCOUNT])
    response = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Parameter": {"Value": raw}}))
    monkeypatch.setattr(MODULE["subprocess"], "run", lambda *a, **kw: response)
    if raw == json.dumps(CONFIG):
        MODULE["main"]()
        assert capsys.readouterr().out.strip() == MODULE["render"](CONFIG, ACCOUNT, REGION)
    else:
        with pytest.raises(SystemExit, match="Invalid native credential configuration; deployment refused"):
            MODULE["main"]()
        assert capsys.readouterr().out == ""
