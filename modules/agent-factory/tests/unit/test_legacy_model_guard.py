"""A rejected legacy preflight cannot reach either raw Bedrock API."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.credentials import Credentials

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('legacy_guard', ROOT / 'gateway/app/legacy_model_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

@pytest.mark.parametrize('body,allowed', [(b'{"legacy_permitted":true,"posture":"report_only"}', True), (b'{"legacy_permitted":true,"posture":"enforcing"}', False), (b'{}', False)])
def test_legacy_guard_requires_authoritative_permissive_posture(monkeypatch, body, allowed):
    monkeypatch.setenv('ADP_CHAT_MODEL_POLICY_ENABLED', 'true')
    monkeypatch.setenv('ADP_AGENT_CONTROL_ENDPOINT', 'https://api123.execute-api.us-east-1.amazonaws.com/dev/agent/internal/v1/agent')
    creds = Credentials('test', 'test', 'temporary')
    monkeypatch.setattr(guard.botocore.session, 'get_session', lambda: SimpleNamespace(get_credentials=lambda: creds))
    response = Mock(status=200)
    response.read.return_value = body
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(guard.urllib.request, 'build_opener', lambda *args: opener)
    if allowed:
        guard.require_legacy_chat_admission()
    else:
        with pytest.raises(RuntimeError, match='authority refused'):
            guard.require_legacy_chat_admission()
    assert opener.open.call_args.args[0].full_url.endswith('/legacy-chat-preflight')


def test_both_legacy_calls_guard_before_the_billable_boundary():
    import ast
    tree = ast.parse((ROOT / 'gateway/app/sqs_consumer.py').read_text())
    for name in ['_invoke_with_agent_sdk', '_invoke_raw_bedrock']:
        func = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        # First statement after the docstring refuses before either API client.
        assert ast.unparse(func.body[1]) == 'require_legacy_chat_admission()'
