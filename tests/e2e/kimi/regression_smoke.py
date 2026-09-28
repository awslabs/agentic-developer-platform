"""Run only on disposable regression EC2, after dedicated ADP CLI login.

Uses the installed ADP proxy without replacing its auth/origin/capability guards.
Adds request/output limits for this smoke and records no request bodies or tokens.
"""

import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path.home()
WORK = ROOT / "kimi-regression"
CLI = ROOT / ".adp/bin"
MODEL = "global.moonshotai.kimi-k3"


def proxy():
    sys.path.insert(0, str(CLI))
    spec = importlib.util.spec_from_file_location(
        "adp_smoke_proxy", CLI / "bg-gateway-proxy.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    original = module.GatewayProxyHandler._read_request_body
    counts = {
        "requests": 0,
        "request_bytes": 0,
        "models": [],
        "tool_result_requests": 0,
    }

    def bounded_body(handler):
        raw = original(handler)
        if handler.command != "POST" or handler.path != "/openai/v1/responses":
            raise ValueError("smoke only permits Responses requests")
        body = json.loads(raw)
        # Kimi 2.1.1 ignores max_output_size for Responses. This test-only
        # boundary supplies the explicit cap before sending any paid request.
        body.setdefault("max_output_tokens", 1024)
        raw = json.dumps(body).encode()
        cap = body.get("max_output_tokens")
        if body.get("model") != MODEL or type(cap) is not int or not 0 < cap <= 1024:
            raise ValueError("smoke model or output bound rejected")
        if body.get("service_tier", "default") not in ("default", "standard"):
            raise ValueError("smoke requires standard pricing")
        if counts["requests"] >= 12 or counts["request_bytes"] + len(raw) > 500000:
            raise ValueError("smoke aggregate request limit reached")
        counts["requests"] += 1
        counts["request_bytes"] += len(raw)
        counts["models"].append(body["model"])
        if any(
            x.get("type") == "function_call_output"
            for x in body.get("input", [])
            if isinstance(x, dict)
        ):
            counts["tool_result_requests"] += 1
        (WORK / "requests.json").write_text(json.dumps(counts))
        return raw

    module.GatewayProxyHandler._read_request_body = bounded_body
    module.main(
        [
            "--gateway-url",
            "https://d1g6cal2ts4iis.cloudfront.net/api",
            "--auth-helper",
            str(CLI / "bg-cognito-auth.sh"),
            "--port",
            "9191",
            "--identity-file",
            str(WORK / "proxy-identity.json"),
        ]
    )


def main():
    WORK.mkdir(exist_ok=True, mode=0o700)
    if "--proxy" in sys.argv:
        return proxy()
    home = WORK / "kimi-home"
    home.mkdir(exist_ok=True, mode=0o700)
    (home / "config.toml").write_text("""default_model = "adp-kimi-k3"
[providers.adp]
type = "openai_responses"
base_url = "http://127.0.0.1:9191/openai/v1"
api_key_env = "ADP_KIMI_LOCAL_CAPABILITY"
[models.adp-kimi-k3]
provider = "adp"
model = "global.moonshotai.kimi-k3"
max_context_size = 1000000
max_output_size = 1024
capabilities = ["thinking", "tool_use"]
""")
    fixture = WORK / "fixture"
    fixture.mkdir(exist_ok=True)
    marker = "KIMI_FILE_" + secrets.token_hex(8)
    (fixture / "sample.txt").write_text(marker + "\n")
    cap = secrets.token_urlsafe(32)
    env = dict(
        os.environ,
        ADP_GATEWAY_DUMMY=cap,
        ADP_KIMI_LOCAL_CAPABILITY=cap,
        KIMI_CODE_HOME=str(home),
    )
    with (WORK / "proxy.log").open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, __file__, "--proxy"], env=env, stdout=log, stderr=log
        )
        try:
            for _ in range(50):
                if (WORK / "proxy-identity.json").exists():
                    break
                if proc.poll() is not None:
                    raise RuntimeError("proxy failed")
                time.sleep(0.2)
            result = subprocess.run(
                [
                    str(ROOT / ".kimi-code/bin/kimi"),
                    "--prompt",
                    "Read sample.txt using the file read tool. Reply with its exact contents and nothing else. Do not edit files, run shell commands, use the network or delegate.",
                    "--output-format",
                    "stream-json",
                ],
                cwd=fixture,
                env=env,
                capture_output=True,
                text=True,
                timeout=240,
            )
            (WORK / "cli-output.jsonl").write_text(result.stdout)
            (WORK / "cli-stderr.log").write_text(result.stderr)
            counts = (
                json.loads((WORK / "requests.json").read_text())
                if (WORK / "requests.json").exists()
                else {}
            )
            report = {
                "exit_code": result.returncode,
                "marker_found": marker in result.stdout,
                **counts,
            }
            print(json.dumps(report))
            (WORK / "result.json").write_text(json.dumps(report))
            if (
                result.returncode
                or not report["marker_found"]
                or not counts.get("tool_result_requests")
            ):
                raise SystemExit(1)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            (WORK / "proxy-identity.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
