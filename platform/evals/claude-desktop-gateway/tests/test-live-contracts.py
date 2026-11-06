import ast
import base64
import fnmatch
import json
import logging
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
import unittest


EVAL_DIR = Path(__file__).resolve().parents[1]
GATEWAY_SRC = EVAL_DIR.parents[2] / "modules/gateway/src"


def model_resolver():
    source = GATEWAY_SRC / "proxy/model_resolver.py"
    tree = ast.parse(source.read_text())
    tree.body = [
        node for node in tree.body
        if (isinstance(node, ast.AnnAssign) and node.target.id in (
            "DEFAULT_MODEL_ALIASES", "DEFAULT_ALLOWED_PATTERNS"
        )) or (isinstance(node, ast.ClassDef) and node.name == "ModelResolver")
    ]
    namespace = {
        "fnmatch": fnmatch, "re": re, "Any": Any, "TokenContext": SimpleNamespace,
        "logger": logging.getLogger(__name__),
        "ModelNotAllowedError": runpy.run_path(str(GATEWAY_SRC / "shared/exceptions.py"))["ModelNotAllowedError"],
    }
    exec(compile(tree, str(source), "exec"), namespace)
    return namespace["ModelResolver"]()


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.fixture"


class LiveContractTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.workdir = Path(self.scratch.name)
        (self.workdir / "pod").mkdir()

    def run_phase(self, commands, **extra_env):
        environment = {
            "PATH": os.environ["PATH"], "HOME": str(self.workdir),
            "EVAL_WORKDIR": str(self.workdir), "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null", "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_MAX_ATTEMPTS": "1", **extra_env,
        }
        return subprocess.run(
            ["bash", "-c", '''
set -e
script="$1"; shift
source "$script"
POD_WORKDIR="$WORKDIR/pod"
GATEWAY_URL=https://gateway.example.test/api
h_aws() { return 90; }
h_kubectl() { return 90; }
laptop() { return 90; }
gateway_http() { return 90; }
laptop_put_file() { cp "$1" "$2"; }
laptop_get_file() { cp "$1" "$2"; }
''' + commands, "contract-test", str(EVAL_DIR / "run-eval.sh")],
            env=environment, capture_output=True, text=True, timeout=30,
        )

    def test_direct_count_serializes_raw_json_with_aws_cli(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.headers, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                response = b'{"inputTokens":42}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = self.run_phase('''
MODEL=global.anthropic.claude-sonnet-4-6
h_aws() { aws --region "$AWS_REGION" --no-sign-request --endpoint-url "$EVAL_TEST_ENDPOINT" "$@"; }
gateway_http() {
  printf '%s' '{"input_tokens":42}' > "$4"
  printf 'x-adp-token-count-mode: exact\r\n' > "$5"
  printf 200
}
count_tokens
''', EVAL_TEST_ENDPOINT=f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            worker.join()
            server.server_close()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gateway == direct: true", result.stdout)
        self.assertEqual(len(requests), 1)
        path, headers, payload = requests[0]
        self.assertEqual(path, "/model/anthropic.claude-sonnet-4-6/count-tokens")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(json.loads(base64.b64decode(payload["input"]["invokeModel"]["body"])), {
            "anthropic_version": "bedrock-2023-05-31", "max_tokens": 256,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Count this sample."}]}],
        })

    def run_models(self, error="model_not_allowed", response_model=None):
        return self.run_phase('''
gateway_http() {
  if [ "$1" = GET ]; then
    printf '%s' '{"data":[{"id":"global.anthropic.claude-sonnet-4-6"}]}' > "$4"
    printf 200
  else
    jq --arg error "$TEST_ERROR" --arg response_model "$TEST_RESPONSE_MODEL" \
      '{detail:{error:$error,model:(if $response_model == "" then .model else $response_model end),allowed_models:["anthropic.claude-*"]}}' \
      "$POD_WORKDIR/$3" > "$4"
    printf 403
  fi
}
models
''', TEST_ERROR=error, TEST_RESPONSE_MODEL=response_model or "")

    def test_control_is_denied_by_default_gateway_policy(self):
        result = self.run_models()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        control = json.loads((self.workdir / "denied-request.json").read_text())["model"]
        resolver = model_resolver()
        context = SimpleNamespace(org_id="fixture-selected", team_id="fixture-team")
        self.assertTrue(resolver.is_model_allowed("anthropic.claude-3-haiku-20240307-v1:0", context))
        with self.assertRaises(Exception) as denied:
            resolver.check_model_access(resolver.resolve_model(control), context)
        self.assertEqual(denied.exception.error, "model_not_allowed")
        self.assertEqual(denied.exception.status_code, 403)

    def test_provider_403_is_not_model_admission_evidence(self):
        result = self.run_models(error="access_denied")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("gateway: control response does not prove model admission denial", result.stderr)

    def test_denial_must_name_the_requested_control(self):
        result = self.run_models(response_model="another-model")
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def run_identity(self, token):
        (self.workdir / "pod/token").write_text(token)
        (self.workdir / "refresh-token").write_text("fixture")
        return self.run_phase('''
EVAL_REFRESH_TOKEN_FILE="$WORKDIR/refresh-token"
laptop_provision() { :; }
laptop_write_curl_auth_config() { :; }
laptop() { if [ "$1" = node ]; then command "$@"; fi; }
setup_cli
printf 'IDENTITY %s %s\n' "$SMOKE_USER" "$SMOKE_ORG"
''')

    def test_scoped_tenant_is_authoritative(self):
        for claims in ({"sub": "fixture-user"}, {"sub": "fixture-user", "custom:org_id": "fixture-home"}):
            with self.subTest(claims=claims):
                result = self.run_identity(f'adpctx1~{jwt({"tenant": "fixture-selected"})}~{jwt(claims)}')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("IDENTITY fixture-user fixture-selected", result.stdout)

    def test_scoped_tenant_must_be_valid_without_legacy_fallback(self):
        for tenant in (None, "", "unsafe/tenant", 123, ["fixture-selected"]):
            with self.subTest(tenant=tenant):
                result = self.run_identity(
                    f'adpctx1~{jwt({"tenant": tenant})}~{jwt({"sub": "fixture-user", "org_id": "fixture-home"})}'
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("IDENTITY", result.stdout)

    def test_unwrapped_tokens_retain_legacy_org_claims(self):
        for claim in ("custom:org_id", "org_id"):
            with self.subTest(claim=claim):
                result = self.run_identity(jwt({"sub": "fixture-user", claim: "fixture-home"}))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("IDENTITY fixture-user fixture-home", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
