"""One installed stdio Interpreter journey using an already issued operator session.

This client never approves, enrolls, refreshes, provisions or invokes a model.
The gateway remains the authority for the original approval and funding.
"""

import base64
import argparse
import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from modules.tools.platform.scripts.native_test_session import NoRedirects, canonical, private_file, request_json
from tests.e2e.tools.installed_clients import source_file
from tests.e2e.tools.runner import MCPClient

CODE = 'from pathlib import Path\nimport json\np = Path("/tmp/adp-fixture.txt")\np.write_bytes(b"ADP\\n")\nprint(json.dumps({"ok": True, "bytes": 4}))\n'
FILE_PATH = "/tmp/adp-fixture.txt"
FILE_BYTES = b"ADP\n"


@dataclass(frozen=True)
class OperatorLocalSession:
    transport_file: Path
    reference: dict
    binding: dict
    started_at: int
    token: str = field(repr=False)
    capability: str = field(repr=False)

    @classmethod
    def read(cls, path):
        path = Path(path)
        value = json.loads(private_file(path, 65536))
        reference, binding = value["reference"], value["binding"]
        if (value["version"] != 2 or reference.get("producer") != "operator_local"
                or set(reference) != {"producer", "run_id", "tenant_id", "subject_id", "approval_revision", "deployment_id",
                                      "gateway_origin", "source_revision", "contract_sha256"}
                or reference["contract_sha256"] != hashlib.sha256(canonical(binding)).hexdigest()
                or binding.get("execute_code") != CODE or binding.get("file_path") != FILE_PATH
                or value["models"] != [] or value["max_discoveries"] != 1
                or type(value["started_at"]) is not int):
            raise ValueError("Existing exact operator Interpreter session required")
        return cls(path, reference, binding, value["started_at"], value["access_token"], value["capability"])

    def status(self):
        if not self.started_at <= time.time() < self.started_at + 600:
            raise ValueError("Original operator status horizon expired")
        return request_json(build_opener(ProxyHandler({}), NoRedirects()),
            self.reference["gateway_origin"] + "/api/v1/operator-tools/runs/" + self.reference["run_id"] + "/status",
            {"Authorization": "Bearer " + self.token, "x-adp-native-test-capability": self.capability})


def _receipt(response):
    blocks = response.get("content")
    if not isinstance(blocks, list) or len(blocks) != 1 or blocks[0].get("type") != "text":
        raise ValueError("Original stdio response unavailable")
    result = json.loads(blocks[0]["text"])
    if not isinstance(result, dict):
        raise ValueError("Original stdio receipt unavailable")
    return result


def _provider(receipt):
    value = receipt.get("result")
    for _ in range(4):
        if not isinstance(value, dict) or "session_id" in value:
            break
        value = value.get("result")
    if not isinstance(value, dict):
        raise ValueError("Original Interpreter result unavailable")
    return value


def run_operator_interpreter(installed, environ, session):
    """Use one verified [installed, 'mcp'] process and retain original journals."""
    if type(session) is not OperatorLocalSession:
        raise ValueError("Existing operator session required")
    current = OperatorLocalSession.read(session.transport_file)
    if current != session:
        raise ValueError("Original operator transport changed")
    installed, home = Path(installed), Path(environ["HOME"])
    if (installed != home / ".adp/bin/adp" or not home.is_absolute() or home.is_symlink()
            or not stat.S_ISDIR(home.lstat().st_mode) or home.lstat().st_uid != os.getuid() or home.lstat().st_mode & 0o077
            or installed.resolve() != installed
            or session.transport_file.resolve().is_relative_to(home.resolve())):
        raise ValueError("Verified isolated installation required")
    sources = {}
    for name in ("adp", "adp-mcp.py", "adp_common.py", "adp_native_test.py", "adp_native_credential_handoff.py", "adp_deployments.py"):
        path = installed.parent / name
        if path.is_symlink() or path.read_bytes() != source_file(session.reference["source_revision"], name):
            raise ValueError("Installed CLI source differs from original approval")
        sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    env = {key: value for key, value in environ.items()
           if not key.startswith(("ANTHROPIC_", "OPENAI_", "AWS_", "CLAUDE_", "CODEX_", "ADP_", "SIGV4_"))
           and key not in {"BG_PROXY_PORT", "BG_AUTH_PROXY_PORT", "BG_ACCESS_TOKEN", "PYTHONPATH", "PYTHONSTARTUP"}}
    env["ADP_NATIVE_TEST_TRANSPORT_FILE"] = str(session.transport_file)
    config = home / ".bedrock-gateway"
    config.mkdir(mode=0o700, exist_ok=True)
    if config.is_symlink() or not config.is_dir() or config.stat().st_uid != os.getuid() or config.stat().st_mode & 0o077:
        raise ValueError("Private isolated operation journal required")
    env["BG_CONFIG_DIR"] = str(config)
    start, cleanup, behavior = None, None, False
    with MCPClient([str(installed), "mcp"], environ=env) as client:
        def invoke(name, arguments):
            return _receipt(client.request("tools/call", {"name": name, "arguments": arguments}))

        try:
            listing = client.request("tools/list")
            if not {"code_interpreter___" + name for name in ("start", "execute", "result", "file")} <= {
                    row["name"] for row in listing["tools"]}:
                raise ValueError("Original Interpreter discovery unavailable")
            start = invoke("code_interpreter___start", {"ttl_seconds": 300})
            if start.get("status") != "confirmed" or start.get("result_status") != "success":
                raise ValueError("Original Interpreter start unconfirmed")
            session_id = _provider(start)["session_id"]
            owned = {"execution_ref": start["operation_ref"], "session_id": session_id}
            execute = invoke("code_interpreter___execute", {**owned, "code": CODE, "language": "python"})
            if execute.get("status") not in {"unknown", "confirmed"}:
                raise ValueError("Original Interpreter execute unavailable")
            status = invoke("adp___operation_status", {"operation_ref": execute["operation_ref"]})
            if status.get("status") != "confirmed" or status.get("result_status") != "success":
                raise ValueError("Original Interpreter execute remains unresolved")
            task = _provider(status)["provider_id"]
            result = invoke("code_interpreter___result", {**owned, "provider_id": task})
            if result.get("status") != "confirmed" or result.get("result_status") != "success":
                raise ValueError("Original Interpreter result unconfirmed")
            output = _provider(result)["result"]
            if output.get("exitCode") != 0 or json.loads(output.get("stdout", "")) != {"ok": True, "bytes": 4}:
                raise ValueError("Retained inert code outcome differs")
            file = invoke("code_interpreter___file", {**owned, "path": FILE_PATH})
            if file.get("status") != "confirmed" or file.get("result_status") != "success":
                raise ValueError("Original Interpreter file unconfirmed")
            content = _provider(file)["result"]
            if (content.get("path") != FILE_PATH or base64.b64decode(content["data_base64"], validate=True) != FILE_BYTES
                    or content.get("byte_length") != len(FILE_BYTES)
                    or content.get("content_sha256") != hashlib.sha256(FILE_BYTES).hexdigest()):
                raise ValueError("Original Interpreter file bytes differ")
            behavior = True
        finally:
            if start is not None and start.get("execution_id") and start.get("operation_ref"):
                for _ in range(2):
                    try:
                        cleanup = invoke("adp___execution_close", {"operation_ref": start["operation_ref"]})
                    except (OSError, ValueError, TimeoutError):
                        cleanup = {"cleanup_status": "unknown"}
                    if cleanup.get("state") == "closed" and cleanup.get("cleanup_status") == "confirmed":
                        break
    if not behavior or not cleanup or cleanup.get("cleanup_status") != "confirmed":
        raise ValueError("Original Interpreter cleanup remains unconfirmed; retain journals")
    governance = session.status()
    if (governance.get("run_id") != session.reference["run_id"] or governance.get("funding_status") not in {"held", "settled"}
            or governance.get("accounting_status") not in {"pending", "settled"}):
        raise ValueError("Original operator funding evidence unavailable; retain journals")
    return {"schema_version": "1.0", "producer": "operator_local", "run_id": session.reference["run_id"],
            "approval_revision": session.reference["approval_revision"], "behavioral_success": True,
            "cleanup_confirmed": True, "model_requests": 0, "file_sha256": hashlib.sha256(FILE_BYTES).hexdigest(),
            "funding_status": governance["funding_status"], "accounting_status": governance["accounting_status"],
            "governance_complete": governance.get("governance_complete") is True, "installed_sources": sources}


def main():
    parser = argparse.ArgumentParser(description="Use one already approved operator Interpreter session")
    parser.add_argument("--installed", required=True)
    parser.add_argument("--transport-file", required=True)
    parser.add_argument("--evidence-file", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    session = OperatorLocalSession.read(args.transport_file)
    report = run_operator_interpreter(args.installed, os.environ, session)
    data = canonical(report) + b"\n"
    if len(data) > 8192:
        raise ValueError("Original operator evidence exceeds bound")
    descriptor = os.open(args.evidence_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as evidence:
        evidence.write(data)
        evidence.flush()
        os.fsync(evidence.fileno())
    print("Interpreter behavior and original cleanup confirmed; accounting status: " + report["accounting_status"])


if __name__ == "__main__":
    main()
