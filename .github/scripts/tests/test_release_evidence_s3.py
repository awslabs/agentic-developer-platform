"""Exercise release evidence through the real CI packer and a versioned S3 fake."""
import base64
import hashlib
import json
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / '.github/scripts'))
import release_evidence_s3 as release


@pytest.fixture
def store(tmp_path, monkeypatch):
    workspace = tmp_path / 'checkout'
    workspace.mkdir()
    temporary = tmp_path / 'temp'
    temporary.mkdir()
    env = {'GITHUB_WORKSPACE': str(workspace), 'RUNNER_TEMP': str(temporary),
           'GITHUB_REPOSITORY': 'aws-e/adp', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
           'GITHUB_JOB': 'upgrade', 'GITHUB_WORKFLOW_SHA': 'b' * 40}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ('GITHUB_OUTPUT', 'GITHUB_STEP_SUMMARY'):
        monkeypatch.delenv(key, raising=False)
    bucket = 'adp-dev-ci-evidence-123456789012'
    objects = {}
    def aws(*args):
        if args[:2] == ('sts', 'get-caller-identity'):
            return {'Account': '123456789012'}
        key = args[args.index('--key') + 1]
        assert args[args.index('--bucket') + 1] == bucket
        if args[1] == 'put-object':
            data = Path(args[args.index('--body') + 1]).read_bytes()
            assert '--if-none-match' in args
            if key in objects:
                import subprocess
                raise subprocess.CalledProcessError(1, ['aws'], stderr='PreconditionFailed')
            objects[key] = data
        data = objects[key]
        result = {'VersionId': 'v1', 'ServerSideEncryption': 'AES256', 'ContentLength': len(data),
                  'ChecksumSHA256': base64.b64encode(hashlib.sha256(data).digest()).decode()}
        if args[1] == 'get-object':
            Path(args[-1]).write_bytes(data)
        return result
    run = {'id': 123, 'run_attempt': 2, 'conclusion': 'success', 'status': 'completed',
           'event': 'workflow_dispatch', 'head_branch': 'main', 'head_sha': 'b' * 40,
           'repository': {'full_name': 'aws-e/adp'}, 'path': '.github/workflows/adp-release.yml'}
    def command(args, **kwargs):
        return 'a' * 40 if args[0] == 'git' else json.dumps(run)
    monkeypatch.setattr(release.evidence, 'aws', aws)
    monkeypatch.setattr(release.subprocess, 'check_output', command)
    acceptance = {'status': 'passed', 'environment': 'integration-test', 'account': '000000000220',
                  'release_id': 'candidate', 'source_sha': 'a' * 40, 'manifest_sha256': 'c' * 64}
    path = workspace / 'acceptance.json'
    def upload(**changes):
        path.write_text(json.dumps(dict(acceptance, **changes)))
        release.upload('acceptance-integration-test', path, bucket)
    return bucket, objects, run, upload, path


def test_roundtrip_exact_old_candidate_and_identical_upload_retry(store, tmp_path):
    bucket, objects, run, upload, path = store
    upload()
    upload()
    release.download('123', tmp_path / 'download', bucket)
    assert (tmp_path / 'download/acceptance.json').read_bytes() == path.read_bytes()
    assert len(objects) == 3  # archive, content-addressed manifest, deterministic receipt


@pytest.mark.parametrize('field,value', [('status', 'failed'), ('environment', 'pre-production'), ('source_sha', 'd' * 40)])
def test_rejects_failed_or_wrong_source_acceptance(store, tmp_path, field, value):
    bucket, _, _, upload, _ = store
    upload(**{field: value})
    with pytest.raises(ValueError, match='Acceptance did not pass'):
        release.download('123', tmp_path / 'download', bucket)
    assert not (tmp_path / 'download/acceptance.json').exists()


@pytest.mark.parametrize('field,value', [('conclusion', 'failure'), ('status', 'in_progress'),
    ('event', 'pull_request'), ('head_branch', 'feature'), ('repository', {'full_name': 'other/repo'}),
    ('path', '.github/workflows/other.yml'), ('id', 456)])
def test_rejects_untrusted_workflow_runs(store, tmp_path, field, value):
    bucket, _, run, upload, _ = store
    upload()
    run[field] = value
    with pytest.raises(ValueError, match='successful integration release run'):
        release.download('123', tmp_path / 'download', bucket)


def test_never_falls_back_to_evidence_from_an_older_attempt(store, tmp_path):
    bucket, _, run, upload, _ = store
    upload()
    run['run_attempt'] = 3
    with pytest.raises(KeyError, match='/123/3/'):
        release.download('123', tmp_path / 'download', bucket)


def test_rejects_wrong_workflow_revision(store, tmp_path):
    bucket, _, run, upload, _ = store
    upload()
    run['head_sha'] = 'd' * 40
    with pytest.raises(ValueError, match='metadata differs'):
        release.download('123', tmp_path / 'download', bucket)


@pytest.mark.parametrize('tamper', ['archive', 'version', 'outside'])
def test_rejects_corruption_changed_version_and_cross_run_pointers(store, tmp_path, tamper):
    bucket, objects, _, upload, _ = store
    upload()
    pointer = next(k for k in objects if k.endswith('/receipt.json'))
    receipt = json.loads(objects[pointer])
    if tamper == 'archive':
        archive = next(k for k in objects if k.endswith('.tar.gz'))
        objects[archive] += b'corrupted'
    else:
        receipt['version_id' if tamper == 'version' else 'key'] = 'wrong'
        objects[pointer] = json.dumps(receipt).encode()
    with pytest.raises(ValueError, match='immutable receipt|outside the selected run'):
        release.download('123', tmp_path / 'download', bucket)


def test_release_uploader_uses_runner_irsa_without_changing_parent_credentials(store, monkeypatch):
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE'):
        monkeypatch.setenv(name, 'deployment')
    monkeypatch.setenv('AWS_ROLE_ARN', 'runner-role')
    monkeypatch.setenv('AWS_WEB_IDENTITY_TOKEN_FILE', '/runner-token')
    assert release.runner_bucket() == store[0]
    import os
    assert 'AWS_ACCESS_KEY_ID' not in os.environ and 'AWS_PROFILE' not in os.environ
    assert os.environ['AWS_ROLE_ARN'] == 'runner-role'
    assert os.environ['AWS_WEB_IDENTITY_TOKEN_FILE'] == '/runner-token'


def test_release_workflows_require_s3_evidence_on_arc_and_load_workflow_revision():
    for name in ('adp-release.yml', 'adp-release-upgrade.yml', 'adp-release-promote.yml'):
        text = (ROOT / '.github/workflows' / name).read_text()
        assert 'actions/upload-artifact@' not in text
        assert 'actions/download-artifact@' not in text
        doc = yaml.safe_load(text)
        for job in doc['jobs'].values():
            if 'uses' not in job:
                assert job['runs-on'] == 'arc-runner-org'
                assert not job.get('continue-on-error')
            for step in job.get('steps', []):
                if 'release_evidence_s3.py' in step.get('run', ''):
                    assert not step.get('continue-on-error')
                    if ' upload ' in step['run']:
                        assert 'git show "$GITHUB_WORKFLOW_SHA:' in step['run']
    reusable = yaml.safe_load((ROOT / '.github/workflows/adp-release-upgrade.yml').read_text())
    steps = reusable['jobs']['upgrade']['steps']
    download = next(i for i, s in enumerate(steps) if s.get('name') == 'Retrieve verified integration acceptance from S3')
    checkout = next(i for i, s in enumerate(steps) if s.get('name') == "Use the release's exact reviewed source")
    assert download < checkout
    upload = steps[-1]
    assert "steps.upgrade.outcome == 'success'" in upload['if']
    assert "steps.upgrade.outcome == 'failure'" in upload['if']
