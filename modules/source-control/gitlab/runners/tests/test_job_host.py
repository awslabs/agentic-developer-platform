"""Offline entry-point checks for the baked disposable job-host guard."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "templates" / "job-user-data.sh.tftpl"


class JobHostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="adp-job-host-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.physical_interface("ens5")
        (self.root / "sys/class/net/docker0").mkdir()
        (self.root / "sys/class/net/arbitrary-job-bridge").mkdir()
        binaries = self.root / "bin"
        binaries.mkdir()
        (binaries / "nft").write_text("""#!/bin/bash
set -eu
printf 'nft %s\\n' "$*" >> "$ADP_HOST_TEST_ROOT/calls"
case "$1" in
  -c|-f)
    test ! -e "$ADP_HOST_TEST_ROOT/fail-nft"
    file="${@: -1}"
    if grep -Fxq 'delete table inet adp_job' "$file"; then
      test -e "$ADP_HOST_TEST_ROOT/nft-state"
      if [[ "$1" == -f ]]; then tail -n +2 "$file" > "$ADP_HOST_TEST_ROOT/nft-state"; fi
    else
      test ! -e "$ADP_HOST_TEST_ROOT/nft-state"
      if [[ "$1" == -f ]]; then cp "$file" "$ADP_HOST_TEST_ROOT/nft-state"; fi
    fi
    ;;
  list)
    test -e "$ADP_HOST_TEST_ROOT/nft-state"
    sed -e 's/priority -150/priority mangle/g' -e 's/priority -310/priority raw - 10/g' "$ADP_HOST_TEST_ROOT/nft-state"
    ;;
  *) exit 1 ;;
esac
""")
        (binaries / "systemctl").write_text("""#!/bin/bash
set -eu
printf 'systemctl %s\\n' "$*" >> "$ADP_HOST_TEST_ROOT/calls"
if [[ "$1" == is-active && -e "$ADP_HOST_TEST_ROOT/inactive" ]]; then exit 1; fi
""")
        (binaries / "sshd").write_text("""#!/bin/bash
printf 'authorizedkeyscommand /opt/aws/bin/eic_run_authorized_keys %%u %%f\nauthorizedkeyscommanduser %s\n' "${ADP_TEST_EIC_USER:-ec2-instance-connect}"
""")
        (binaries / "id").write_text("""#!/bin/bash
[[ $* == '-u ec2-instance-connect' ]] || exit 1
echo "${ADP_TEST_EIC_UID:-997}"
""")
        for binary in binaries.iterdir():
            binary.chmod(0o700)
        self.environment = dict(os.environ, ADP_HOST_TEST_ROOT=str(self.root),
                                PATH=f"{binaries}:{os.environ['PATH']}")

    def physical_interface(self, name, *, driver="ena", vendor="0x1d0f", device_class="0x020000"):
        device = self.root / "sys/class/net" / name / "device"
        device.mkdir(parents=True)
        (device / "vendor").write_text(vendor + "\n")
        (device / "class").write_text(device_class + "\n")
        driver_path = self.root / "sys/bus/pci/drivers" / driver
        driver_path.mkdir(parents=True, exist_ok=True)
        (device / "driver").symlink_to(driver_path)
        return device

    def run_host(self, *arguments, success=True):
        result = subprocess.run(["bash", str(SCRIPT), *arguments], env=self.environment,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return result

    def bake(self):
        self.run_host("--bake")
        return self.root / "usr/local/libexec/adp-job-host"

    def test_bake_requires_firewall_before_network_ssh_and_docker(self):
        installed = self.bake()
        self.assertEqual(installed.read_bytes(), SCRIPT.read_bytes())
        unit = (self.root / "etc/systemd/system/adp-job-firewall.service").read_text()
        docker = (self.root / "etc/systemd/system/docker.service.d/10-adp-job-host.conf").read_text()
        ssh = (self.root / "etc/systemd/system/sshd.service.d/10-adp-job-host.conf").read_text()
        policy = (self.root / "etc/adp/job-firewall.nft").read_text()
        self.assertIn("Before=network-pre.target docker.service sshd.service", unit)
        self.assertIn("WantedBy=sysinit.target", unit)
        self.assertIn("Requires=adp-job-firewall.service", docker)
        self.assertIn("--network-verify", docker)
        self.assertIn("Requires=adp-job-firewall.service", ssh)
        self.assertIn("--verify", ssh)
        for chain in ("prerouting", "input", "forward", "output"):
            self.assertIn(f"chain {chain} {{", policy)
            priority = -310 if chain == "prerouting" else -150
            self.assertIn(f"hook {chain} priority {priority}", policy)
        self.assertEqual(policy.count("policy drop;"), 3)
        self.assertIn("ip daddr 169.254.169.254 drop", policy)
        self.assertIn("ip6 daddr fd00:ec2::254 drop", policy)
        forward = policy.split("chain forward {", 1)[1].split("chain output {", 1)[0]
        self.assertNotIn("tcp dport 443 accept", forward)
        self.assertIn("ct state established,related accept", forward)
        calls = (self.root / "calls").read_text()
        self.assertLess(calls.index("systemctl daemon-reload"), calls.index("systemctl enable adp-job-firewall.service"))
        self.assertFalse((self.root / "run/adp-job-ready").exists())

    def test_readiness_drains_sshd_output_and_preserves_producer_failure(self):
        self.bake()
        self.run_host("--enforce")
        self.run_host("--allow", "10.0.0.10", "10.0.0.11")
        eic = self.root / "opt/aws/bin/eic_run_authorized_keys"
        eic.parent.mkdir(parents=True)
        eic.write_text("#!/bin/sh\nexit 0\n")
        eic.chmod(0o500)
        (self.root / "bin/sshd").write_text("""#!/usr/bin/env python3
import os, signal, sys
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
os.write(1, b'authorizedkeyscommand /opt/aws/bin/eic_run_authorized_keys %u %f\\n')
os.write(1, b'authorizedkeyscommanduser ec2-instance-connect\\n')
# Exceed any normal pipe buffer after the matching lines. An early-exiting
# consumer deterministically breaks sshd even though its config is correct.
for _ in range(256):
    os.write(1, b'unrelated-option value\\n' * 1024)
sys.exit(int(os.environ.get('ADP_TEST_SSHD_EXIT', '0')))
""")
        self.run_host("--ready")
        ready = self.root / "run/adp-job-ready"
        self.assertTrue(ready.exists())
        ready.unlink()
        self.environment["ADP_TEST_SSHD_EXIT"] = "17"
        self.run_host("--ready", success=False)
        self.assertFalse(ready.exists())

    def test_missing_bake_or_initial_firewall_never_opens_docker(self):
        self.run_host("--boot", success=False)
        self.bake()
        self.run_host("--boot", success=False)
        self.run_host("--verify", success=False)
        (self.root / "run").mkdir()
        (self.root / "run/adp-job-ready").touch()
        self.run_host("--boot", success=False)
        self.assertFalse((self.root / "run/adp-job-ready").exists())

    def test_failure_and_tampering_prevent_admission(self):
        installed = self.bake()
        (self.root / "fail-nft").touch()
        self.run_host("--enforce", success=False)
        self.assertFalse((self.root / "nft-state").exists())
        (self.root / "fail-nft").unlink()
        self.run_host("--enforce")
        self.run_host("--boot")
        self.run_host("--verify", success=False)
        installed.chmod(0o700)
        installed.write_bytes(installed.read_bytes() + b"\n")
        self.run_host("--boot", success=False)
        self.run_host("--verify", success=False)

    def test_docker_restart_and_host_reboot_recheck_denial(self):
        self.bake()
        self.run_host("--enforce")
        self.run_host("--boot")
        (self.root / "run").mkdir(exist_ok=True)
        (self.root / "run/adp-job-ready").touch()
        self.run_host("--verify")
        original = (self.root / "nft-state").read_text()
        for address in ("169.254.169.254", "fd00:ec2::254"):
            (self.root / "nft-state").write_text(original.replace(address, "192.0.2.1"))
            self.run_host("--verify", success=False)
        (self.root / "nft-state").write_text(original)
        with (self.root / "nft-state").open("a") as rules:
            rules.write("\n  ip daddr 0.0.0.0/0 accept\n")
        self.run_host("--verify", success=False)
        (self.root / "nft-state").unlink()
        self.run_host("--verify", success=False)
        self.run_host("--boot", success=False)
        self.assertFalse((self.root / "run/adp-job-ready").exists())
        self.run_host("--enforce")
        self.run_host("--boot")
        self.run_host("--verify", success=False)

    def test_network_admission_rejects_metadata_public_and_unexpected_rules(self):
        self.bake()
        self.run_host("--enforce")
        self.run_host("--boot")
        for first, second in (("169.254.169.254", "10.0.0.11"),
                              ("fd00:ec2::254", "10.0.0.11"),
                              ("8.8.8.8", "10.0.0.11"),
                              ("10.0.0.10", "10.0.0.10"),
                              ("10.0.0.10;drop", "10.0.0.11")):
            self.run_host("--allow", first, second, success=False)
            self.assertFalse((self.root / "run/adp-job-ready").exists())
        for cache in ("169.254.169.254", "fd00:ec2::254", "8.8.8.8", "10.0.0.10"):
            self.run_host("--allow", "10.0.0.10", cache, success=False)
        self.run_host("--allow", "10.0.0.10", "10.0.0.11", "10.0.0.14", success=False)
        self.run_host("--allow", "10.0.0.10", "10.0.0.14")
        self.run_host("--verify", success=False)
        self.run_host("--ready", success=False)
        eic = self.root / "opt/aws/bin/eic_run_authorized_keys"
        eic.parent.mkdir(parents=True)
        eic.write_text("#!/bin/sh\nexit 0\n")
        eic.chmod(0o500)
        self.run_host("--ready")
        self.run_host("--verify")
        policy = (self.root / "etc/adp/job-firewall.nft").read_text()
        forward = policy.split("chain forward {", 1)[1].split("chain output {", 1)[0]
        self.assertEqual(forward.count("tcp dport 443 accept"), 2)
        self.assertIn("ip daddr 10.0.0.14 tcp dport 443 accept", forward)
        self.assertNotIn("ip daddr 10.0.0.11 tcp dport 443 accept", policy)
        self.assertNotIn("dport 53", policy)
        self.assertIn("ip daddr 169.254.169.254 drop", forward)
        self.assertIn("ip6 daddr fd00:ec2::254 drop", forward)
        with (self.root / "nft-state").open("a") as state:
            state.write("\n ip daddr 0.0.0.0/0 accept\n")
        self.run_host("--verify", success=False)
        self.run_host("--boot", success=False)
        self.assertFalse((self.root / "run/adp-job-ready").exists())

    def test_canonical_nft_reload_and_failed_reload_keep_admission_closed(self):
        self.bake()
        (self.root / "unrelated-table").write_text("untouched")
        self.run_host("--enforce")
        policy = (self.root / "etc/adp/job-firewall.nft").read_text()
        loaded = self.root / "etc/adp/job-firewall.loaded"
        self.assertNotEqual(loaded.read_text(), policy)
        self.run_host("--enforce")
        self.run_host("--boot")
        (self.root / "run").mkdir(exist_ok=True)
        (self.root / "run/adp-job-ready").touch()
        self.run_host("--verify")
        self.run_host("--allow", "10.0.0.10", "10.0.0.14")
        self.assertEqual((self.root / "unrelated-table").read_text(), "untouched")
        calls = (self.root / "calls").read_text()
        self.assertGreaterEqual(calls.count("nft -f "), 3)
        self.assertEqual(calls.count("nft -c -f "), calls.count("nft -f "))
        (self.root / "fail-nft").touch()
        self.run_host("--enforce", success=False)
        self.assertFalse(loaded.exists())
        self.assertFalse((self.root / "run/adp-job-ready").exists())
        self.run_host("--verify", success=False)
        (self.root / "fail-nft").unlink()
        self.run_host("--enforce")
        self.run_host("--boot")
        self.run_host("--verify", success=False)

    def test_failed_admission_reload_does_not_update_policy_or_snapshot(self):
        self.bake()
        self.run_host("--enforce")
        self.run_host("--boot")
        (self.root / "run").mkdir(exist_ok=True)
        (self.root / "run/adp-job-ready").touch()
        policy = self.root / "etc/adp/job-firewall.nft"
        original = policy.read_text()
        (self.root / "fail-nft").touch()
        self.run_host("--allow", "10.0.0.10", "10.0.0.14", success=False)
        self.assertEqual(policy.read_text(), original)
        self.assertFalse((self.root / "etc/adp/job-firewall.loaded").exists())
        self.assertFalse((self.root / "run/adp-job-ready").exists())
        self.run_host("--network-verify", success=False)
        self.assertFalse(list((self.root / "etc/adp").glob("*.reload.*")))
        (self.root / "fail-nft").unlink()
        self.run_host("--enforce")
        self.run_host("--boot")
        self.run_host("--verify", success=False)

    def test_ssh_replies_dhcp_and_metadata_exceptions_are_narrow(self):
        self.bake()
        policy = (self.root / "etc/adp/job-firewall.nft").read_text()
        self.assertIn("hook prerouting priority -310", policy)
        for chain in ("input", "output", "forward"):
            self.assertIn(f"hook {chain} priority -150", policy)
        output = policy.split("chain output {", 1)[1]
        self.assertLess(output.index("ip daddr 169.254.169.254 drop"),
                        output.index("ct state established,related accept"))
        self.assertIn("meta skuid 0 ip daddr 169.254.169.254 tcp dport 80 accept", output)
        self.assertIn('meta skuid "ec2-instance-connect" ip daddr 169.254.169.254 tcp dport 80 accept', output)
        self.assertIn("meta skuid 0 oifname $adp_interface ip daddr 255.255.255.255 udp sport 68 udp dport 67 accept", output)
        self.assertIn("iifname $adp_interface udp sport 67 udp dport 68 accept", policy)
        incoming = policy.split("chain input {", 1)[1].split("chain forward {", 1)[0]
        for denial in ("iifname != $adp_interface tcp dport 22 drop",
                       "iifname != $adp_interface udp sport 67 udp dport 68 drop"):
            self.assertLess(incoming.index(denial), incoming.index("ct state established,related accept"))
        self.assertNotIn("dport 53", policy)
        self.run_host("--enforce")
        self.run_host("--boot")
        self.run_host("--allow", "10.0.0.10", "10.0.0.14")
        after = (self.root / "etc/adp/job-firewall.nft").read_text()
        self.assertIn('define adp_interface = "ens5"', after)
        self.assertIn("iifname $adp_interface tcp dport 22 accept", after)
        self.assertNotIn("    tcp dport 22 accept", after)
        self.assertIn("ct state established,related accept", after)
        self.run_host("--ready", success=False)
        eic = self.root / "opt/aws/bin/eic_run_authorized_keys"
        eic.parent.mkdir(parents=True)
        eic.write_text("#!/bin/sh\nexit 0\n")
        eic.chmod(0o500)
        self.environment["ADP_TEST_EIC_USER"] = "10001"
        self.run_host("--ready", success=False)
        self.environment["ADP_TEST_EIC_USER"] = "ec2-instance-connect"
        self.environment["ADP_TEST_EIC_UID"] = "10001"
        self.run_host("--ready", success=False)
        self.environment["ADP_TEST_EIC_UID"] = "997"
        self.run_host("--ready")
        self.run_host("--verify")
        (self.root / "nft-state").write_text(after.replace("udp sport 67 udp dport 68 accept", "udp sport 67 udp dport 68 drop"))
        self.run_host("--verify", success=False)

    def test_inactive_firewall_service_prevents_boot(self):
        self.bake()
        self.run_host("--enforce")
        (self.root / "run").mkdir()
        (self.root / "run/adp-job-ready").touch()
        (self.root / "inactive").touch()
        self.run_host("--verify", success=False)
        self.run_host("--boot", success=False)
        self.run_host("--allow", "10.0.0.10", "10.0.0.14", success=False)

    def test_interface_is_resolved_on_each_host_and_refresh_preserves_destination_policy(self):
        self.bake()
        (self.root / "sys/class/net/ens5").rename(self.root / "sys/class/net/enp39s0")
        self.run_host("--enforce")
        self.run_host("--allow", "10.0.0.10", "10.0.0.14")
        self.run_host("--enforce")
        policy = (self.root / "etc/adp/job-firewall.nft").read_text()
        self.assertIn('define adp_interface = "enp39s0"', policy)
        self.assertIn("ip daddr 10.0.0.10 tcp dport 443 accept", policy)
        self.assertIn("ip daddr 10.0.0.14 tcp dport 443 accept", policy)
        self.run_host("--network-verify")

    def test_ambiguous_or_unverified_hardware_refuses_admission(self):
        self.bake()
        self.run_host("--enforce")
        device = self.root / "sys/class/net/ens5/device"
        for filename, changed in (("vendor", "0xffff"), ("class", "0x030000")):
            original = (device / filename).read_text()
            with self.subTest(filename=filename):
                (device / filename).write_text(changed)
                self.run_host("--network-verify", success=False)
                self.run_host("--allow", "10.0.0.10", "10.0.0.14", success=False)
                self.assertFalse((self.root / "run/adp-job-ready").exists())
                (device / filename).write_text(original)
        original_driver = (device / "driver").readlink()
        (device / "driver").unlink()
        (device / "driver").symlink_to(self.root / "sys/class/net/docker0")
        self.run_host("--enforce", success=False)
        (device / "driver").unlink()
        (device / "driver").symlink_to(original_driver)
        self.physical_interface("ens6")
        self.run_host("--enforce", success=False)
        self.assertFalse((self.root / "etc/adp/job-firewall.loaded").exists())


    def test_bridge_names_cannot_admit_ssh_or_dhcp_on_either_policy(self):
        self.bake()
        for bridge in ("docker0", "arbitrary-job-bridge", "br-per-build-42"):
            (self.root / "sys/class/net" / bridge).mkdir(exist_ok=True)
        self.run_host("--enforce")
        before = (self.root / "etc/adp/job-firewall.nft").read_text()
        self.run_host("--allow", "10.0.0.10", "10.0.0.14")
        after = (self.root / "etc/adp/job-firewall.nft").read_text()
        for policy in (before, after):
            self.assertIn('define adp_interface = "ens5"', policy)
            incoming = policy.split("chain input {", 1)[1].split("chain forward {", 1)[0]
            for denial in ("iifname != $adp_interface tcp dport 22 drop",
                           "iifname != $adp_interface udp sport 67 udp dport 68 drop"):
                self.assertLess(incoming.index(denial), incoming.index("ct state established,related accept"))
            self.assertIn("iifname $adp_interface udp sport 67 udp dport 68 accept", incoming)
            self.assertNotIn("    tcp dport 22 accept", incoming)
            for bridge in ("docker0", "arbitrary-job-bridge", "br-per-build-42"):
                self.assertNotIn(f'iifname "{bridge}" tcp dport 22 accept', incoming)
        self.assertNotIn("iifname $adp_interface tcp dport 22 accept", before)
        self.assertIn("iifname $adp_interface tcp dport 22 accept", after)

if __name__ == "__main__":
    unittest.main()
