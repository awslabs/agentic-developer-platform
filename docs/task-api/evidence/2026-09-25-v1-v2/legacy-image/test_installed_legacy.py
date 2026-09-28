"""Exercise installed full entrypoint with controlled external process/API receipts."""
import json
from unittest.mock import MagicMock
import pytest
import entrypoint
from tests import test_entrypoint as helpers

ready_gateway_proxy = helpers.ready_gateway_proxy

@pytest.mark.parametrize('persona', ['developer', 'agent-codex-reviewer'])
def test_installed_full_entrypoint_preserves_exec_and_finish(persona, monkeypatch, tmp_path):
    calls = []
    statuses = MagicMock()
    monkeypatch.setitem(helpers.SAMPLE_ENVELOPE, 'persona', persona)
    monkeypatch.setattr(entrypoint, 'update_invocation_status', statuses)
    if persona == 'agent-codex-reviewer':
        monkeypatch.setattr(entrypoint, '_handle_success', MagicMock(side_effect=AssertionError('Codex entered Claude finalization')))

    def execute(*args, **kwargs):
        command = args[0] if args else kwargs.get('args', [])
        if command and command[0] == 'node':
            calls.append((command, kwargs))
            return MagicMock(returncode=0, stdout='{"summary":"controlled Codex result"}\n', stderr='')
        if command[:2] == ['git', 'ls-remote']:
            return MagicMock(returncode=2, stdout='', stderr='')
        return MagicMock(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(helpers, '_subprocess_side_effect_fresh_branch', execute)
    helpers.TestEntrypointMain().test_full_sequence_success(monkeypatch=monkeypatch, tmp_path=tmp_path, start_refused=False)
    assert len(calls) == 1
    command, options = calls[0]
    if persona == 'agent-codex-reviewer':
        assert command == ['node', '/app/codex-reviewer/dist/index.js', '--embedded']
        assert json.loads(options['input']) == helpers.SAMPLE_ENVELOPE
        assert options['text'] is True and options['capture_output'] is True
        assert any(call.args[2] == 'complete' and call.kwargs.get('summary') == '{"summary":"controlled Codex result"}' for call in statuses.call_args_list)
    else:
        assert command == ['node', '/app/dist/agent-worker.js']
        assert 'input' not in options and 'capture_output' not in options
