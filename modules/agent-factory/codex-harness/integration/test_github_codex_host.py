"""Packaged GitHub adapter with the real pinned SDK and local host fixtures."""
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("persona,mode", [(p, "success") for p in ["architect", "product", "pm", "intent-refinement"]]
                         + [("architect", m) for m in ["steer", "unknown", "tampered", "repository-mismatch", "repository-read"]])
def test_packaged_github_sdk(tmp_path, persona, mode):
    node = shutil.which("node")
    assert node
    snapshot = json.loads(subprocess.check_output([node, str(PACKAGE / "scripts/catalogue.mjs"),
                                                  str(PACKAGE / "personas" / f"{persona}.json")], text=True))["snapshots"][0]
    package = tmp_path / "app/codex-harness"
    shutil.copytree(PACKAGE / "dist", package / "dist")
    shutil.copytree(PACKAGE.parent / "rules", package / "rules")
    shutil.copyfile(PACKAGE / "package.json", package / "package.json")
    (package / "node_modules").symlink_to(PACKAGE / "node_modules", target_is_directory=True)
    shared = tmp_path / "app/dist"
    shared.mkdir()
    (shared / "package.json").write_text('{"type":"module"}')
    token = tmp_path / "app/codex-reviewer/dist"
    token.mkdir(parents=True)
    (token / "package.json").write_text('{"type":"module"}')
    (token / "token-lifecycle.js").write_text('export async function withGitHubTokenRenewal(work) { return work(); }')
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    context = {"version": 1, "persona": f"agent-codex-{persona}", "repository": "owner/repo", "repositoryId": "123",
               "issue": 12, "snapshot": snapshot, "capabilities": ["artifacts.publish"], "maxTurns": 20, "maxTools": 0,
               "maxOutputTokens": 4096, "harnessRevision": "codex-sdk-0.155.1/adp-v1"}
    if mode == "repository-read":
        context["capabilities"].append("repository.read")
        context["maxTools"] = 32
    if mode == "tampered":
        context["snapshot"]["instructions"] = "unbound replacement"
    (shared / "codex-persona-policy.js").write_text('''
import { appendFileSync } from 'node:fs';
const context = JSON.parse(process.env.FIXTURE_CONTEXT);
context.deadlineMs = Date.now() + 45000;
let pending;
export async function admitCodexPersonaModel() {
  return { runId: 'fixture-run', model: 'gpt-5-codex', snapshotDigest: 'a'.repeat(64), generation: 1, evidenceId: 'fixture', context };
}
export async function codexPersonaOperation(body) {
  appendFileSync(process.env.FIXTURE_ARTIFACTS + '/operations.jsonl', JSON.stringify(body) + '\\n');
  if (body.action === 'claim') {
    if (pending) throw Error('unknown effect');
    pending = body.operation_id;
    return { status: 'admitted' };
  }
  if (pending !== body.operation_id) throw Error('operation mismatch');
  pending = undefined;
  return { status: 'confirmed', result: body.result };
}
''')
    (shared / "codex-persona-controls.js").write_text('''
import { appendFileSync } from 'node:fs';
let steered = false;
export async function startCodexPersonaControls(log, signal) {
 return { signal, explain(text) { appendFileSync(process.env.FIXTURE_ARTIFACTS + '/progress.txt', text + '\\n'); }, async checkpoint() { signal.throwIfAborted(); },
  takeSteering() { if (process.env.FIXTURE_MODE === 'steer' && !steered) { steered = true; return ['Also inspect retry configuration.']; } return []; },
  pendingSteering() { return false; }, async operation(work) { signal.throwIfAborted(); return work(); }, async close() {} };
}
''')
    (shared / "codex-persona-reporting.js").write_text('''
import { writeFileSync } from 'node:fs';
export async function createCodexPersonaReporter() {
 return { log() {}, progress() {}, async finish(result, provenance) {
  writeFileSync(process.env.FIXTURE_ARTIFACTS + '/report.json', JSON.stringify({ result, provenance }));
 }, async fail() { writeFileSync(process.env.FIXTURE_ARTIFACTS + '/failed', 'true'); } };
}
''')
    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({"id": ' + ('999' if mode == 'repository-mismatch' else '123')
                  + '} if sys.argv[1] == "api" else {"number":12,"title":"Fixture architecture request","body":"Inspect supplied evidence", "comments":[]}))\n')
    gh.chmod(0o700)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    (workspace / 'README.md').write_text('Pinned repository evidence 471.\n')
    subprocess.run(['git', 'add', 'README.md'], cwd=workspace, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '--allow-empty', '-qm', 'fixture'], cwd=workspace, check=True)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            if mode == 'unknown':
                self.send_response(503)
                self.end_headers()
                return
            response = {"id": "resp_fixture", "status": "completed", "output": [
                {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed", "phase": "final_answer",
                 "content": [{"type": "output_text", "text": "Fixture evidence report.", "annotations": []}]}],
                        "usage": {"input_tokens": 100, "output_tokens": 10}}
            if mode == 'repository-read' and len(requests) == 1:
                response["output"] = [{"id": "fc_fixture", "type": "function_call", "call_id": "call_fixture", "name": "repository_file",
                                       "namespace": "mcp__adp", "arguments": json.dumps({"path": "README.md"}), "status": "completed"}]
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'], 'AGENT_TYPE': f'agent-codex-{persona}',
           'TARGET_REPO': 'owner/repo', 'ISSUE_NUMBER': '12', 'WORK_DIR': str(workspace), 'FIXTURE_MODE': mode,
           'FIXTURE_CONTEXT': json.dumps(context), 'FIXTURE_ARTIFACTS': str(artifacts), 'SIGV4_PROXY_PORT': str(server.server_port)}
    try:
        result = subprocess.run([node, str(package / 'dist/github-entry.mjs'), '--embedded'], cwd=workspace,
                                env=env, capture_output=True, text=True, timeout=60)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    success = mode in {'success', 'steer', 'repository-read'}
    assert (result.returncode == 0) == success, result.stderr
    assert (artifacts / 'report.json').exists() == success
    assert len(requests) == (2 if mode in {'steer', 'repository-read'} else 0 if mode in {'tampered', 'repository-mismatch'} else 1), result.stderr
    if success:
        assert 'Working through' in (artifacts / 'progress.txt').read_text()
    if requests:
        assert f'personas/{persona}.md' in json.dumps(requests[0])
        assert requests[0]['model'] == 'gpt-5-codex'
    if mode == 'steer':
        assert 'Also inspect retry configuration.' in json.dumps(requests[1])
    if mode == 'unknown':
        operations = [json.loads(line) for line in (artifacts / 'operations.jsonl').read_text().splitlines()]
        assert [(value['kind'], value['action']) for value in operations] == [('model', 'claim')]

    if mode == 'repository-read':
        assert 'Pinned repository evidence 471.' in json.dumps(requests[1])
        operations = [json.loads(line) for line in (artifacts / 'operations.jsonl').read_text().splitlines()]
        assert [(value['kind'], value['action']) for value in operations].count(('tool', 'claim')) == 1
        assert [(value['kind'], value['action']) for value in operations].count(('tool', 'settle')) == 1
