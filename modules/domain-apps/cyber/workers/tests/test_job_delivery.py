"""Exercise exact-version capabilities, including partial-download rejection."""
import copy
import hashlib
import time
from contextlib import nullcontext
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_delivery import download_sample
from sample_access import AccessDenied, ObjectRef

REF = ObjectRef('samples', 'o/org/t/team/u/user/s/session/task/in/sample.bin')
DATA = b'immutable sample'


def manifest():
    return {
        'expires_at': int(time.time()) + 100,
        'sample_download': {
            'url': 'https://samples.s3.us-east-1.amazonaws.com/' + REF.key + '?versionId=v1',
            'version': 'v1', 'sha256': hashlib.sha256(DATA).hexdigest(), 'size': len(DATA),
        },
    }


def session(monkeypatch, *, data=DATA, status=200):
    response = Mock(status_code=status)
    response.iter_content.return_value = [data]
    client = Mock()
    client.get.return_value = nullcontext(response)
    monkeypatch.setattr('job_delivery.requests.Session', lambda: nullcontext(client))
    return client


def test_exact_sample_download(monkeypatch, tmp_path):
    client = session(monkeypatch)
    body = manifest()
    dest = tmp_path / 'sample'
    download_sample(body, REF, dest)
    assert dest.read_bytes() == DATA
    assert client.trust_env is False
    assert client.get.call_args.kwargs == {'stream': True, 'allow_redirects': False, 'timeout': (3, 30)}


@pytest.mark.parametrize('field,value', [
    ('url', 'http://samples.s3.amazonaws.com/' + REF.key + '?versionId=v1'),
    ('url', 'https://samples.s3.amazonaws.com.evil.test/' + REF.key + '?versionId=v1'),
    ('url', 'https://samples.s3.amazonaws.com/other-job?versionId=v1'),
    ('url', 'https://samples.s3.amazonaws.com/' + REF.key + '?versionId=v2'),
    ('url', 'https://samples.s3.amazonaws.com/' + REF.key + '?versionId=v1&versionId=v2'),
    ('version', 'null'), ('size', 0), ('size', 64 * 1024 * 1024 + 1),
])
def test_invalid_capability_never_connects(monkeypatch, tmp_path, field, value):
    client = session(monkeypatch)
    body = manifest()
    body['sample_download'][field] = value
    with pytest.raises(AccessDenied):
        download_sample(body, REF, tmp_path / 'sample')
    assert not client.get.called


@pytest.mark.parametrize('case', ['redirect', 'tamper', 'truncated', 'oversized', 'expired'])
def test_failed_stream_removes_partial_sample(monkeypatch, tmp_path, case):
    body = copy.deepcopy(manifest())
    data = {'tamper': b'x' * len(DATA), 'truncated': DATA[:-1], 'oversized': DATA + b'x'}.get(case, DATA)
    session(monkeypatch, data=data, status=302 if case == 'redirect' else 200)
    if case == 'expired':
        body['expires_at'] = 1
    dest = tmp_path / 'sample'
    with pytest.raises(AccessDenied):
        download_sample(body, REF, dest)
    assert not dest.exists()
