import copy
import importlib.util
import json
import os
from pathlib import Path
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("host_observation", ROOT / "host_observation.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
CONTAINER = "a" * 64


class HostObservationTests(unittest.TestCase):
    def setUp(self):
        self.expected = {"job_id": 11, "project_id": 17, "pipeline_id": 23, "source_revision": "b" * 40}
        self.canary = "glrt-synthetic-observer-redaction-canary"
        self.raw = {
            "Id": CONTAINER, "Image": "sha256:" + "c" * 64, "Created": "2026-11-01T00:00:01Z",
            "Config": {"User": "10001:10001", "Env": ["CI_JOB_TOKEN=" + self.canary],
                       "Cmd": [self.canary], "Healthcheck": {"Test": [self.canary]},
                       "Labels": {host.PREFIX + "managed": "true", host.PREFIX + "job.id": "11",
                                  host.PREFIX + "project.id": "17", host.PREFIX + "pipeline.id": "23",
                                  host.PREFIX + "job.sha": "b" * 40, host.PREFIX + "type": "build",
                                  host.PREFIX + "runner.id": self.canary, "custom-data": self.canary}},
            "HostConfig": {"Privileged": False, "CapAdd": None, "CapDrop": ["ALL"],
                           "SecurityOpt": ["no-new-privileges:true"], "NetworkMode": "owned-job-network",
                           "PidMode": "", "IpcMode": "private", "Devices": [], "DeviceRequests": None,
                           "LogConfig": {"Config": {"token": self.canary}}},
            "State": {"Running": True, "Pid": 1234, "StartedAt": "2026-11-01T00:00:02Z", "Error": self.canary},
            "Mounts": [{"Type": "volume", "Source": "/var/lib/docker/volumes/owned/_data",
                        "Destination": "/builds", "RW": True}],
        }
        self.kernel = {"uids": [10001] * 4, "effective_capabilities_hex": "0000000000000000",
                       "no_new_privileges": True, "namespaces": {"net": {"same_as_host": False}}}

    def observe(self, **kwargs):
        return host.observation(CONTAINER, self.expected,
            inspect=kwargs.pop("inspect", lambda _: copy.deepcopy(self.raw)),
            kernel=kwargs.pop("kernel", lambda _: copy.deepcopy(self.kernel)),
            enforcement=kwargs.pop("enforcement", lambda: {"status": "unavailable"}), now=lambda: 123, **kwargs)

    def test_exports_only_scoped_kernel_and_docker_facts_without_canaries(self):
        result = self.observe()
        self.assertEqual(result["job_id"], 11)
        self.assertEqual(result["kernel"]["uids"], [10001] * 4)
        self.assertFalse(result["privileged"])
        self.assertEqual(result["mounts"][0]["Destination"], "/builds")
        self.assertNotIn(self.canary, json.dumps(result))
        self.assertNotIn("trusted_host_verified", result)
        self.assertNotEqual(result["acceptance"], "pass")

    def test_wrong_job_project_pipeline_source_or_unmanaged_container_is_refused(self):
        for key, value in (("job.id", "12"), ("project.id", "18"), ("pipeline.id", "24"),
                           ("job.sha", "d" * 40), ("managed", "false"), ("type", "unknown")):
            with self.subTest(label=key):
                previous = self.raw["Config"]["Labels"][host.PREFIX + key]
                self.raw["Config"]["Labels"][host.PREFIX + key] = value
                kernel = Mock()
                with self.assertRaises(host.ObservationError):
                    self.observe(kernel=kernel)
                kernel.assert_not_called()
                self.raw["Config"]["Labels"][host.PREFIX + key] = previous

    def test_stopped_container_cannot_invent_a_kernel_uid(self):
        self.raw["State"].update(Running=False, Pid=0)
        kernel = Mock()
        with self.assertRaisesRegex(host.ObservationError, "historical_kernel_observation_missing"):
            self.observe(kernel=kernel)
        kernel.assert_not_called()

    def test_restart_or_pid_reuse_invalidates_the_observation(self):
        for change in ({"Pid": 1235}, {"StartedAt": "2026-11-01T00:00:03Z"}, {"Running": False}):
            with self.subTest(change=change):
                later = copy.deepcopy(self.raw)
                later["State"].update(change)
                inspect = Mock(side_effect=[self.raw, later])
                with self.assertRaisesRegex(host.ObservationError, "container_changed"):
                    self.observe(inspect=inspect)

    def test_identity_change_during_inspection_is_incomplete(self):
        kernel = Mock(side_effect=[self.kernel, {**self.kernel, "uids": [0] * 4}])
        with self.assertRaisesRegex(host.ObservationError, "kernel_identity_changed"):
            self.observe(kernel=kernel)

    def test_missing_or_failed_guard_is_not_reported_as_verified_enforcement(self):
        result = self.observe(enforcement=Mock(side_effect=RuntimeError("synthetic-private-error")))
        self.assertEqual(result["host_enforcement"], {"status": "unavailable"})
        self.assertNotIn("synthetic-private-error", json.dumps(result))

    def test_guard_readback_rejects_failure_or_policy_change_and_retains_real_file_times(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {name: root / relative for name, relative in {
                "guard": "usr/local/libexec/adp-job-host", "policy": "etc/adp/job-firewall.nft",
                "loaded": "etc/adp/job-firewall.loaded", "ready": "run/adp-job-ready"}.items()}
            for name, path in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic " + name)
            os.utime(files["loaded"], ns=(1_000_000_000, 1_000_000_000))
            os.utime(files["ready"], ns=(2_000_000_000, 2_000_000_000))
            run = Mock(return_value=SimpleNamespace(returncode=0))
            result = host.enforcement_identity(root, run=run)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["denial_installed_at_unix_ns"], 1_000_000_000)
            self.assertEqual(result["executor_ready_at_unix_ns"], 2_000_000_000)
            self.assertEqual(run.call_args.args[0], [str(files["guard"]), "--verify"])
            with self.assertRaisesRegex(host.ObservationError, "verification_failed"):
                host.enforcement_identity(root, run=Mock(return_value=SimpleNamespace(returncode=1)))
            def changed(*args, **kwargs):
                files["policy"].write_text("changed during inspection")
                return SimpleNamespace(returncode=0)
            with self.assertRaisesRegex(host.ObservationError, "enforcement_changed"):
                host.enforcement_identity(root, run=changed)

    def test_startup_checks_network_guard_without_inventing_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("usr/local/libexec/adp-job-host", "etc/adp/job-firewall.nft",
                             "etc/adp/job-firewall.loaded"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic pinned guard")
            run = Mock(return_value=SimpleNamespace(returncode=0))
            result = host.enforcement_identity(root, run=run, ready_required=False)
            self.assertEqual(result["status"], "verified")
            self.assertIsNone(result["executor_ready_at_unix_ns"])
            self.assertEqual(run.call_args.args[0][-1], "--network-verify")
            with self.assertRaisesRegex(host.ObservationError, "host_executor_not_ready"):
                host.enforcement_identity(root, run=run)

    def test_startup_requires_real_service_invocations_and_no_admission(self):
        guard = Mock(return_value={"status": "verified", "executor_ready_at_unix_ns": None})
        run = Mock(side_effect=[SimpleNamespace(returncode=0, stdout="a" * 32 + "\n"),
                               SimpleNamespace(returncode=0, stdout="b" * 32 + "\n")])
        result = host.startup_identity(run=run, enforcement=guard)
        self.assertEqual(result["service_invocations"],
                         {"docker.service": "a" * 32, "adp-job-firewall.service": "b" * 32})
        guard.assert_called_once_with(ready_required=False)
        for code, value in ((0, ""), (0, "not-a-service-identity"), (1, "a" * 32)):
            with self.subTest(code=code, value=value), self.assertRaisesRegex(host.ObservationError, "invocation_missing"):
                host.startup_identity(run=Mock(return_value=SimpleNamespace(returncode=code, stdout=value)), enforcement=guard)
        with self.assertRaisesRegex(host.ObservationError, "observer_started_after_admission"):
            host.startup_identity(run=Mock(return_value=SimpleNamespace(returncode=0, stdout="a" * 32)),
                                  enforcement=Mock(return_value={"executor_ready_at_unix_ns": 1}))

    def test_unsafe_actual_settings_are_recorded_without_being_normalized_away(self):
        self.raw["HostConfig"].update(Privileged=True, CapAdd=["SYS_ADMIN"], NetworkMode="host",
                                    Devices=[{"PathOnHost": "/dev/console", "PathInContainer": "/dev/console",
                                              "CgroupPermissions": "rwm"}])
        self.raw["Mounts"].append({"Type": "bind", "Source": "/root/.aws", "Destination": "/credentials", "RW": True})
        self.kernel.update(uids=[0] * 4, effective_capabilities_hex="000001ffffffffff",
                           namespaces={"net": {"same_as_host": True}})
        result = self.observe()
        self.assertTrue(result["privileged"])
        self.assertEqual(result["kernel"]["uids"], [0] * 4)
        self.assertEqual(result["cap_add"], ["SYS_ADMIN"])
        self.assertEqual(result["devices"][0]["PathOnHost"], "/dev/console")
        self.assertEqual(result["mounts"][-1]["Source"], "/root/.aws")


if __name__ == "__main__":
    unittest.main()
