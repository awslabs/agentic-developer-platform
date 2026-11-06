import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import operations_repair as repair
from lib.codex_workspace import WorkspaceError


def test_combined_restore_preserves_unicode_and_fails_explicitly_without_partial_history(monkeypatch, capsys):
    board = {"tasks": [{"id": "C1", "note": "😀" * 12000}]}
    delta = {"additions": {f"N{i}": {"id": f"N{i}", "title": "😀" * 300} for i in range(32)}}
    result = {"story_board": {"state": "available", "board": board}, "story_checkpoint": {"delta": delta},
        "operations_tasks": {"cycles": [{"evidence": [{"receipt_ref": "retained-fixture" * 20}]} for _ in range(10)]}}
    assert len(json.dumps(result).encode()) > 256 * 1024
    monkeypatch.setattr(repair.sys, "argv", ["operations_repair", "repair_tasks"])
    monkeypatch.setattr(repair.sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(repair, "invoke", lambda *_: result)
    repair.main()
    output = capsys.readouterr().out
    assert len(output.encode()) < 256 * 1024
    assert json.loads(output) == result
    result["operations_tasks"]["cycles"][0]["evidence"][0]["receipt_ref"] = "x" * (256 * 1024)
    monkeypatch.setattr(repair.sys, "stdin", io.StringIO("{}"))
    repair.main()
    refused = json.loads(capsys.readouterr().out)
    assert refused["error"] == "story_restore_response_oversized"
    assert refused["story_board"]["state"] == refused["story_checkpoint"]["state"] == "unavailable"
    assert "No partial tasks or evidence" in refused["next_action"]
    assert "operations_tasks" not in refused


def test_repeated_edits_publish_only_new_changes_and_use_gateway_merge(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init', '-b', 'agent/issue-12')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (tmp_path/'app.py').write_text('broken\n')
    git('add', '.')
    git('commit', '-m', 'base')
    monkeypatch.setenv('ADP_ENGINE_OPERATIONS_STORY', 'true')
    monkeypatch.setenv('AGENT_TYPE', 'agent-codex-operations')
    monkeypatch.setenv('WORK_DIR', str(tmp_path))
    state = {'repository': 'example/service', 'default_branch_head': git('rev-parse', 'HEAD'), 'branch_head': None}
    calls = []
    def request(body):
        calls.append(body)
        if body['operation'] == 'read_repository': return {'repository': state}
        if body['operation'] == 'validate_repair_edit':
            assert body['repository'] == state['repository']
            change = body['changes'][0]
            assert change['path'] in {'app.py', 'link'}
            import base64
            return {'path': change['path'], 'sha256': hashlib.sha256(base64.b64decode(change['content_base64'])).hexdigest()}
        if body['operation'] == 'publish_commit':
            assert body['expected_head'] == state['branch_head']
            state['branch_head'] = str(len(calls) % 10)*40
            return {'commit_sha': state['branch_head']}
        if body['operation'] == 'upsert_pull_request': return {'pull_request': {'number': 12}}
        if body['operation'] == 'merge_pull_request': return {'merge': {'merged': True, 'sha': 'b'*40}}
        raise AssertionError(body)
    monkeypatch.setattr(repair.github, '_request', request)
    for cycle in range(3):
        current = repair.invoke('repair_read', {'path': 'app.py'})
        text = f'fixed={cycle}\n'
        repair.invoke('repair_write', {'path': 'app.py', 'content': text, 'expected_sha256': current['sha256']})
        assert repair.invoke('repair_publish', {'title': 'fix deployment', 'body': 'Test in CI'})['pull_request']['number'] == 12
        assert repair.invoke('repair_merge', {'pull_number': 12, 'expected_head': state['branch_head']})['merge']['merged']
    assert len([c for c in calls if c['operation']=='publish_commit']) == 3
    assert len([c for c in calls if c['operation']=='validate_repair_edit']) == 3
    repair.invoke('repair_publish', {'title': 'fix deployment', 'body': 'Retry after response loss'})
    assert len([c for c in calls if c['operation']=='publish_commit']) == 3
    with pytest.raises(WorkspaceError):
        repair.invoke('repair_write', {'path': 'app.py', 'content': 'stale', 'expected_sha256': hashlib.sha256(b'broken\n').hexdigest()})
    with pytest.raises(WorkspaceError):
        repair.invoke('repair_read', {'path': '../credential'})
    (tmp_path/'link').symlink_to(tmp_path.parent/'outside')
    with pytest.raises(OSError):
        repair.invoke('repair_write', {'path': 'link', 'content': 'escape', 'expected_sha256': 'a'*64})


def test_gateway_refusal_prevents_local_edit(tmp_path, monkeypatch):
    (tmp_path / 'app.py').write_text('original')
    monkeypatch.setenv('ADP_ENGINE_OPERATIONS_STORY', 'true')
    monkeypatch.setenv('AGENT_TYPE', 'operations')
    monkeypatch.setenv('WORK_DIR', str(tmp_path))
    monkeypatch.setattr(repair.github, 'read_repository', lambda: {'repository': {'repository': 'example/service', 'default_branch_head': 'a'*40}})
    def refuse(_body):
        raise repair.github.MediatedRefused('repair path outside accepted scope')
    monkeypatch.setattr(repair.github, '_request', refuse)
    with pytest.raises(repair.github.MediatedRefused):
        repair.invoke('repair_write', {'path': 'app.py', 'content': 'changed', 'expected_sha256': hashlib.sha256(b'original').hexdigest()})
    assert (tmp_path / 'app.py').read_text() == 'original'


@pytest.mark.parametrize('agent_type', ['operations', 'agent-codex-operations'])
def test_both_stacks_receive_scope_diagnosis_without_editing_workspace(tmp_path, monkeypatch, capsys, agent_type):
    (tmp_path / 'app.py').write_text('original')
    monkeypatch.setenv('ADP_ENGINE_OPERATIONS_STORY', 'true')
    monkeypatch.setenv('AGENT_TYPE', agent_type)
    monkeypatch.setenv('WORK_DIR', str(tmp_path))
    monkeypatch.setattr(repair.github, 'read_repository', lambda: {'repository': {'repository': 'example/service', 'default_branch_head': 'a'*40}})
    message = 'repair_scope_not_accepted: Ask the flow owner to accept a plan amendment and reapprove invalidated limits.'
    def refuse(body):
        assert body['operation'] == 'validate_repair_edit'
        raise repair.github.MediatedRefused(message)
    monkeypatch.setattr(repair.github, '_request', refuse)
    monkeypatch.setattr(sys, 'argv', ['operations_repair', 'repair_write'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({
        'path': 'app.py', 'content': 'changed', 'expected_sha256': hashlib.sha256(b'original').hexdigest(),
    })))
    repair.main()
    assert json.loads(capsys.readouterr().out) == {'error': message}
    assert (tmp_path / 'app.py').read_text() == 'original'


def test_repair_tasks_are_brokered_without_local_progress_files(tmp_path, monkeypatch):
    monkeypatch.setenv('ADP_ENGINE_OPERATIONS_STORY', 'true')
    monkeypatch.setenv('AGENT_TYPE', 'operations')
    monkeypatch.setenv('WORK_DIR', str(tmp_path))
    monkeypatch.setattr(repair.github, 'read_repository', lambda: {'repository': {'repository': 'example/service', 'default_branch_head': 'a'*40}})
    requests = []
    monkeypatch.setattr(repair.github, '_request', lambda payload: requests.append(payload) or {'operations_tasks': {'cycles': []}})
    assert repair.invoke('repair_tasks', {})['operations_tasks']['cycles'] == []
    repair.invoke('repair_progress', {'stage': 'diagnose', 'state': 'done', 'diagnosis_code': 'runtime_failed'})
    assert requests == [
        {'operation': 'read_repair_tasks'},
        {'operation': 'record_repair_task', 'task_stage': 'diagnose', 'task_state': 'done', 'task_cycle': 1, 'diagnosis_code': 'runtime_failed'},
    ]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('name,args,operation', [
    ('repair_tasks', {}, 'read_repair_tasks'),
    ('repair_progress', {'stage': 'test', 'state': 'blocked'}, 'record_repair_task'),
    ('repair_status', {'pull_number': 12}, 'read_repository'),
    ('repair_merge', {'pull_number': 12, 'expected_head': 'a' * 40}, 'merge_pull_request'),
])
def test_gateway_tools_do_not_require_a_second_provider_read(monkeypatch, name, args, operation):
    monkeypatch.setenv('ADP_ENGINE_OPERATIONS_STORY', 'true')
    monkeypatch.setenv('AGENT_TYPE', 'operations')
    monkeypatch.setenv('WORK_DIR', '/unused-remote-only')
    monkeypatch.setattr(repair.github, 'read_repository', lambda: pytest.fail('Redundant provider read'))
    calls = []
    def request(body):
        calls.append(body)
        return {'observed': True}
    monkeypatch.setattr(repair.github, '_request', request)
    assert repair.invoke(name, args) == {'observed': True}
    assert len(calls) == 1 and calls[0]['operation'] == operation

    def refuse(body):
        raise repair.github.MediatedRefused('current authority denied')
    monkeypatch.setattr(repair.github, '_request', refuse)
    with pytest.raises(repair.github.MediatedRefused, match='current authority denied'):
        repair.invoke(name, args)


@pytest.fixture
def large_source(tmp_path, monkeypatch):
    import base64

    source = tmp_path / 'large.py'
    content = '# café\n' * 7000 + 'value = "original"\n' + '# end\n' * 2000
    source.write_text(content)
    source.chmod(0o755)
    monkeypatch.setenv('ADP_ENGINE_OPERATIONS_STORY', 'true')
    monkeypatch.setenv('AGENT_TYPE', 'operations')
    monkeypatch.setenv('WORK_DIR', str(tmp_path))
    monkeypatch.setattr(repair.github, 'read_repository', lambda: {'repository': {'repository': 'example/service', 'default_branch_head': 'a'*40}})
    validated = []

    def request(body):
        assert body['operation'] == 'validate_repair_edit'
        item = body['changes'][0]
        data = base64.b64decode(item['content_base64'])
        validated.append(data)
        return {'path': item['path'], 'sha256': hashlib.sha256(data).hexdigest()}

    monkeypatch.setattr(repair.github, '_request', request)
    return source, content, validated


def test_large_file_windows_reassemble_utf8_with_full_file_digest(large_source):
    source, original, validated = large_source
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    offset, pages = 0, []
    while offset is not None:
        page = repair.invoke('repair_read', {'path': source.name, 'offset': offset, 'expected_sha256': digest})
        assert page['sha256'] == digest and len(page['content'].encode()) <= 8192
        assert page['offset_unit'] == 'characters'
        pages.append(page['content'])
        offset = page['next_offset']
    assert ''.join(pages) == original and len(pages) > 4
    assert validated == []
    source.write_text(original + '# another editor\n')
    with pytest.raises(WorkspaceError, match='changed'):
        repair.invoke('repair_read', {'path': source.name, 'offset': 8192, 'expected_sha256': digest})


def test_exact_large_file_repair_validates_complete_result_and_preserves_rest(large_source):
    source, original, validated = large_source
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = repair.invoke('repair_replace', {'path': source.name, 'old_text': 'value = "original"',
        'new_text': 'value = "fixed"', 'expected_sha256': digest})
    expected = original.replace('value = "original"', 'value = "fixed"').encode()
    assert source.read_bytes() == expected
    assert validated == [expected]
    assert result['sha256'] == hashlib.sha256(expected).hexdigest()
    assert source.stat().st_mode & 0o777 == 0o755
    with pytest.raises(WorkspaceError, match='stale'):
        repair.invoke('repair_replace', {'path': source.name, 'old_text': 'value = "fixed"',
            'new_text': 'incorrect', 'expected_sha256': digest})
    assert validated == [expected]


@pytest.mark.parametrize('old_text', ['not present', '# end\n', ''])
def test_replacement_requires_one_nonempty_match(large_source, old_text):
    source, original, validated = large_source
    with pytest.raises(WorkspaceError):
        repair.invoke('repair_replace', {'path': source.name, 'old_text': old_text,
            'new_text': 'changed', 'expected_sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
    assert source.read_text() == original and not validated


def test_replacement_rejects_overlapping_matches(large_source):
    source, _, validated = large_source
    source.write_text('aaa')
    with pytest.raises(WorkspaceError, match='exactly once'):
        repair.invoke('repair_replace', {'path': source.name, 'old_text': 'aa', 'new_text': 'x',
            'expected_sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
    assert source.read_text() == 'aaa' and not validated


def test_policy_refusal_or_mismatched_receipt_cannot_edit_large_source(large_source, monkeypatch):
    source, original, _ = large_source
    args = {'path': source.name, 'old_text': 'value = "original"', 'new_text': 'value = "fixed"',
            'expected_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    monkeypatch.setattr(repair.github, '_request', lambda body: {'path': source.name, 'sha256': '0'*64})
    with pytest.raises(repair.github.MediatedRefused):
        repair.invoke('repair_replace', args)
    assert source.read_text() == original
    def refuse(body):
        raise repair.github.MediatedRefused('scope changed')
    monkeypatch.setattr(repair.github, '_request', refuse)
    with pytest.raises(repair.github.MediatedRefused):
        repair.invoke('repair_replace', args)
    assert source.read_text() == original


@pytest.mark.parametrize('args', [{'offset': -1}, {'offset': True}, {'offset': 999999}, {'limit': 8193}, {'limit': 0}])
def test_invalid_read_windows_are_refused(large_source, args):
    source, _, _ = large_source
    with pytest.raises(WorkspaceError):
        repair.invoke('repair_read', {'path': source.name, **args})


def test_large_source_bounds_and_links_remain_enforced(large_source):
    import os
    source, original, validated = large_source
    source.write_bytes(b'x' * (256 * 1024 + 1))
    with pytest.raises(WorkspaceError, match='256 KiB'):
        repair.invoke('repair_read', {'path': source.name})
    source.write_bytes(b'\xff')
    with pytest.raises(WorkspaceError, match='UTF-8'):
        repair.invoke('repair_read', {'path': source.name})
    source.write_text(original)
    link = source.with_name('linked.py')
    link.symlink_to(source)
    with pytest.raises(OSError):
        repair.invoke('repair_read', {'path': link.name})
    link.unlink()
    os.link(source, link)
    with pytest.raises(WorkspaceError, match='one link'):
        repair.invoke('repair_read', {'path': source.name})
    assert not validated


def test_invalid_progress_diagnosis_explains_correction_before_gateway_call(large_source):
    _, _, validated = large_source
    with pytest.raises(WorkspaceError, match='only accepted for stage diagnose'):
        repair.invoke('repair_progress', {'stage': 'deploy', 'state': 'in_progress', 'diagnosis_code': 'waiting'})
    assert not validated
