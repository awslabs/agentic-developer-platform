"""run-probe-cycle.sh records model invocability evidence on demand (#7967).

The script is exercised against a stub kubectl so no cluster is needed: the stub
serves the CronJob JSON, records the applied Job manifest, reports the Job state and
returns the probe log the real runner prints (per-slot lines plus the summary JSON).
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "platform/scripts/run-probe-cycle.sh"

STUB = r"""#!/usr/bin/env bash
# Accepts the kubectl invocations run-probe-cycle.sh makes; -n <ns> may appear anywhere.
args=()
while [ $# -gt 0 ]; do case "$1" in -n) shift 2 ;; *) args+=("$1"); shift ;; esac; done
verb="${args[0]}"; set -- "${args[@]:1}"
log() { printf '%s\n' "$*" >> "$STUB_DIR/calls"; }
case "$verb" in
  get)
    if [ "$1" = cronjob ]; then log "get cronjob $2"; cat "$STUB_DIR/cronjob.json"; exit 0; fi
    if [ "$1" = job ]; then log "get job $2"; cat "$STUB_DIR/job-status.json"; exit 0; fi
    exit 1 ;;
  apply) cat > "$STUB_DIR/applied.json"; log "apply"; exit 0 ;;
  logs) log "logs $1"; cat "$STUB_DIR/job-log.txt"; exit 0 ;;
  delete) log "delete $*"; exit 0 ;;
esac
exit 1
"""


def cronjob(suspend, local_switch):
    return {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": "persona-model-invocability-probe", "namespace": "adp-agents"},
        "spec": {
            "schedule": "17 2 * * *", "suspend": suspend,
            "jobTemplate": {
                "metadata": {"labels": {"app.kubernetes.io/component": "persona-model-probe"}},
                "spec": {
                    "backoffLimit": 0, "activeDeadlineSeconds": 1800,
                    "template": {
                        "metadata": {"labels": {"app.kubernetes.io/name": "agent-scaledjob"}},
                        "spec": {
                            "serviceAccountName": "persona-model-probe-sa", "restartPolicy": "Never",
                            "containers": [{
                                "name": "persona-model-probe", "image": "registry/adp-agent-runtime@sha256:abc",
                                "command": ["node", "/app/dist/invocability-probe/index.js"],
                                "env": [{"name": "AWS_REGION", "value": "us-east-1"},
                                        {"name": "ADP_PERSONA_MODEL_PROBE_ENABLED", "value": local_switch}],
                            }],
                        },
                    },
                },
            },
        },
    }


def job_status(state):
    conditions = {"complete": [{"type": "Complete", "status": "True"}],
                  "failed": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}],
                  "pending": []}[state]
    return {"status": {"conditions": conditions}}


class RunProbeCycleTests(unittest.TestCase):
    def run_script(self, *, suspend=False, local_switch="true", state="complete", log="", timeout="20"):
        with tempfile.TemporaryDirectory() as temp:
            stub_dir = Path(temp)
            (stub_dir / "bin").mkdir()
            kubectl = stub_dir / "bin/kubectl"
            kubectl.write_text(STUB)
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
            (stub_dir / "cronjob.json").write_text(json.dumps(cronjob(suspend, local_switch)))
            (stub_dir / "job-status.json").write_text(json.dumps(job_status(state)))
            (stub_dir / "job-log.txt").write_text(log)
            env = dict(os.environ, PATH=f"{stub_dir / 'bin'}:{os.environ['PATH']}", STUB_DIR=str(stub_dir),
                       ENVIRONMENT="test", TIMEOUT_SECONDS=timeout, POLL_SECONDS="1")
            result = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, text=True, capture_output=True)
            applied = json.loads((stub_dir / "applied.json").read_text()) if (stub_dir / "applied.json").exists() else None
            calls = (stub_dir / "calls").read_text() if (stub_dir / "calls").exists() else ""
            return result, applied, calls

    PROVEN_LOG = ("[invocability-probe] harness started model=global.anthropic.claude-opus-5\n"
                  '{"completed":2,"outcomes":{"proven":2,"refused":0,"error":0},"stoppedReason":"no_candidates"}\n')

    def test_a_proven_cycle_exits_zero_and_prints_the_summary(self):
        result, applied, calls = self.run_script(log=self.PROVEN_LOG)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cycle summary completed=2 proven=2 refused=0 error=0 stopped=no_candidates", result.stdout)
        self.assertIn("[invocability-probe] harness started model=global.anthropic.claude-opus-5", result.stdout)
        self.assertEqual(applied["kind"], "Job")
        self.assertTrue(applied["metadata"]["name"].startswith("probe-cycle-"))
        self.assertEqual(applied["metadata"]["namespace"], "adp-agents")
        self.assertEqual(applied["metadata"]["annotations"]["cronjob.kubernetes.io/instantiate"], "manual")
        container = applied["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["node", "/app/dist/invocability-probe/index.js"])
        self.assertEqual(applied["spec"]["template"]["spec"]["serviceAccountName"], "persona-model-probe-sa")
        self.assertEqual(applied["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"], "agent-scaledjob")
        self.assertEqual([e for e in container["env"] if e["name"] == "ADP_PERSONA_MODEL_PROBE_ENABLED"],
                         [{"name": "ADP_PERSONA_MODEL_PROBE_ENABLED", "value": "true"}])
        self.assertNotIn("delete", calls, "the Job is kept for inspection by default")

    def test_the_local_switch_is_forced_on_only_when_the_cronjob_is_not_suspended(self):
        result, applied, _ = self.run_script(suspend=False, local_switch="false", log=self.PROVEN_LOG)
        self.assertEqual(result.returncode, 0, result.stderr)
        env = {e["name"]: e["value"] for e in applied["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["ADP_PERSONA_MODEL_PROBE_ENABLED"], "true")
        self.assertEqual(env["AWS_REGION"], "us-east-1")

    def test_a_suspended_cronjob_with_the_switch_off_is_refused_with_the_tfvars_fix(self):
        result, applied, _ = self.run_script(suspend=True, local_switch="false")
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIsNone(applied, "no Job is created")
        self.assertIn("persona_model_probe_enabled = true in environments/test/modules/webhook-ingress.tfvars", result.stderr)

    def test_a_failed_job_exits_one(self):
        result, _, _ = self.run_script(state="failed", log="[invocability-probe] fatal: ADP persona-model probe is locally disabled\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("[invocability-probe] fatal:", result.stdout)
        self.assertIn("probe job failed", result.stderr)

    def test_a_configuration_refusal_exits_one_with_the_gateway_tfvars_fix(self):
        for reason in ("disabled", "zero_slots", "zero_budget", "platform_account_unconfigured"):
            with self.subTest(reason=reason):
                log = json.dumps({"completed": 0, "outcomes": {"proven": 0, "refused": 0, "error": 0}, "stoppedReason": reason}) + "\n"
                result, _, _ = self.run_script(log=log)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"the gateway refused the cycle ({reason})", result.stderr)
                self.assertIn("environments/test/modules/gateway.tfvars", result.stderr)

    def test_nothing_to_claim_exits_three(self):
        for reason in ("no_candidates", "cycle_budget_exhausted"):
            with self.subTest(reason=reason):
                log = json.dumps({"completed": 0, "outcomes": {"proven": 0, "refused": 0, "error": 0}, "stoppedReason": reason}) + "\n"
                result, _, _ = self.run_script(log=log)
                self.assertEqual(result.returncode, 3, result.stdout)
                self.assertIn(f"nothing to claim ({reason})", result.stderr)

    def test_claims_without_a_proven_outcome_exit_one(self):
        log = json.dumps({"completed": 3, "outcomes": {"proven": 0, "refused": 2, "error": 1}, "stoppedReason": "no_candidates"}) + "\n"
        result, _, _ = self.run_script(log=log)
        self.assertEqual(result.returncode, 1)
        self.assertIn("3 slot(s) claimed but none proven", result.stderr)

    def test_a_job_that_never_finishes_exits_one_after_the_timeout(self):
        result, _, _ = self.run_script(state="pending", timeout="2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not finish within 2s", result.stderr)

    def test_a_completed_job_without_a_summary_exits_one(self):
        result, _, _ = self.run_script(log="no json here\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("without a cycle summary", result.stderr)


if __name__ == "__main__":
    unittest.main()
