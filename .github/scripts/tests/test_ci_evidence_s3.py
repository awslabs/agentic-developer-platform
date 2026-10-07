"""Exercise real evidence packaging and fail-closed S3 receipt validation."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('ci_evidence', ROOT / '.github/scripts/upload_ci_evidence_s3.py')
uploader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uploader)
policy_spec = importlib.util.spec_from_file_location('ci_policy', ROOT / 'platform/scripts/ci-evidence-runner-policy.py')
policy = importlib.util.module_from_spec(policy_spec)
policy_spec.loader.exec_module(policy)


def test_pack_preserves_exact_checkout_evidence_and_checksums(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    body = b'{"checked_out_revision":"' + b'a' * 40 + b'"}\n'
    (root / 'checkout.json').write_bytes(body)
    (root / 'nested').mkdir()
    (root / 'nested' / 'image.png').write_bytes(b'png-test')
    archive = tmp_path / 'archive.tar.gz'
    rows = uploader.archive(uploader.collect(root), archive)
    with tarfile.open(archive) as tar:
        assert tar.extractfile('checkout.json').read() == body
        assert tar.extractfile('nested/image.png').read() == b'png-test'
    assert rows[0]['sha256'] == hashlib.sha256(body).hexdigest()
    first = archive.read_bytes()
    uploader.archive(uploader.collect(root), archive)
    assert archive.read_bytes() == first


@pytest.mark.parametrize('kind', ['file', 'directory', 'parent'])
def test_refuses_symlinks(tmp_path, kind):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('must not be uploaded')
    root = tmp_path / 'evidence'
    root.mkdir()
    if kind == 'parent':
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
        root = root / 'secret'
    else:
        (root / 'link').symlink_to(outside if kind == 'directory' else outside / 'secret')
    with pytest.raises(ValueError, match='symbolic link'):
        uploader.collect(root)


def test_empty_and_oversize_inputs_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='no files'):
        uploader.collect(tmp_path)
    (tmp_path / 'file').write_bytes(b'12345')
    monkeypatch.setattr(uploader, 'MAX_BYTES', 4)
    with pytest.raises(ValueError, match='bound'):
        uploader.collect(tmp_path)


@pytest.mark.parametrize('mismatch', ['ChecksumSHA256', 'ContentLength', 'VersionId', 'ServerSideEncryption'])
def test_upload_verifies_actual_object_receipt(tmp_path, mismatch):
    file = tmp_path / 'file'
    file.write_bytes(b'test')
    checksum = base64.b64encode(hashlib.sha256(b'test').digest()).decode()
    head = {'ChecksumSHA256': checksum, 'ContentLength': 4, 'VersionId': 'v1', 'ServerSideEncryption': 'AES256'}
    head[mismatch] = 'wrong'
    with patch.object(uploader, 'aws', side_effect=[{'VersionId': 'v1'}, head]) as api:
        with pytest.raises(ValueError, match='mismatch'):
            uploader.upload('bucket', 'key', file)
    assert '--if-none-match' in api.call_args_list[0].args


def test_identical_retry_uses_existing_version_without_overwrite(tmp_path):
    file = tmp_path / 'file'
    file.write_bytes(b'test')
    checksum = base64.b64encode(hashlib.sha256(b'test').digest()).decode()
    head = {'ChecksumSHA256': checksum, 'ContentLength': 4, 'VersionId': 'v1', 'ServerSideEncryption': 'AES256'}
    error = subprocess.CalledProcessError(1, ['aws'], stderr='PreconditionFailed')
    with patch.object(uploader, 'aws', side_effect=[error, head]):
        result = uploader.upload('bucket', 'key', file)
    assert result['version_id'] == 'v1'


def test_real_main_manifest_binds_source_and_separates_attempts(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    monkeypatch.delenv('GITHUB_OUTPUT', raising=False)
    workspace, temp = tmp_path / 'workspace', tmp_path / 'temp'
    workspace.mkdir()
    temp.mkdir()
    (workspace / 'report.xml').write_text('<testsuite/>')
    env = {'CI_EVIDENCE_NAME': 'tests', 'CI_EVIDENCE_PATH': 'report.xml', 'GITHUB_REPOSITORY': 'aws-e/adp',
           'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_JOB': 'tests',
           'GITHUB_WORKSPACE': str(workspace), 'RUNNER_TEMP': str(temp), 'GITHUB_WORKFLOW_SHA': 'b' * 40}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    manifests, keys = [], []
    def upload(bucket, key, file):
        keys.append(key)
        if file.name == 'manifest.json':
            manifests.append(json.loads(file.read_text()))
        return {'bucket': bucket, 'key': key, 'version_id': 'v1', 'sha256': hashlib.sha256(file.read_bytes()).hexdigest(), 'size': file.stat().st_size}
    with patch.object(uploader, 'aws', return_value={'Account': '123456789012'}), patch.object(uploader, 'upload', side_effect=upload), patch.object(uploader.subprocess, 'check_output', return_value='a' * 40):
        uploader.main()
        monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
        uploader.main()
    assert manifests[0]['checked_out_revision'] == 'a' * 40
    assert manifests[0]['workflow_revision'] == 'b' * 40
    assert '/123/1/tests/' in keys[0] and '/123/2/tests/' in keys[2]
    assert manifests[0]['files'][0]['path'] == 'report.xml'


def test_policy_changes_only_the_reviewed_object_resource():
    before = {'Statement': [{'Sid': 'OwnSmokeSource', 'Action': ['s3:GetObject', 's3:PutObject'], 'Resource': ['source']},
                            {'Sid': 'Keep', 'Effect': 'Deny', 'Action': 'iam:*', 'Resource': '*'}]}
    after = policy.updated(before, 'OwnSmokeSource', 'Resource', 'source', 'evidence')
    assert before['Statement'][0]['Resource'] == ['source']
    assert after['Statement'][0]['Resource'] == ['source', 'evidence']
    assert after['Statement'][1] == before['Statement'][1]
    before['Statement'][0]['Action'].append('s3:DeleteObject')
    with pytest.raises(ValueError, match='ceiling'):
        policy.updated(before, 'OwnSmokeSource', 'Resource', 'source', 'evidence')


def test_required_workflows_keep_arc_and_mandatory_s3_evidence():
    for name in ['agent-explanations-browser.yml', 'gateway-ci.yml', 'superplane-ui-browser-ci.yml', 'agent-control-ci.yml']:
        path = ROOT / '.github/workflows' / name
        text = path.read_text()
        workflow = yaml.safe_load(text)
        assert 'actions/upload-artifact@' not in text
        for job in workflow['jobs'].values():
            if 'uses' not in job:
                assert job['runs-on'] == 'arc-runner-org'
        uploads = [s for j in workflow['jobs'].values() for s in j.get('steps', [])
                   if s.get('uses') == './.github/actions/upload-ci-evidence-s3' or s.get('name') == 'Retain checkout evidence in S3']
        assert uploads
        for step in uploads:
            if step.get('name') != 'Upload Coverage':
                assert not step.get('continue-on-error')
        if name == 'agent-control-ci.yml':
            assert len(uploads) == 3
            for step in uploads:
                assert 'git show "$GITHUB_WORKFLOW_SHA:' in step['run']
                assert 'git checkout' not in step['run']
                assert step['env']['CI_EVIDENCE_NAME'].startswith('checked-out-revision-')
