"""Root producer transport binds the complete request and preserves queue bytes."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.credentials import Credentials

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'webhook-ingress/lambda/common/model_root_client.py'
spec = importlib.util.spec_from_file_location('pmm_root_client', SOURCE)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def test_transport_copies_are_identical():
    assert SOURCE.read_bytes() == (ROOT / 'gateway/lambdas/ingest/model_root_client.py').read_bytes()

@pytest.mark.parametrize('refusal', [None, 'static', 'changed', 'oversize'])
def test_root_transport_binds_sts_proof_and_refuses_untrusted_receipts(monkeypatch, refusal):
    monkeypatch.setenv('ADP_AGENT_CONTROL_ENDPOINT', 'https://api123.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent')
    creds = Credentials('example', 'example', None if refusal == 'static' else 'temporary')
    monkeypatch.setattr(client.botocore.session, 'get_session', lambda: SimpleNamespace(get_credentials=lambda: creds))
    envelope = {'message_id': 'run-1', 'tenant_id': 'tenant', 'persona': 'developer', 'metadata': {'fraction': 1.0}}
    final = envelope | {'correlation': {'root_human_id': 'human'}}
    receipt = {'envelope': final, 'envelope_json': client._canonical(final).decode()}
    if refusal == 'changed':
        receipt['envelope_json'] = json.dumps(final | {'message_id': 'other'})
    raw = b'x' * 262145 if refusal == 'oversize' else json.dumps(receipt).encode()
    def open_request(request, timeout):
        assert 0 < timeout <= 6
        document = json.loads(request.data)
        assert document['subject'] == 'human-sub'
        proof = json.loads(base64.b64decode(request.headers['X-adp-producer-proof']))
        assert proof['x-adp-work-invocation'] == hashlib.sha256(request.data).hexdigest()
        assert 'x-adp-work-invocation;' in proof['authorization']
        assert document['envelope'] == envelope
        response = Mock(status=200)
        response.read.return_value = raw
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response
    opener = SimpleNamespace(open=Mock(side_effect=open_request))
    monkeypatch.setattr(client.urllib.request, 'build_opener', lambda *args: opener)
    if refusal:
        with pytest.raises(client.RootRegistrationRefusedError):
            client.register_model_root(envelope, source='chat', subject='human-sub')
    else:
        assert client.register_model_root(envelope, source='chat', subject='human-sub') == receipt['envelope_json']
    if refusal == 'static':
        opener.open.assert_not_called()
