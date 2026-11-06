"""Exercise the one-use manager startup path without AWS credentials."""

import importlib.util
import json
import os
import signal
import subprocess
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_manager", ROOT / "scripts/prepare_manager.py")
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)
NAME = "adp-test-gitlab-job-42"
INSTANCE = "i-0123456789abcdef0"
NONCE = "12345678-1234-1234-1234-123456789abc"


def group(*, instances=(), desired=0, group_id="42", max_size=2):
    return {
        "AutoScalingGroupName": NAME, "MinSize": 0, "MaxSize": max_size,
        "DesiredCapacity": desired,
        "Tags": [{"Key": key, "Value": value} for key, value in (
            ("Component", "gitlab-job"), ("Environment", "test"), ("GroupId", group_id)
        )],
        "Instances": [{"InstanceId": instance, "ProtectedFromScaleIn": True}
                      for instance in instances],
    }


SYSTEM_ID = "r_" + "1" * 12


def registration_paths(directory):
    config = directory / "config"
    credentials = directory / "credentials"
    config.mkdir()
    credentials.mkdir()
    (config / ".runner_system_id").write_text(SYSTEM_ID + "\n")
    (config / ".adp-verified-runner.json").write_text(json.dumps(
        {"runner_id": 101, "system_id": SYSTEM_ID}))
    (config / "config.toml").write_text('[[runners]]\nid = 101\nname = "gitlab-group-42"\n'
                                        'token = "glrt-synthetic-canary"\ntls-ca-file = "/secrets/ca.pem"\n')
    (credentials / "runner-token").write_text("glrt-synthetic-canary")
    return config, credentials


class ManagerLifecycleTests(unittest.TestCase):
    def test_guard_sigterm_exits_cleanly_instead_of_waiting_for_kubernetes_kill(self):
        code = """
import sys, time, types
sys.path.insert(0, sys.argv.pop(1))
import prepare_manager as manager
sys.modules['boto3'] = types.SimpleNamespace(client=lambda *a, **kw: None)
def serve(*args):
    print('ready', flush=True)
    while True:
        time.sleep(1)
manager.serve = serve
manager.main()
"""
        process = subprocess.Popen([os.sys.executable, "-c", code, str(ROOT / "scripts"),
            "--environment", "test", "--region", "us-east-1", "--group-id", "42"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_fresh_start_and_each_finished_failed_or_cancelled_host_drain(self):
        for outcome in ("success", "failure", "cancelled", "manager-restart"):
            with self.subTest(outcome=outcome):
                aws = Mock()
                aws.describe_auto_scaling_groups.side_effect = [
                    {"AutoScalingGroups": [group(instances=(INSTANCE,), desired=1)]},
                    {"AutoScalingGroups": [group()]},
                ]
                with tempfile.TemporaryDirectory() as directory:
                    heartbeat = Mock()
                    config, credentials = registration_paths(Path(directory))
                    manager.acknowledge(Path(directory), NONCE, aws, heartbeat, "test", "42",
                                        config, credentials, drain_owned=lambda *args: manager.drain(
                                            *args, clock=lambda: 0, pause=lambda _: None))
                    self.assertEqual((Path(directory) / "ack").read_text(), NONCE + "\n")
                aws.set_instance_protection.assert_called_once_with(
                    AutoScalingGroupName=NAME, InstanceIds=[INSTANCE], ProtectedFromScaleIn=False)
                aws.set_desired_capacity.assert_called_once_with(
                    AutoScalingGroupName=NAME, DesiredCapacity=0, HonorCooldown=False)
                aws.terminate_instance_in_auto_scaling_group.assert_not_called()
                self.assertEqual(heartbeat.put_parameter.call_args_list[0].kwargs, {
                    "Name": "/adp/test/gitlab-runners/42/registration-receipt",
                    "Value": json.dumps({"runner_id": 101, "system_id": SYSTEM_ID}, sort_keys=True, separators=(",", ":")),
                    "Type": "String", "Overwrite": True,
                })
                self.assertEqual(heartbeat.put_parameter.call_args_list[1].kwargs, {
                    "Name": "/adp/test/gitlab-runners/42/manager-alive", "Value": "active",
                    "Type": "String", "Overwrite": True,
                })
                self.assertNotIn("glrt-synthetic-canary", json.dumps(
                    [call.kwargs for call in heartbeat.put_parameter.call_args_list]))

    def test_old_ack_cannot_authorize_manager_when_drain_times_out(self):
        aws = Mock()
        aws.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [group(instances=(INSTANCE,), desired=0)]}
        ticks = iter((0, 1, 241))
        with tempfile.TemporaryDirectory() as directory:
            ack = Path(directory) / "ack"
            ack.write_text("other-instance\n")
            config, credentials = registration_paths(Path(directory))
            with self.assertRaises(manager.StartupError):
                manager.acknowledge(Path(directory), NONCE, aws, Mock(), "test", "42",
                                    config, credentials,
                                    drain_owned=lambda *args: manager.drain(
                                        *args, clock=lambda: next(ticks), pause=lambda _: None))
            self.assertEqual(ack.read_text(), "other-instance\n")
            aws.set_desired_capacity.assert_not_called()

    def test_unpublished_heartbeat_never_acknowledges_manager(self):
        aws = Mock()
        aws.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [group()]}
        ssm = Mock()
        ssm.put_parameter.side_effect = OSError("offline synthetic failure")
        with tempfile.TemporaryDirectory() as directory:
            config, credentials = registration_paths(Path(directory))
            with self.assertRaises(OSError):
                manager.acknowledge(Path(directory), NONCE, aws, ssm, "test", "42", config, credentials)
            self.assertFalse((Path(directory) / "ack").exists())
            self.assertFalse((Path(directory) / "lease").exists())

    def test_mismatched_registration_never_drains_or_publishes(self):
        for changed, replacement in ((".runner_system_id", "r_" + "2" * 12),
                                     (".runner_system_id", "s_" + "2" * 32),
                                     (".adp-verified-runner.json", '{"runner_id":102,"system_id":"' + SYSTEM_ID + '"}'),
                                     ("config.toml", '[[runners]]\nid = 102\nname = "gitlab-group-42"\n'
                                                     'token = "glrt-synthetic-canary"\ntls-ca-file = "/secrets/ca.pem"\n')):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                config, credentials = registration_paths(directory)
                (config / changed).write_text(replacement)
                autoscaling = Mock()
                ssm = Mock()
                with self.assertRaises(manager.StartupError):
                    manager.acknowledge(directory, NONCE, autoscaling, ssm, "test", "42", config, credentials)
                autoscaling.assert_not_called()
                ssm.assert_not_called()
                self.assertFalse((directory / "ack").exists())

    def test_registered_token_and_tls_path_must_match_loader(self):
        for old, replacement in (("glrt-synthetic-canary", "glrt-other"),
                                 ("/secrets/ca.pem", "/missing-ca")):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary:
                config, credentials = registration_paths(Path(temporary))
                configuration = config / "config.toml"
                configuration.write_text(configuration.read_text().replace(old, replacement))
                with self.assertRaises(manager.StartupError):
                    manager.verified_registration(config, credentials, "42")

    def test_failed_runner_verify_never_requests_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            script = (ROOT / "templates" / "manager-prestart.sh").read_text()
            self.assertIn("/entrypoint verify --url= >/dev/null 2>&1", script)
            script = script.replace("/entrypoint verify --url= >/dev/null 2>&1", "false")
            script = script.replace("/runner-guard/", temporary + "/")
            entrypoint = directory / "prestart.sh"
            entrypoint.write_text(script)
            result = subprocess.run(["bash", str(entrypoint)], capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((directory / "request").exists())

    def test_receipt_publication_failure_never_acknowledges(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config, credentials = registration_paths(directory)
            ssm = Mock()
            ssm.put_parameter.side_effect = OSError("synthetic receipt write failed")
            with self.assertRaises(OSError):
                manager.acknowledge(directory, NONCE, Mock(), ssm, "test", "42", config, credentials,
                                    drain_owned=lambda *args: None)
            self.assertFalse((directory / "ack").exists())
            self.assertFalse((directory / "lease").exists())

    def test_wrong_group_or_unsafe_capacity_is_never_modified(self):
        for snapshot in (group(group_id="43"), group(max_size=3),
                         group(instances=(INSTANCE,), desired=3)):
            aws = Mock()
            aws.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [snapshot]}
            with self.subTest(snapshot=snapshot), self.assertRaises(manager.StartupError):
                manager.drain(aws, "test", "42")
            aws.set_desired_capacity.assert_not_called()
            aws.set_instance_protection.assert_not_called()

    def test_prestart_requires_fresh_receipt_before_starting_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = Path(directory)
            script = (ROOT / "templates" / "manager-prestart.sh").read_text()
            script = script.replace("/runner-guard/", directory + "/")
            script = script.replace("/entrypoint verify --url= >/dev/null 2>&1", "true")
            script = script.replace('kill -TERM "$manager_pid"', 'exit 1')
            script = script.replace("+ 300", "+ 2").replace("sleep 1", "sleep 0.01")
            (guard / "ack").write_text("old-manager-receipt\n")
            entrypoint = guard / "prestart.sh"
            entrypoint.write_text(script)
            process = subprocess.Popen(["bash", str(entrypoint)], start_new_session=True)
            try:
                for _ in range(100):
                    if (guard / "request").exists():
                        break
                    time.sleep(0.01)
                self.assertTrue((guard / "request").exists())
                self.assertIsNone(process.poll())
                nonce = (guard / "request").read_text().strip()
                self.assertNotEqual(nonce, "old-manager-receipt")
                (guard / "ack").write_text(nonce + "\n")
                self.assertEqual(process.wait(timeout=3), 0)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
                time.sleep(0.1)

    def test_invalid_nonce_never_invokes_aws(self):
        with tempfile.TemporaryDirectory() as directory:
            aws = Mock()
            with self.assertRaises(manager.StartupError):
                config, credentials = registration_paths(Path(directory))
                manager.acknowledge(Path(directory), "other-instance", aws, Mock(), "test", "42",
                                    config, credentials)
            aws.assert_not_called()


if __name__ == "__main__":
    unittest.main()
