"""Profile selection, repeatability and fleet DB budget without a live cluster."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "platform/scripts/render-gateway-capacity.py"
spec = importlib.util.spec_from_file_location("gateway_capacity_render", SCRIPT)
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
BASE = ROOT / "modules/gateway/k8s/deployment.yaml"


class GatewayCapacityRenderTests(unittest.TestCase):
    def test_profiles_render_without_cluster_configuration_or_discovery_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KUBECONFIG": os.devnull, "HOME": directory}
        ):
            for profile, budget in (("default", (None,) * 4), ("llm-proxy", (402, 3, 80, 1))):
                with self.subTest(profile=profile):
                    bundle = renderer.checked_bundle(BASE, profile, budget)
                    self.assertEqual(set(bundle), {"deployment", "hpa", "pdb"})
                    self.assertEqual(bundle["deployment"]["spec"]["replicas"], 2)
                    self.assertEqual(bundle["hpa"]["spec"]["minReplicas"], 2)
                    self.assertEqual(bundle["pdb"]["spec"]["maxUnavailable"], 1)

    def test_default_and_opt_in_merge_preserve_security_image_and_probes(self):
        baseline = renderer.checked_bundle(BASE, "default", (None,) * 4)
        profile = renderer.checked_bundle(BASE, "llm-proxy", (402, 3, 80, 1))
        self.assertEqual(baseline, renderer.checked_bundle(BASE, "default", (None,) * 4))
        self.assertEqual(profile, renderer.checked_bundle(BASE, "llm-proxy", (402, 3, 80, 1)))
        for bundle in (baseline, profile):
            self.assertEqual(bundle["hpa"]["spec"]["minReplicas"], 2)
            self.assertEqual(bundle["hpa"]["spec"]["maxReplicas"], 12)
            self.assertEqual(bundle["pdb"]["spec"]["maxUnavailable"], 1)
            container = bundle["deployment"]["spec"]["template"]["spec"]["containers"][0]
            original = baseline["deployment"]["spec"]["template"]["spec"]["containers"][0]
            for field in ("image", "securityContext", "startupProbe", "readinessProbe", "livenessProbe"):
                self.assertEqual(container.get(field), original.get(field))
            self.assertEqual(bundle["deployment"]["spec"]["replicas"], 2)
        selected = profile["deployment"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(selected["resources"]["requests"]["cpu"], "1")
        self.assertEqual(selected["args"][-1], "2")
        self.assertEqual(profile["deployment"]["spec"]["strategy"]["rollingUpdate"]["maxSurge"], 1)

    def test_baseline_resources_and_natural_cooldown_are_profile_specific(self):
        configurations = (
            ("default", (None,) * 4, {"cpu": "250m", "memory": "2Gi"},
             {"cpu": "1000m", "memory": "3Gi"}, 600, 120),
            ("llm-proxy", (402, 3, 80, 1), {"cpu": "1", "memory": "1Gi"},
             {"cpu": "2", "memory": "2Gi"}, 120, 60),
        )
        for name, budget, requests, limits, window, period in configurations:
            with self.subTest(profile=name):
                bundle = renderer.checked_bundle(BASE, name, budget)
                self.assertEqual(bundle["hpa"]["spec"]["minReplicas"], 2)
                self.assertEqual(bundle["deployment"]["spec"]["replicas"], 2)
                self.assertEqual(bundle["pdb"]["spec"]["maxUnavailable"], 1)
                self.assertEqual(bundle["deployment"]["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"], 0)
                container = bundle["deployment"]["spec"]["template"]["spec"]["containers"][0]
                self.assertEqual(container["resources"], {"requests": requests, "limits": limits})
                down = bundle["hpa"]["spec"]["behavior"]["scaleDown"]
                self.assertEqual(down["stabilizationWindowSeconds"], window)
                self.assertEqual(down["policies"], [{"type": "Pods", "value": 1, "periodSeconds": period}])

        launcher = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        workflow = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
        for entrypoint in (launcher, workflow):
            self.assertNotIn("kubectl scale deployment/bedrockgateway", entrypoint)
            self.assertNotIn("kubectl patch hpa/bedrockgateway", entrypoint)

    def test_pinned_older_source_receives_memory_headroom_without_changing_cpu(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "old.yaml"
            manifest.write_text(BASE.read_text().replace("memory: 2Gi", "memory: 1536Mi")
                                .replace("memory: 3Gi", "memory: 2048Mi"))
            bundle = renderer.checked_bundle(manifest, "default", (None,) * 4)
            resources = bundle["deployment"]["spec"]["template"]["spec"]["containers"][0]["resources"]
            self.assertEqual(resources, {"requests": {"cpu": "250m", "memory": "2Gi"},
                                         "limits": {"cpu": "1000m", "memory": "3Gi"}})
            self.assertEqual(bundle, renderer.checked_bundle(manifest, "default", (None,) * 4))

    def test_legacy_implicit_four_worker_image_retains_its_pool_budget(self):
        legacy = renderer.render(["patch", "--local", "--type", "merge", "-p", "{}", "-f", str(BASE), "-o", "json"])
        container = legacy["spec"]["template"]["spec"]["containers"][0]
        container.pop("command")
        container.pop("args")
        for item in container["env"]:
            if item["name"] == "BG_RDS_POOL_SIZE":
                item["value"] = "2"
            elif item["name"] == "BG_RDS_POOL_MAX_OVERFLOW":
                item["value"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "legacy.json"
            manifest.write_text(json.dumps(legacy))
            bundle = renderer.checked_bundle(manifest, "default", (402, 3, 80, 1))
        rendered = bundle["deployment"]["spec"]["template"]["spec"]["containers"][0]
        self.assertNotIn("command", rendered)
        self.assertNotIn("args", rendered)
        values = {item["name"]: item.get("value") for item in rendered["env"]}
        self.assertEqual((values["BG_RDS_POOL_SIZE"], values["BG_RDS_POOL_MAX_OVERFLOW"]), ("2", "1"))

    def test_memory_floor_preserves_larger_settings_and_refuses_invalid_values(self):
        resources = {"requests": {"memory": "3Gi"}, "limits": {"memory": "4096Mi"}}
        renderer.default_memory_headroom(resources)
        self.assertEqual(resources, {"requests": {"memory": "3Gi"}, "limits": {"memory": "4096Mi"}})
        for request, limit in (("0", "3Gi"), ("4Gi", "3Gi"), ("bad", "3Gi")):
            with self.subTest(request=request, limit=limit), self.assertRaises(ValueError):
                renderer.default_memory_headroom({"requests": {"memory": request}, "limits": {"memory": limit}})

    def test_disabled_or_unsafe_cooldown_and_missing_resource_request_fail_before_apply(self):
        original_render = renderer.render
        for invalid in ("disabled", "missing-period", "missing-requests", "disruption"):
            with self.subTest(invalid=invalid):
                def mutated_render(arguments):
                    document = original_render(arguments)
                    if invalid == "disabled" and document["kind"] == "HorizontalPodAutoscaler":
                        document["spec"]["behavior"]["scaleDown"]["selectPolicy"] = "Disabled"
                    elif invalid == "missing-period" and document["kind"] == "HorizontalPodAutoscaler":
                        document["spec"]["behavior"]["scaleDown"]["policies"][0].pop("periodSeconds")
                    elif invalid == "missing-requests" and document["kind"] == "Deployment":
                        document["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"].pop("memory")
                    elif invalid == "disruption" and document["kind"] == "PodDisruptionBudget":
                        document["spec"]["maxUnavailable"] = 2
                    return document

                with patch.object(renderer, "render", side_effect=mutated_render):
                    with self.assertRaisesRegex(ValueError, "cooldown|requests and limits"):
                        renderer.checked_bundle(BASE, "default", (None,) * 4)

    def test_partial_budget_unknown_profile_and_expensive_pool_fail_closed(self):
        for profile, budget in (("bad", (None,) * 4), ("llm-proxy", (None,) * 4),
                                ("llm-proxy", (300, 3, 80, 1)), ("llm-proxy", (402, 3, 80, 12)),
                                ("llm-proxy", (402, 3, None, 0)), ("default", (402, -1, 0, 0))):
            with self.subTest(profile=profile, budget=budget):
                with self.assertRaises(ValueError):
                    renderer.checked_bundle(BASE, profile, budget)
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "unsafe.yaml"
            manifest.write_text(BASE.read_text().replace('name: BG_RDS_SPLIT_POOL_ENABLED\n              value: "false"',
                                                         'name: BG_RDS_SPLIT_POOL_ENABLED\n              value: "true"'))
            with self.assertRaisesRegex(ValueError, "Split DB pools"):
                renderer.checked_bundle(manifest, "default", (None,) * 4)

    def test_missing_selection_defaults_but_outage_and_invalid_selection_refuse(self):
        with patch.object(renderer, "selected_parameter", return_value=None):
            self.assertEqual(renderer.selected_capacity("test"), ("default", (None,) * 4))
        with patch.object(renderer, "selected_parameter", return_value="typo"):
            with self.assertRaisesRegex(ValueError, "Unknown capacity profile"):
                renderer.selected_capacity("test")
        for stderr in ("AccessDenied", "network error"):
            with patch.object(renderer.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", stderr)):
                with self.assertRaisesRegex(ValueError, "cannot be read"):
                    renderer.selected_capacity("test")
        with patch.object(renderer.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "ParameterNotFound")):
            self.assertEqual(renderer.selected_capacity("test"), ("default", (None,) * 4))
        with patch.object(renderer, "selected_parameter", side_effect=["llm-proxy", "402", "3", "80", "1"]):
            self.assertEqual(renderer.selected_capacity("test"), ("llm-proxy", (402, 3, 80, 1)))

    def test_admission_limits_are_opt_in_complete_and_reproducible(self):
        for value in (("2", "1", None), ("0", "1", "100"), ("2", "-1", "100"),
                      ("2", "1", "5001")):
            with self.assertRaises(ValueError):
                renderer.validate_admission(value)
        self.assertIsNone(renderer.validate_admission((None,) * 3))
        settings = renderer.validate_admission(("2", "1", "100"))
        bundle = renderer.checked_bundle(BASE, "default", (None,) * 4, admission=settings)
        container = bundle["deployment"]["spec"]["template"]["spec"]["containers"][0]
        fields = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual(fields["BG_MAX_ACTIVE_STREAMS_PER_WORKER"], "2")
        self.assertEqual(fields["BG_STREAM_ADMISSION_QUEUE_LIMIT"], "1")
        self.assertEqual(fields["BG_STREAM_ADMISSION_WAIT_MS"], "100")
        self.assertNotEqual(bundle["deployment"]["metadata"]["annotations"]["adp.aws-e.example/capacity-selection"],
                            renderer.checked_bundle(BASE, "default", (None,) * 4)["deployment"]["metadata"]["annotations"]["adp.aws-e.example/capacity-selection"])

    def test_reapply_preserves_bundle_and_changed_selection_refuses_apply(self):
        original = renderer.checked_bundle(BASE, "default", (None,) * 4, admission=(2, 1, 100))
        self.assertEqual(original, renderer.checked_bundle(BASE, "default", (None,) * 4,
                                                           admission=(2, 1, 100)))
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "deployment.json").write_text(json.dumps(original["deployment"]))
            with patch.object(renderer, "selected_capacity", return_value=("default", (None,) * 4)):
                with patch.object(renderer, "selected_admission", return_value=(2, 1, 100)):
                    renderer.verify_selection("test", bundle)
                for changed in ((2, 2, 100), None):
                    with patch.object(renderer, "selected_admission", return_value=changed):
                        with self.assertRaisesRegex(ValueError, "changed during apply"):
                            renderer.verify_selection("test", bundle)
            self.assertEqual(json.loads((bundle / "deployment.json").read_text()), original["deployment"])

    def test_cli_refuses_existing_bundle_and_invalid_values_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            command = ["python3", str(SCRIPT), "--manifest", str(BASE), "--profile", "default", "--output-dir", directory]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = (Path(directory) / "deployment.json").read_bytes()
            self.assertEqual(json.loads(content)["kind"], "Deployment")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(content, (Path(directory) / "deployment.json").read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            command = ["python3", str(SCRIPT), "--manifest", str(BASE), "--profile", "llm-proxy", "--output-dir", directory]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(list(Path(directory).iterdir()), [])
