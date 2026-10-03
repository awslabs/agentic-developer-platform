"""Packaged GitHub adapter with the real pinned SDK and local host fixtures."""
import json
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from planning_fixture import planning_output

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("persona,mode", [(p, "success") for p in ["architect", "product", "pm", "intent-refinement"]]
                         + [("architect", m) for m in ["steer", "unknown", "tampered", "repository-mismatch", "repository-read", "transient", "persistent-http", "budget-http", "long-discussion", "large-discussion", "many-tools", "large-output", "broken-stream"]])
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
    if mode == "budget-http":
        context["maxTurns"] = 1
    if mode in {"repository-read", "many-tools"}:
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
 }, async fail(error) { writeFileSync(process.env.FIXTURE_ARTIFACTS + '/failed', String(error)); } };
}
''')
    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({"id": ' + ('999' if mode == 'repository-mismatch' else '123')
                  + '} if sys.argv[1] == "api" and "/issues/" not in sys.argv[2] else [] if sys.argv[1] == "api" else {"number":12,"title":"Fixture architecture request","body":"Inspect supplied evidence", "comments":[]}))\n')
    if mode in {'long-discussion', 'large-discussion'}:
        # Shared architect rules consume ~49 KiB before task/schema text.
        # Preserve a substantial issue and amendments rather than dropping them.
        issue_payload = {"number": 12, "title": "Fixture architecture request", "body": "Original requirement. " * 250,
                         "comments": [{"id": "human-amendment", "author": {"login": "human"},
                                       "body": "Preserve this amendment. " * (12000 if mode == "large-discussion" else 650)}]}
        gh.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({"id":123} if sys.argv[1] == "api" else '
                      + repr(issue_payload) + '))\n')
    gh.chmod(0o700)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    repository_text = 'Pinned repository evidence 471.\n' + (' '.join(f'evidence{i:05d}' for i in range(1500)) if mode == 'repository-read' else '')
    (workspace / 'README.md').write_text(repository_text)
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
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if mode in {'persistent-http', 'budget-http'} or (mode == 'transient' and len(requests) == 1):
                self.send_response(500)
                self.end_headers()
                return
            response = {"id": "resp_fixture", "status": "completed", "output": [
                {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed", "phase": "final_answer",
                 "content": [{"type": "output_text", "text": json.dumps(planning_output(persona, "issue")), "annotations": []}]}],
                        "usage": {"input_tokens": 100, "output_tokens": 10}}
            if (mode == 'repository-read' and len(requests) == 1) or (mode == 'many-tools' and len(requests) <= 35):
                response["output"] = [{"id": f"fc_fixture_{len(requests)}", "type": "function_call", "call_id": f"call_fixture_{len(requests)}", "name": "repository_file",
                                       "namespace": "mcp__adp", "arguments": json.dumps({"path": "README.md"}), "status": "completed"}]
            if mode == 'large-output':
                artifact = planning_output(persona, "issue")
                artifact['artifact']['design'] = 'Detailed architecture. ' * 500
                response['output'][0]['content'][0]['text'] = json.dumps(artifact)
                response['output'].insert(0, {"id": "reasoning_fixture", "type": "reasoning", "encrypted_content": "x" * 70000, "summary": []})
                response['usage']['output_tokens'] = 12000
            body = ('event: response.completed\ndata: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n\n').encode()
            if mode == 'broken-stream':
                body = b'event: response.created\ndata: {"type":"response.created","response":{"status":"in_progress"}}\n\n'

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
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
    success = mode in {'success', 'steer', 'repository-read', 'transient', 'long-discussion', 'large-discussion', 'many-tools', 'large-output'}
    assert (result.returncode == 0) == success, result.stderr
    assert (artifacts / 'report.json').exists() == success
    assert len(requests) == (36 if mode == 'many-tools' else 3 if mode in {'persistent-http', 'budget-http'} else 2 if mode in {'steer', 'repository-read', 'transient'} else 0 if mode in {'tampered', 'repository-mismatch'} else 1), result.stderr
    if success:
        assert 'Working through' in (artifacts / 'progress.txt').read_text()
        assert 'planning_persona' in json.loads((artifacts / 'report.json').read_text())['result']['response']
    if requests:
        assert f'personas/{persona}.md' in json.dumps(requests[0])
        assert requests[0]['model'] == 'gpt-5-codex'
    if mode == 'steer':
        assert 'Also inspect retry configuration.' in json.dumps(requests[1])
    if mode == 'unknown':
        operations = [json.loads(line) for line in (artifacts / 'operations.jsonl').read_text().splitlines()]
        assert [(value['kind'], value['action']) for value in operations] == [('model', 'claim')]

    if mode == 'repository-read':
        assert any(repository_text in part.get('text', '') for item in requests[1]['input']
                   if item.get('type') == 'function_call_output' for part in item['output'])
        operations = [json.loads(line) for line in (artifacts / 'operations.jsonl').read_text().splitlines()]
        assert [(value['kind'], value['action']) for value in operations].count(('tool', 'claim')) == 1
        assert [(value['kind'], value['action']) for value in operations].count(('tool', 'settle')) == 1

    if mode in {'transient', 'persistent-http', 'budget-http'}:
        operations = [json.loads(line) for line in (artifacts / 'operations.jsonl').read_text().splitlines()]
        model_ops = [value for value in operations if value['kind'] == 'model']
        assert [value['action'] for value in model_ops] == ['claim', 'settle'] * len(requests)
        assert len({value['operation_id'] for value in model_ops}) == len(requests)
        assert json.loads(model_ops[1]['result']) == {'httpStatus': 500}
        if mode == 'persistent-http':
            assert 'HTTP 500' in (artifacts / 'failed').read_text()

    if mode in {'long-discussion', 'large-discussion'}:
        prompt = json.dumps(requests[0])
        assert 'Original requirement. ' * 250 in prompt
        assert ('Preserve this amendment. ' * (12000 if mode == 'large-discussion' else 650)).strip() in prompt
        if mode == 'large-discussion':
            assert len(prompt.encode()) > 256 * 1024

    if mode == 'many-tools':
        operations = [json.loads(line) for line in (artifacts / 'operations.jsonl').read_text().splitlines()]
        assert sum(op['kind'] == 'tool' and op['action'] == 'settle' for op in operations) == 35
        assert sum(op['kind'] == 'model' and op['action'] == 'settle' for op in operations) == 36

    if requests:
        assert 'max_output_tokens' not in requests[0]
        assert requests[0]['stream'] is True
    if mode == 'large-output':
        report = json.loads((artifacts / 'report.json').read_text())
        assert ('Detailed architecture. ' * 500).strip() in report['result']['response']
        assert report['result']['usage']['output_tokens'] == 12000

    if mode == 'broken-stream':
        operations = [json.loads(line) for line in (artifacts / 'operations.jsonl').read_text().splitlines()]
        assert [(value['kind'], value['action']) for value in operations] == [('model', 'claim')]
