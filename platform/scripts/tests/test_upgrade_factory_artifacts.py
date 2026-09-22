"""Execute the factory upgrade stage with service doubles to catch omitted publishes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


class FactoryArtifactsTests(unittest.TestCase):
    def run_stage(self, package_exit=0, chat_exit=0):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index('if [ "$DEPLOY_FACTORY" = true ]; then\n  step "Step 10/12:')
        block = source[start:source.index('\nrefresh_credentials', start)]
        prefix = '''set -euo pipefail
step() { :; }; ok() { :; }; warn() { :; }
fail() { echo "$*" >&2; exit 1; }
bash() {
  echo "script $(basename "$1") image=${AGENT_IMAGE:-}" >> "$CALLS"
  if [[ "$1" == */build-agent-factory-lambdas.sh ]]; then return "$PACKAGE_EXIT"; fi
}
terraform_update_apply() { echo "plan $1" >> "$CALLS"; }
terraform() { if [ "$1" = output ]; then echo value; fi; }
kubectl() {
  if [ "$1" = apply ]; then cat >/dev/null; fi
  if [ "$1" = get ]; then echo "$AGENT_IMAGE"; fi
}
run_codebuild() {
  echo "build $1 tag=$IMAGE_TAG" >> "$CALLS"
  if [[ "$1" == *-chat-agent ]]; then return "$CHAT_EXIT"; fi
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            env = dict(os.environ, ROOT_DIR=str(ROOT), SCRIPT_DIR=str(ROOT / "platform/scripts"),
                       ENVIRONMENT="dev", AWS_REGION="us-east-1", DEPLOY_FACTORY="true", DEPLOY_GATEWAY="true", UPDATE_MODE="true",
                       LOCAL_MODE="false", UPGRADE_RUN_DIR=tmp, STATE_BUCKET="state", REGISTRY="registry.test",
                       IMAGE_TAG="release-sha", CALLS=str(calls), PACKAGE_EXIT=str(package_exit), CHAT_EXIT=str(chat_exit))
            result = subprocess.run(["bash", "-c", prefix + block + '\nprintf "tag=%s\\n" "$IMAGE_TAG"'],
                                    env=env, text=True, capture_output=True)
            return result, calls.read_text().splitlines()

    def test_all_artifacts_are_published_with_distinct_worker_images(self):
        result, calls = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            "script build-agent-factory-lambdas.sh image=",
            "plan agent-factory",
            "build adp-dev-agent-gateway tag=release-sha",
            "build adp-dev-chat-agent tag=release-sha-chat",
            "script deploy-chat-scaledjob.sh image=registry.test/adp-chat-agent:release-sha-chat",
        ])
        self.assertIn("tag=release-sha", result.stdout)

    def test_failed_lambda_package_stops_before_infrastructure(self):
        result, calls = self.run_stage(package_exit=43)
        self.assertEqual(result.returncode, 43)
        self.assertEqual(len(calls), 1)

    def test_failed_chat_build_stops_before_chat_deployment(self):
        result, calls = self.run_stage(chat_exit=44)
        self.assertEqual(result.returncode, 44)
        self.assertFalse(any("deploy-chat-scaledjob.sh" in line for line in calls))


if __name__ == "__main__":
    unittest.main()
