"""Execute both capacity selection entry points with synthetic SSM; never contact AWS."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


class GatewayCapacityWireTests(unittest.TestCase):
    def run_render(self, route, profile, admission=None, expect_success=True, bucket=""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aws = root / "aws"
            aws.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
values = {
    "insights-results-bucket": os.environ["TEST_INSIGHTS_BUCKET"],
    "capacity-profile": os.environ["TEST_CAPACITY_PROFILE"],
    "capacity-db-max": "402", "capacity-db-reserved": "3", "capacity-db-other": "80",
    "capacity-terminating-pods": "1",
    "capacity-max-active-streams-per-worker": os.environ["TEST_ACTIVE"],
    "capacity-stream-admission-queue-limit": os.environ["TEST_QUEUE"],
    "capacity-stream-admission-wait-ms": os.environ["TEST_WAIT"],
}
if "get-parameters" in args:
    names = [arg for arg in args if arg.startswith("/")]
    present = [{"Name": name, "Value": values[name.rsplit("/", 1)[-1]]} for name in names
               if name.rsplit("/", 1)[-1] in values]
    print(json.dumps({"Parameters": present, "InvalidParameters": [name for name in names
                      if name not in {item["Name"] for item in present}]}))
else:
    name = args[args.index("--name") + 1].rsplit("/", 1)[-1]
    if name == "capacity-profile" and values[name] == "outage":
        sys.exit("network error")
    if name not in values or (not values[name] and name != "insights-results-bucket"):
        sys.exit("ParameterNotFound")
    print(values[name])
''')
            aws.chmod(0o755)
            bundle = root / "bundle"
            bundle.mkdir()
            manifest = root / "deployment.json"
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", TEST_CAPACITY_PROFILE=profile,
                       ENVIRONMENT="test", GITHUB_OUTPUT=os.devnull, NAMESPACE="adp-gateway",
                       TEST_INSIGHTS_BUCKET=bucket,
                       PERSONA_MODEL_MAPPING_ENABLED="false", CAPACITY_BUNDLE=str(bundle),
                       RENDERED_DEPLOYMENT=str(manifest), RUNNER_TEMP=str(root),
                       GATEWAY_IMAGE="example.test/gateway@sha256:" + "a" * 64,
                       RELEASE_IMAGE="example.test/gateway@sha256:" + "a" * 64,
                       TEST_ACTIVE=str(admission[0]) if admission and admission[0] is not None else "",
                       TEST_QUEUE=str(admission[1]) if admission and admission[1] is not None else "",
                       TEST_WAIT=str(admission[2]) if admission and admission[2] is not None else "")
            if route == "workflow":
                import yaml
                workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-deploy.yml").read_text())
                run = next(step["run"] for job in workflow["jobs"].values()
                           for step in job.get("steps", []) if step.get("name") == "Migrate schema then roll out new image")
                block = run[run.index("SSM_SNAPSHOT=$(mktemp)"):run.index('python3 "$RUNNER_TEMP/adp-gateway-control/capacity-lock.py" verify', run.index("get_ssm() {"))]
                control = root / "adp-gateway-control"
                control.mkdir()
                (control / "batch-ssm.py").write_bytes((ROOT / "modules/gateway/scripts/batch-ssm.py").read_bytes())
                (control / "render-capacity.py").write_bytes((ROOT / "platform/scripts/render-gateway-capacity.py").read_bytes())
                cwd = ROOT
                prefix = 'set -euo pipefail\n'
            else:
                source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
                block = source[source.index("  # Render deployment settings as well as the ConfigMap."):
                               source.index('\n  if [ "$UPDATE_MODE" = true ]; then', source.index("  # Render deployment settings as well as the ConfigMap."))]
                cwd = ROOT / "modules/gateway"
                prefix = '''set -euo pipefail
UPDATE_MODE=false
SCRIPT_DIR=../../platform/scripts
capacity_lock() { :; }
_get_ssm() { aws ssm get-parameter --name "$1" --query Parameter.Value --output text 2>/dev/null || echo "${2:-}"; }
fail() { echo "$*" >&2; exit 1; }
gateway_deployment_replicas() { echo 2; }
migrate_gateway_before_rollout() { :; }
kubectl() { if [[ " $* " == *" -f - "* ]]; then cat >/dev/null; fi; }
python3() {
  if [[ "$1" == scripts/apply-gateway-deployment.py ]]; then
    cp "$3" "$CAPTURED_DEPLOYMENT"
    echo deployment.apps/bedrockgateway-replaced
  else
    command python3 "$@"
  fi
}
'''
                env["CAPTURED_DEPLOYMENT"] = str(root / "captured.json")
                env["BUNDLE_PATH"] = str(root / "bundle-path")
                block += '\nprintf "%s" "$CAPACITY_BUNDLE" > "$BUNDLE_PATH"\n'
            script = root / "render.sh"
            script.write_text(prefix + block)
            result = subprocess.run(["bash", str(script)], cwd=cwd, env=env,
                                    capture_output=True, text=True)
            if expect_success:
                self.assertEqual(result.returncode, 0, result.stderr)
                if route == "launcher":
                    bundle = Path((root / "bundle-path").read_text())
                rendered = json.loads((root / ("captured.json" if route == "launcher" else "deployment.json")).read_text())
                self.assertEqual(json.loads((bundle / "deployment.json").read_text()), rendered)
                container = next(item for item in rendered["spec"]["template"]["spec"]["containers"]
                                 if item["name"] == "bedrockgateway")
                self.assertEqual(container["image"], "example.test/gateway@sha256:" + "a" * 64)
                return rendered, json.loads((bundle / "hpa.json").read_text()), json.loads((bundle / "pdb.json").read_text())
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((bundle / "deployment.json").exists())
            self.assertFalse((root / "captured.json").exists())
            return None

    def test_both_paths_select_identical_capacity_and_retain_two_pod_floor(self):
        for profile, workers, per_pod in (("default", 1, 12), ("llm-proxy", 2, 20)):
            with self.subTest(profile=profile):
                outputs = [self.run_render(route, profile) for route in ("workflow", "launcher")]
                for deployment, hpa, pdb in outputs:
                    self.assertEqual(deployment["metadata"]["annotations"]["adp.aws-e.example/capacity-profile"], profile)
                    self.assertEqual(hpa["spec"]["minReplicas"], 2)
                    self.assertEqual(pdb["spec"]["maxUnavailable"], 1)
                    container = deployment["spec"]["template"]["spec"]["containers"][0]
                    self.assertEqual(container["command"], ["uvicorn"])
                    self.assertEqual(int(container["args"][container["args"].index("--workers") + 1]), workers)
                    env = {item["name"]: item.get("value") for item in container["env"]}
                    self.assertEqual(int(env["BG_RDS_POOL_SIZE"]) + int(env["BG_RDS_POOL_MAX_OVERFLOW"]), per_pod // workers)
                self.assertEqual(outputs[0][1], outputs[1][1])
                self.assertEqual(outputs[0][2], outputs[1][2])
                self.assertEqual(outputs[0], self.run_render("workflow", profile))
                self.assertEqual(outputs[1], self.run_render("launcher", profile))

    def test_both_profiles_retain_insights_and_runtime_identity_from_base(self):
        for route in ("workflow", "launcher"):
            for profile in ("default", "llm-proxy"):
                for bucket in ("", "example-insights-results-123"):
                    with self.subTest(route=route, profile=profile, bucket=bucket):
                        deployment, _, _ = self.run_render(route, profile, bucket=bucket)
                        container = deployment["spec"]["template"]["spec"]["containers"][0]
                        values = {entry["name"]: entry for entry in container["env"]}
                        self.assertEqual(values["BG_INSIGHTS_RESULTS_BUCKET"]["value"], bucket)
                        for name, field in (("ADP_POD_UID", "metadata.uid"),
                                            ("ADP_POD_NAMESPACE", "metadata.namespace")):
                            self.assertEqual(values[name]["valueFrom"], {"fieldRef": {"fieldPath": field}})

    def test_explicit_inference_limits_render_in_both_paths_without_a_default(self):
        for route in ("workflow", "launcher"):
            deployment, _, _ = self.run_render(route, "default", admission=(2, 1, 100))
            values = {item["name"]: item.get("value") for item in
                      deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual(values["BG_MAX_ACTIVE_STREAMS_PER_WORKER"], "2")
            self.assertEqual(values["BG_STREAM_ADMISSION_QUEUE_LIMIT"], "1")
            self.assertEqual(values["BG_STREAM_ADMISSION_WAIT_MS"], "100")
            self.run_render(route, "default", admission=(2, None, 100), expect_success=False)

    def test_invalid_selection_stops_both_paths_before_mutation(self):
        for route in ("workflow", "launcher"):
            self.run_render(route, "unknown", expect_success=False)
            self.run_render(route, "outage", expect_success=False)

    def test_entry_points_use_protected_apply_and_shared_bundle(self):
        launcher = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        workflow = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
        for source, renderer, apply in (
            (launcher, "render-gateway-capacity.py", "apply-gateway-deployment.py"),
            (workflow, "render-capacity.py", "apply-deployment.py"),
        ):
            self.assertIn(renderer, source)
            self.assertIn(apply, source)
            self.assertIn('$CAPACITY_BUNDLE/hpa.json', source)
            self.assertIn('$CAPACITY_BUNDLE/pdb.json', source)
        self.assertNotIn('kubectl set image deployment/bedrockgateway', launcher)
