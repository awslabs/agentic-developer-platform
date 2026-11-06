"""Run the gateway shell helpers against a controlled kubectl implementation."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]


class GatewayRolloutTests(unittest.TestCase):
    def run_healthcheck(self, body, status=200, exec_exit=0):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *_args):
                pass

        with tempfile.TemporaryDirectory() as directory, HTTPServer(('127.0.0.1', 0), Handler) as server:
            root = Path(directory)
            # Model the runtime without curl, forwarding the actual probe command
            # to this interpreter and changing only its target to the local fixture.
            mock = root / 'kubectl'
            mock.write_text('''#!/usr/bin/python3 -I
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
Path(os.environ["CALLS"]).write_text(json.dumps(args))
if int(os.environ["EXEC_EXIT"]):
    sys.exit(int(os.environ["EXEC_EXIT"]))
command = args[args.index("--") + 1:]
if command[0] != "python":
    sys.exit(127)
command[0] = os.environ["TEST_PYTHON"]
command[-1] = os.environ["TEST_HEALTH_URL"]
os.execv(command[0], command)
''')
            mock.chmod(0o755)
            (root / 'sitecustomize.py').write_text(
                'from pathlib import Path\nPath(__file__).with_name("site-loaded").touch()\n')
            env = dict(os.environ, PATH=f'{root}:{os.environ["PATH"]}', PYTHONPATH=str(root),
                       CALLS=str(root / 'calls'), TEST_PYTHON=sys.executable, EXEC_EXIT=str(exec_exit),
                       TEST_HEALTH_URL=f'http://127.0.0.1:{server.server_port}/health')
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            try:
                result = subprocess.run(
                    ['bash', '-c', 'source "$1"; gateway_healthcheck', 'test', str(SCRIPTS / 'gateway-rollout.sh')],
                    env=env, text=True, capture_output=True, timeout=15)
            finally:
                server.shutdown()
                thread.join()
            self.assertFalse((root / 'site-loaded').exists(), 'Probe must not start inherited telemetry site hooks')
            return result, json.loads((root / 'calls').read_text())

    def test_healthcheck_uses_runtime_python_and_accepts_healthy_json(self):
        result, args = self.run_healthcheck('{"status":"healthy"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status': 'healthy'})
        self.assertEqual(args[:8], ['exec', '-n', 'adp-gateway', 'deploy/bedrockgateway', '-c',
                                   'bedrockgateway', '--request-timeout=30s', '--'])
        self.assertEqual(args[-1], 'http://localhost:8080/health')

    def test_healthcheck_rejects_unhealthy_non_json_http_error_and_exec_failure(self):
        for body, status, exec_exit in (
                ('{"status":"unhealthy"}', 200, 0), ('{}', 200, 0), ('[]', 200, 0),
                ('<html>frontend fallback</html>', 200, 0),
                ('{"status":"healthy"}', 503, 0), ('{"status":"healthy"}', 200, 7)):
            with self.subTest(body=body, status=status, exec_exit=exec_exit):
                result, _ = self.run_healthcheck(body, status, exec_exit)
                self.assertNotEqual(result.returncode, 0)

    def test_post_rollout_probe_warns_only_when_check_fails(self):
        source = (SCRIPTS / 'deploy-all.sh').read_text()
        start = source.index('    # Post-rollout health check')
        block = source[start:source.index('\n  else', start)]
        stub = 'sleep() { :; }; warn() { echo "$*"; }; gateway_healthcheck() { return "$PROBE_EXIT"; };\n'
        for exit_code in (0, 1):
            result = subprocess.run(
                ['bash', '-c', 'set -euo pipefail; ' + stub + block],
                env=dict(os.environ, PROBE_EXIT=str(exit_code), SCRIPT_DIR=str(SCRIPTS)),
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual('Health check inconclusive' in result.stdout, bool(exit_code))
            if exit_code:
                self.assertIn('gateway_healthcheck', result.stdout)

    def run_helper(self, command, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / "kubectl"
            mock.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
if [[ "$1 $2" == "rollout status" ]]; then exit "${ROLLOUT_EXIT:-0}"; fi
if [[ "$*" == *spec.replicas* ]]; then
  printf '%s' "${REPLICAS-4}"
  exit "${READ_EXIT:-0}"
fi
exit "${DIAGNOSTIC_EXIT:-0}"
''')
            mock.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", CALLS=str(root / "calls"))
            env.pop("ADP_GATEWAY_ROLLOUT_TIMEOUT_SECONDS", None)
            env.update(overrides)
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail; source "$1"; ' + command,
                 "test", str(SCRIPTS / "gateway-rollout.sh")],
                env=env, text=True, capture_output=True,
            )
            calls = (root / "calls").read_text() if (root / "calls").exists() else ""
            return result, calls

    def test_upgrade_preserves_scaled_and_zero_replicas(self):
        for count in ("4", "12", "0"):
            result, calls = self.run_helper("gateway_deployment_replicas true", REPLICAS=count)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), count)
            self.assertIn("spec.replicas", calls)

    def test_install_keeps_baseline_without_read(self):
        result, calls = self.run_helper("gateway_deployment_replicas false")
        self.assertEqual(result.stdout.strip(), "2")
        self.assertEqual(calls, "")

    def test_missing_or_unreadable_replicas_fail_closed(self):
        for overrides in ({"READ_EXIT": "1"}, {"REPLICAS": ""}, {"REPLICAS": "null"}):
            result, _ = self.run_helper("gateway_deployment_replicas true", **overrides)
            self.assertNotEqual(result.returncode, 0)

    def test_success_uses_default_budget_without_diagnostics(self):
        result, calls = self.run_helper("wait_for_gateway_rollout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--timeout=2400s", calls)
        self.assertEqual(len(calls.splitlines()), 1)

    def test_override_and_failure_diagnostics_preserve_original_exit(self):
        result, calls = self.run_helper(
            "wait_for_gateway_rollout", ADP_GATEWAY_ROLLOUT_TIMEOUT_SECONDS="1800",
            ROLLOUT_EXIT="7", DIAGNOSTIC_EXIT="1",
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("--timeout=1800s", calls)
        self.assertIn("get deployment,replicaset,pods", calls)
        self.assertIn("status.conditions", calls)
        self.assertIn("get events", calls)
        self.assertNotIn("get secrets", calls)

    def test_invalid_timeout_does_not_call_kubectl(self):
        for value in ("0", "-1", "forever", "1s", "9999999"):
            result, calls = self.run_helper(
                "wait_for_gateway_rollout", ADP_GATEWAY_ROLLOUT_TIMEOUT_SECONDS=value,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls, "")

    def test_rendered_manifest_uses_preserved_count(self):
        script = (SCRIPTS / "deploy-all.sh").read_text()
        start = script.index('  GATEWAY_DEPLOYMENT_REPLICAS=$(gateway_deployment_replicas')
        end = script.index('  echo "$DEPLOYMENT_APPLY_RESULT"', start)
        self.assertIn('gateway_deployment_replicas "$UPDATE_MODE"', script[start:end])
        self.assertIn('replicas: ${GATEWAY_DEPLOYMENT_REPLICAS}', script[start:end])
        self.assertNotIn('kubectl rollout status deployment/bedrockgateway', script)
