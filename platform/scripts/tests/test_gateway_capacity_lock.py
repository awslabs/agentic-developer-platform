"""Synthetic conditional DynamoDB ownership and guarded recovery."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
lock_spec = importlib.util.spec_from_file_location("gateway_capacity_lock", SCRIPTS / "gateway-capacity-lock.py")
lock = importlib.util.module_from_spec(lock_spec)
lock_spec.loader.exec_module(lock)
render_spec = importlib.util.spec_from_file_location("gateway_capacity_render", SCRIPTS / "render-gateway-capacity.py")
render = importlib.util.module_from_spec(render_spec)
render_spec.loader.exec_module(render)


class ConditionalTable:
    def __init__(self):
        self.item = None
        self.calls = []

    def call(self, region, *args):
        self.calls.append((region, args))
        operation = args[1]
        if operation == "put-item":
            if self.item:
                raise RuntimeError("ConditionalCheckFailed")
            self.item = json.loads(args[args.index("--item") + 1])
        elif operation == "get-item":
            return {"Item": copy.deepcopy(self.item)} if self.item else {}
        elif operation == "delete-item":
            conditions = json.loads(args[args.index("--expression-attribute-values") + 1])
            if (not self.item or self.item["Owner"] != conditions[":owner"]
                    or self.item["Started"] != conditions[":started"]):
                raise RuntimeError("ConditionalCheckFailed")
            self.item = None
        else:
            raise AssertionError(args)
        return {}


class GatewayCapacityLockTests(unittest.TestCase):
    def test_overlapping_callers_and_old_owner_cannot_delete_new_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            table = ConditionalTable()
            key = lock.target("123456789012", "us-east-1", "test", "adp-test-eks-cluster", "adp-gateway")
            with patch.object(lock, "aws", side_effect=table.call):
                previous = lock.acquire("us-east-1", key, Path(directory) / "previous")
                self.assertEqual((Path(directory) / "previous").stat().st_mode & 0o777, 0o600)
                with self.assertRaises(RuntimeError):
                    lock.acquire("us-east-1", key, Path(directory) / "overlap")
                self.assertFalse((Path(directory) / "overlap").exists())
                lock.verify("us-east-1", previous)
                lock.release("us-east-1", previous)
                newer = lock.acquire("us-east-1", key, Path(directory) / "newer")
                with self.assertRaises(RuntimeError):
                    lock.verify("us-east-1", previous)
                with self.assertRaises(RuntimeError):
                    lock.release("us-east-1", previous)
                self.assertEqual(table.item["Owner"], {"S": newer["owner"]})
                lock.release("us-east-1", newer)
                self.assertIsNone(table.item)
                self.assertTrue(all(region == "us-east-1" for region, _ in table.calls))

    def test_recovery_requires_stopped_owner_and_unchanged_target(self):
        with tempfile.TemporaryDirectory() as directory:
            table = ConditionalTable()
            key = lock.target("123456789012", "us-east-1", "test", "adp-test-eks-cluster", "adp-gateway")
            receipt_body = json.dumps({"metadata": {"uid": "pod-deployment", "resourceVersion": "41"}})
            with patch.object(lock, "aws", side_effect=table.call), patch.object(
                lock.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, receipt_body, "")
            ):
                previous = lock.acquire("us-east-1", key, Path(directory) / "previous")
                resources = {kind: {"uid": "pod-deployment", "resourceVersion": "41"}
                             for kind in ("deployment", "hpa", "pdb")}
                receipt = {**previous, "stopped": True, "resources": resources}
                for field, value in (("stopped", False), ("target", "different"), ("resources", {})):
                    changed = dict(receipt, **{field: value})
                    with self.assertRaises((ValueError, RuntimeError)):
                        lock.recover("us-east-1", key, changed)
                    lock.verify("us-east-1", previous)
                changed_resource = dict(resources, pdb={"uid": "newer", "resourceVersion": "42"})
                with self.assertRaises(RuntimeError):
                    lock.recover("us-east-1", key, dict(receipt, resources=changed_resource))
                lock.recover("us-east-1", key, receipt)
                self.assertIsNone(table.item)
                newer = lock.acquire("us-east-1", key, Path(directory) / "newer")
                with self.assertRaises(RuntimeError):
                    lock.recover("us-east-1", key, receipt)
                self.assertEqual(table.item["Owner"], {"S": newer["owner"]})

    def test_both_deployment_paths_own_selection_through_rollout_and_readback(self):
        import yaml

        root = SCRIPTS.parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/gateway-deploy.yml").read_text())
        steps = next(job["steps"] for job in workflow["jobs"].values()
                     if any(step.get("name") == "Acquire gateway capacity ownership" for step in job.get("steps", [])))
        names = [step.get("name") for step in steps]
        self.assertLess(names.index("Acquire gateway capacity ownership"),
                        names.index("Ensure namespace and render K8s prerequisites"))
        self.assertLess(names.index("Migrate schema then roll out new image"),
                        names.index("Release gateway capacity ownership"))
        release = steps[names.index("Release gateway capacity ownership")]
        self.assertIn("always()", release["if"])
        run = steps[names.index("Migrate schema then roll out new image")]["run"]
        self.assertLess(run.index('--verify-bundle "$CAPACITY_BUNDLE"'),
                        run.index('apply-deployment.py'))
        self.assertLess(run.index('apply-deployment.py'), run.index('--verify-applied "$CAPACITY_BUNDLE"'))

        launcher = (root / "platform/scripts/deploy-all.sh").read_text()
        self.assertLess(launcher.index("capacity_lock acquire"), launcher.index("# Render deployment settings"))
        self.assertLess(launcher.index("capacity_lock release"), launcher.index("trap - EXIT"))
        migration = launcher.index('migrate_gateway_before_rollout "$RENDERED_DEPLOYMENT"')
        apply = launcher.index('DEPLOYMENT_APPLY_RESULT=$(python3 scripts/apply-gateway-deployment.py')
        self.assertLess(migration, apply)
        self.assertIn('trap capacity_cleanup EXIT', launcher)
        self.assertIn('--verify-applied "$CAPACITY_BUNDLE"', launcher)

    def test_profile_selection_is_serialized_and_refuses_stale_rollback_version(self):
        with tempfile.TemporaryDirectory() as directory:
            table = ConditionalTable()
            key = lock.target("123456789012", "us-east-1", "test", "adp-test-eks-cluster", "adp-gateway")
            writes = []

            def command(region, *args):
                if args[0] == "ssm":
                    writes.append(args)
                    return {}
                return table.call(region, *args)

            with patch.object(lock, "aws", side_effect=command):
                existing = lock.acquire("us-east-1", key, Path(directory) / "deployment")
                with self.assertRaises(RuntimeError):
                    lock.select("us-east-1", "test", key, Path(directory) / "rollback", "default", 4)
                self.assertEqual(writes, [])
                lock.release("us-east-1", existing)
                with patch.object(lock, "profile_parameter", return_value=("llm-proxy", 5)):
                    with self.assertRaisesRegex(ValueError, "version changed"):
                        lock.select("us-east-1", "test", key, Path(directory) / "rollback", "default", 4)
                self.assertIsNone(table.item)
                self.assertFalse((Path(directory) / "rollback").exists())
                self.assertEqual(writes, [])
                with patch.object(lock, "profile_parameter", side_effect=[("llm-proxy", 5), ("default", 6)]):
                    lock.select("us-east-1", "test", key, Path(directory) / "rollback", "default", 5)
                self.assertEqual(len(writes), 1)
                self.assertIsNone(table.item)

    def test_readback_detects_changed_profile_metrics_and_pod_resources(self):
        admission_mock = patch.object(render, "selected_admission", return_value=None)
        admission_mock.start()
        self.addCleanup(admission_mock.stop)
        base = SCRIPTS.parents[1] / "modules/gateway/k8s/deployment.yaml"
        bundle = render.checked_bundle(base, "llm-proxy", (402, 3, 80, 1))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, document in bundle.items():
                (root / (name + ".json")).write_text(json.dumps(document))

            live = copy.deepcopy(bundle)
            responses = {"deployment/bedrockgateway": "deployment", "hpa/bedrockgateway": "hpa",
                         "pdb/bedrockgateway-pdb": "pdb"}

            def kubectl(command, **_):
                return subprocess.CompletedProcess(command, 0,
                                                   json.dumps(live[responses[command[5]]]), "")

            with patch.object(render, "selected_capacity", return_value=("llm-proxy", (402, 3, 80, 1))), \
                 patch.object(render.subprocess, "run", side_effect=kubectl):
                render.verify_applied("test", root)
                current = live["deployment"]["spec"]["template"]["spec"]["containers"][0]
                current["resources"]["requests"]["memory"] = "1024Mi"
                live["hpa"]["spec"]["metrics"][0]["resource"]["target"]["averageValue"] = "1"
                render.verify_applied("test", root)
                live["hpa"]["spec"]["metrics"][0]["resource"]["target"]["averageValue"] = "2000m"
                with self.assertRaisesRegex(ValueError, "HPA differs"):
                    render.verify_applied("test", root)
                live["hpa"] = copy.deepcopy(bundle["hpa"])
                live["deployment"]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "3"
                with self.assertRaisesRegex(ValueError, "pod configuration"):
                    render.verify_applied("test", root)
                live["deployment"] = copy.deepcopy(bundle["deployment"])
                live["deployment"]["metadata"]["annotations"]["adp.aws-e.example/capacity-profile"] = "default"
                with self.assertRaisesRegex(ValueError, "selection differs"):
                    render.verify_applied("test", root)

    def test_resume_checks_deployed_image_before_hpa_exists(self):
        base = SCRIPTS.parents[1] / "modules/gateway/k8s/deployment.yaml"
        bundle = render.checked_bundle(base, "default", (None,) * 4)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name, document in bundle.items():
                (output / (name + ".json")).write_text(json.dumps(document))
            deployed = copy.deepcopy(bundle["deployment"])

            def read_deployment(command, **_):
                self.assertEqual(command[5], "deployment/bedrockgateway")
                return subprocess.CompletedProcess(command, 0, json.dumps(deployed), "")

            with patch.object(render, "selected_capacity", return_value=("default", (None,) * 4)), \
                 patch.object(render, "selected_admission", return_value=None), \
                 patch.object(render.subprocess, "run", side_effect=read_deployment) as read:
                render.verify_applied("test", output, deployment_only=True)
                self.assertEqual(read.call_count, 1)
                for field, value in (("image", "example.test/newer@sha256:" + "a" * 64),
                                     ("command", ["python"]), ("args", ["--workers", "4"])):
                    with self.subTest(field=field):
                        deployed = copy.deepcopy(bundle["deployment"])
                        deployed["spec"]["template"]["spec"]["containers"][0][field] = value
                        with self.assertRaisesRegex(ValueError, "pod configuration differs"):
                            render.verify_applied("test", output, deployment_only=True)

    def test_selection_changes_and_read_failure_refuse_remaining_apply(self):
        admission_mock = patch.object(render, "selected_admission", return_value=None)
        admission_mock.start()
        self.addCleanup(admission_mock.stop)
        base = SCRIPTS.parents[1] / "modules/gateway/k8s/deployment.yaml"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            bundle = render.checked_bundle(base, "llm-proxy", (402, 3, 80, 1))
            (output / "deployment.json").write_text(json.dumps(bundle["deployment"]))
            with patch.object(render, "selected_capacity", return_value=("llm-proxy", (402, 3, 80, 1))):
                render.verify_selection("test", output)
            for profile, budget in (("default", (None,) * 4), ("llm-proxy", (402, 3, 81, 1))):
                with patch.object(render, "selected_capacity", return_value=(profile, budget)):
                    with self.assertRaisesRegex(ValueError, "selection changed"):
                        render.verify_selection("test", output)
            with patch.object(render, "selected_capacity", side_effect=ValueError("SSM unavailable")):
                with self.assertRaisesRegex(ValueError, "SSM unavailable"):
                    render.verify_selection("test", output)

    def test_overlap_during_apply_and_selection_drift_leave_existing_bundle_untouched(self):
        table = ConditionalTable()
        key = lock.target("123456789012", "us-east-1", "test", "adp-test-eks-cluster", "adp-gateway")
        base = SCRIPTS.parents[1] / "modules/gateway/k8s/deployment.yaml"
        bundle = render.checked_bundle(base, "default", (None,) * 4)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            selected = output / "deployment.json"
            selected.write_text(json.dumps(bundle["deployment"]))
            original = selected.read_bytes()
            with patch.object(lock, "aws", side_effect=table.call), \
                 patch.object(render, "selected_admission", return_value=None):
                active = lock.acquire("us-east-1", key, output / "active-owner")
                with patch.object(render, "selected_capacity", return_value=("default", (None,) * 4)):
                    lock.verify("us-east-1", active)
                    render.verify_selection("test", output)
                    with self.assertRaises(RuntimeError):
                        lock.select("us-east-1", "test", key, output / "competing-owner", "llm-proxy", 5)
                    lock.verify("us-east-1", active)
                    render.verify_selection("test", output)
                with patch.object(render, "selected_capacity", return_value=("llm-proxy", (402, 3, 80, 1))):
                    with self.assertRaisesRegex(ValueError, "selection changed"):
                        render.verify_selection("test", output)
                self.assertEqual(selected.read_bytes(), original)
                self.assertFalse((output / "competing-owner").exists())
                self.assertFalse(any(args[0] == "ssm" for _, args in table.calls))
                lock.verify("us-east-1", active)
                lock.release("us-east-1", active)
