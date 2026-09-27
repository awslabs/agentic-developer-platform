"""Execute registration with fake host tools to verify promotion gates."""

import configparser
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "modules/domain-apps/cyber/image-builder/windows/register-cape-vm.sh"


@pytest.mark.parametrize("failure", ["", "agent", "service", "existing"])
def test_registration_only_promotes_ready_guest(tmp_path, failure):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    config = tmp_path / "kvm.conf"
    config.write_text("[kvm]\nmachines = linux\n[linux]\nlabel = linux\n")
    tool = """#!/bin/bash
name=$(basename "$0")
echo "$name $*" >> "$CALL_LOG"
case "$name" in
  aws) touch "${@: -1}" ;;
  virsh)
    case "$1" in
      dominfo) [[ "$FAILURE" == existing ]] ;;
      domifaddr) echo 'vnet0 52:54:00:aa:bb:cc ipv4 192.168.100.120/24' ;;
      snapshot-dumpxml) echo '<domainsnapshot><state>running</state>'; python3 -c 'print("x" * 131072)'; echo '</domainsnapshot>' ;;
      domstate) echo 'shut off' ;;
    esac ;;
  curl) [[ "$FAILURE" != agent ]] ;;
  systemctl) [[ "$FAILURE" != service ]] ;;
  sleep) exit 0 ;;
esac
"""
    for name in ["aws", "virsh", "curl", "systemctl", "sleep"]:
        path = bin_dir / name
        path.write_text(tool)
        path.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CALL_LOG=str(log),
        FAILURE=failure,
        KVM_CONF=str(config),
        IMAGE_DIR=str(tmp_path / "images"),
        DOMAIN_XML_DIR=str(tmp_path / "xml"),
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "2026-09-27"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = log.read_text()
    if failure:
        assert result.returncode != 0, result.stdout
        assert "REGISTRATION COMPLETE" not in result.stdout
        if failure in ("agent", "existing"):
            assert "snapshot-create-as" not in calls
            assert "systemctl restart" not in calls
            assert "win11-cape" not in config.read_text()
    else:
        assert result.returncode == 0, result.stderr
        assert (
            calls.index("curl ")
            < calls.index("snapshot-create-as")
            < calls.index("virsh destroy")
        )
        parsed = configparser.ConfigParser()
        parsed.read(config)
        assert parsed["kvm"]["machines"] == "linux, win11-cape-2026-09-27"
        guest = parsed["win11-cape-2026-09-27"]
        assert guest["label"] == "win11-cape-2026-09-27"
        assert guest["ip"] == "192.168.100.120"
        assert guest["arch"] == "x64"
