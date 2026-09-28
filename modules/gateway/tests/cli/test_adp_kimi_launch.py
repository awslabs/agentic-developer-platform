"""Kimi dispatch must preserve arguments, exit status and deployment selection."""

import json
import os
import subprocess

from .conftest import write_adp_session


def test_kimi_dispatch_preserves_arguments_and_selected_auth_store(adp_script, adp_home):
    write_adp_session(adp_home)
    launcher = adp_home / ".local/share/kimi-adp/launch.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        '#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps({"argv":sys.argv[1:],"auth":os.environ["BG_CONFIG_DIR"]}))\nsys.exit(23)\n'
    )
    launcher.chmod(0o755)
    args = ["--prompt", "read a file with spaces", "--model", "adp-kimi-k3-us"]
    result = subprocess.run(
        ["bash", str(adp_script), "kimi", "--", *args],
        env=dict(
            os.environ, HOME=str(adp_home), ADP_TENANT_ID="work", ADP_TENANT_SUB="sub-123", ADP_TENANT_MODE="lease", ADP_TENANT_MEMBERSHIP="member"
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 23
    assert json.loads(result.stdout) == {"argv": args, "auth": str(adp_home / ".bedrock-gateway")}


def test_missing_adapter_fails_without_launching_native_kimi(adp_script, adp_home):
    write_adp_session(adp_home)
    result = subprocess.run(
        ["bash", str(adp_script), "kimi"],
        env=dict(
            os.environ, HOME=str(adp_home), ADP_TENANT_ID="work", ADP_TENANT_SUB="sub-123", ADP_TENANT_MODE="lease", ADP_TENANT_MEMBERSHIP="member"
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "isolated Kimi ADP adapter is not installed" in result.stderr + result.stdout
