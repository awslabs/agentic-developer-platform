"""Exercise real evidence packaging and fail-closed S3 receipt validation."""
import base64
import hashlib
import importlib.util
import json
import os
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
    (workspace / 'source').mkdir()
    (workspace / 'report.xml').write_text('<testsuite/>')
    env = {'CI_EVIDENCE_NAME': 'tests', 'CI_EVIDENCE_PATH': 'report.xml\noptional/', 'GITHUB_REPOSITORY': 'aws-e/adp',
           'CI_EVIDENCE_SOURCE_DIRECTORY': 'source', 'CI_EVIDENCE_IF_NO_FILES_FOUND': 'warn',
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
    with patch.object(uploader, 'aws', return_value={'Account': '123456789012'}), patch.object(uploader, 'upload', side_effect=upload), patch.object(uploader.subprocess, 'check_output', return_value='a' * 40) as git:
        uploader.main()
        assert git.call_args.args[0] == ['git', '-C', str(workspace / 'source'), 'rev-parse', 'HEAD']
        monkeypatch.setenv('CI_EVIDENCE_SOURCE_DIRECTORY', str(temp))
        with pytest.raises(ValueError, match='Source checkout'):
            uploader.main()
        monkeypatch.setenv('CI_EVIDENCE_SOURCE_DIRECTORY', 'source')
        monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
        uploader.main()
    assert manifests[0]['checked_out_revision'] == 'a' * 40
    assert manifests[0]['workflow_revision'] == 'b' * 40
    assert '/123/1/tests/' in keys[0] and '/123/2/tests/' in keys[2]
    assert manifests[0]['files'][0]['path'] == 'report.xml'
    assert manifests[0]['missing_path_count'] == 1


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


def test_multiple_inputs_globs_and_exact_file_allowlist(tmp_path):
    workspace, temp = tmp_path / 'workspace', tmp_path / 'temp'
    workspace.mkdir()
    temp.mkdir()
    (workspace / 'reports').mkdir()
    (workspace / 'reports' / 'junit.xml').write_text('<testsuite/>')
    (workspace / 'state.json').write_text('private state must not be collected')
    (temp / 'latency.json').write_text('{}')
    files, missing = uploader.collect_paths(f'reports/\n{temp}/*.json\nreports/', workspace, temp)
    assert [(p.name, name) for p, name in files] == [('junit.xml', 'junit.xml'), ('latency.json', 'latency.json')]
    assert missing == 0


@pytest.mark.parametrize('policy', ['error', 'warn', 'ignore'])
def test_missing_and_recursively_empty_paths(tmp_path, policy, capsys):
    (tmp_path / 'empty' / 'nested').mkdir(parents=True)
    value = 'absent*.xml\nempty/'
    if policy == 'error':
        with pytest.raises(ValueError, match='no files'):
            uploader.collect_paths(value, tmp_path, tmp_path, policy)
    else:
        assert uploader.collect_paths(value, tmp_path, tmp_path, policy) == ([], 2)
        assert ('::warning::' in capsys.readouterr().out) == (policy == 'warn')


def test_partial_upload_records_missing_paths(tmp_path):
    (tmp_path / 'junit.xml').write_text('<testsuite/>')
    files, missing = uploader.collect_paths('junit.xml\nscreenshots/', tmp_path, tmp_path, 'warn')
    assert len(files) == 1 and missing == 1


@pytest.mark.parametrize('value', ['../secret', '/etc/passwd', '!*.json'])
def test_input_cannot_escape_allowed_roots(tmp_path, value):
    with pytest.raises(ValueError):
        uploader.collect_paths(value, tmp_path, tmp_path, 'ignore')


def test_glob_cannot_follow_symlink_even_inside_workspace(tmp_path):
    (tmp_path / 'real').mkdir()
    (tmp_path / 'real' / 'secret.json').write_text('private')
    (tmp_path / 'link').symlink_to(tmp_path / 'real', target_is_directory=True)
    with pytest.raises(ValueError, match='symbolic link'):
        uploader.collect_paths('link/*.json', tmp_path, tmp_path)


def test_archive_conflicts_and_combined_size_bound(tmp_path, monkeypatch):
    for directory in ['one', 'two']:
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 'result.json').write_text('123')
    with pytest.raises(ValueError, match='conflicting'):
        uploader.collect_paths('one/\ntwo/', tmp_path, tmp_path)
    (tmp_path / 'two' / 'result.json').rename(tmp_path / 'two' / 'other.json')
    monkeypatch.setattr(uploader, 'MAX_BYTES', 5)
    with pytest.raises(ValueError, match='bound'):
        uploader.collect_paths('one/\ntwo/', tmp_path, tmp_path)


def test_service_receipt_glob_keeps_service_paths_and_excludes_private_inputs(tmp_path):
    for service in ['foundation', 'security']:
        directory = tmp_path / 'receipts' / service
        directory.mkdir(parents=True)
        (directory / 'receipt.json').write_text('{}')
        (directory / 'inputs.tfvars.json').write_text('private input')
        (directory / 'terraform.log').write_text('private log')
    (tmp_path / 'receipts' / 'security' / 'failure-receipt.json').write_text('{}')
    files, missing = uploader.collect_paths('receipts/*/*receipt.json', tmp_path, tmp_path)
    assert missing == 0
    assert [name for _, name in files] == ['foundation/receipt.json', 'security/failure-receipt.json', 'security/receipt.json']


def test_uploader_uses_runner_irsa_without_changing_parent_credentials(tmp_path):
    env = dict(os.environ, AWS_ACCESS_KEY_ID='deployment-key', AWS_SECRET_ACCESS_KEY='deployment-secret',
               AWS_SESSION_TOKEN='deployment-session', AWS_PROFILE='deployment',
               AWS_CONFIG_FILE='/deployment/config', AWS_SHARED_CREDENTIALS_FILE='/deployment/credentials',
               AWS_CONTAINER_CREDENTIALS_FULL_URI='http://credential-provider.invalid',
               AWS_ROLE_ARN='runner-role', AWS_WEB_IDENTITY_TOKEN_FILE=str(tmp_path / 'token'))
    code = '''import json, os, sys
sys.path.insert(0, sys.argv[1])
from upload_ci_evidence_s3 import runner_credentials
runner_credentials()
assert not any(k in os.environ for k in ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN', 'AWS_PROFILE', 'AWS_CONTAINER_CREDENTIALS_FULL_URI'])
assert os.environ['AWS_ROLE_ARN'] == 'runner-role'
assert os.environ['AWS_CONFIG_FILE'] == '/dev/null'
assert os.environ['AWS_SHARED_CREDENTIALS_FILE'] == '/dev/null'
'''
    subprocess.run([os.sys.executable, '-c', code, str(ROOT / '.github/scripts')], env=env, check=True)
    assert env['AWS_ACCESS_KEY_ID'] == 'deployment-key'


def test_missing_irsa_never_falls_back_to_deployment_credentials(monkeypatch):
    for key in ['AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'CI_EVIDENCE_RUNNER_ROLE_ARN', 'CI_EVIDENCE_RUNNER_TOKEN_FILE']:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'deployment-key')
    with pytest.raises(ValueError, match='IRSA'):
        uploader.runner_credentials()


# Deploy workflows upload evidence from the dedicated deployment pool; the pin is
# shared with the release and trust-contract tests.
RUNNER_POOLS = json.loads((ROOT / '.github/runner-pools.json').read_text())
DEPLOY_POOL_WORKFLOWS = frozenset(RUNNER_POOLS['arc-runner-deploy'])
DEPLOY_POOL_JOBS = RUNNER_POOLS['arc-runner-deploy-jobs']


def test_ordinary_ci_uploads_use_s3_and_handoffs_remain_explicit():
    # These transports have authenticated runtime readers or restore consumers.
    # Remove an exception only together with its reader migration and rollout.
    # Product manifests and delivery readers now use signed S3 handoffs. Their
    # private attempt diagnostics use the ordinary runner evidence transport.
    handoffs = {
        'dev-box-deploy.yml': {
            'adp-deployment-context-dev-box-deploy.yml-${{ github.run_attempt }}',
            'adp-release-dev-box-${{ github.run_attempt }}',
        },
        # DevLake's finalized cleanup has an exact authenticated source_reader
        # binding; the qualifier's two archives feed repository evaluation and
        # ReleaseProvider. Intermediate source measurements use ordinary S3.
        'devlake-poc-infrastructure-source.yml': {
            'devlake-infrastructure-finalized-${{ github.run_id }}-${{ github.run_attempt }}',
        },
        'devlake-poc-qualify.yml': {
            'devlake-pilot-${{ github.run_id }}-${{ github.run_attempt }}',
            'devlake-pilot-delivery-${{ github.run_attempt }}',
        },
        'eval-claude-desktop-gateway.yml': {
            'adp-deployment-context-eval-claude-desktop-gateway.yml-${{ github.run_attempt }}',
        },
        'eval-cli-uplift.yml': {
            'adp-deployment-context-eval-cli-uplift.yml-${{ github.run_attempt }}',
            'e32-qualification-result',
        },
        'insights-deploy.yml': {
            'adp-deployment-context-${{ github.run_attempt }}',
            'adp-release-insights-${{ github.run_attempt }}',
        },
        'orchestration-live-tests.yml': {
            'orchestration-evaluation-${{ github.run_id }}-${{ github.run_attempt }}',
            'qualification-${{ github.event.inputs.mode }}-${{ github.run_id }}',
        },
        'tools-platform-deploy.yml': {
            'atlassian-identity-${{ github.run_id }}-${{ github.run_attempt }}',
        },
    }
    actual = {}
    for path in (ROOT / '.github/workflows').glob('*.yml'):
        for job_name, job in yaml.safe_load(path.read_text()).get('jobs', {}).items():
            for step in job.get('steps', []):
                uses = step.get('uses', '')
                if uses.startswith('actions/upload-artifact@'):
                    actual.setdefault(path.name, set()).add(step['with']['name'])
                if uses.endswith('/upload-ci-evidence-s3'):
                    deploy_pool = path.name in DEPLOY_POOL_WORKFLOWS or job_name in DEPLOY_POOL_JOBS.get(path.name, [])
                    expected_pool = 'arc-runner-deploy' if deploy_pool else 'arc-runner-org'
                    if path.name == 'l5-services-release.yml' and job_name == 'publish':
                        assert job['runs-on'] == "${{ inputs.service == 'transform' && 'ubuntu-24.04' || 'arc-runner-org' }}"
                        # The hosted branch has no IRSA. Its selected release
                        # authority retains receipts through a separate step.
                        assert step['if'] == "inputs.service != 'transform'"
                    else:
                        assert job['runs-on'] == expected_pool
                    assert str(step['with'].get('retention-days', 90)) in {'7', '14', '90'}
    assert actual == handoffs
    action = yaml.safe_load((ROOT / '.github/actions/upload-ci-evidence-s3/action.yml').read_text())
    # Composite's explicit shell retains IRSA while offline test shells scrub it.
    assert action['runs']['steps'][0]['shell'] == 'bash'


@pytest.mark.parametrize('workflow_name,artifact_name,condition', [
    ('devlake-poc-provider-profile.yml', 'devlake-provider-profile', None),
    ('devlake-poc-native-source.yml', 'devlake-native-readback', None),
    ('devlake-poc-infrastructure-source.yml', 'devlake-infrastructure-active', "inputs.phase == 'active'"),
])
def test_devlake_intermediate_measurements_use_scoped_s3(workflow_name, artifact_name, condition):
    workflow = yaml.safe_load((ROOT / '.github/workflows' / workflow_name).read_text())
    job = workflow['jobs']['observe']
    assert job['runs-on'] == 'arc-runner-org'
    upload = next(step for step in job['steps'] if step.get('uses') == './.github/actions/upload-ci-evidence-s3')
    assert upload.get('if') == condition and not upload.get('continue-on-error')
    assert upload['with'] == {
        'name': artifact_name + '-${{ github.run_id }}-${{ github.run_attempt }}',
        'path': '${{ runner.temp }}/observation.json', 'if-no-files-found': 'error', 'retention-days': 7,
    }
    # No publication token or new cloud authority is needed for runner evidence.
    assert set(workflow['permissions']) <= {'contents', 'actions'}
    assert set(workflow['permissions'].values()) == {'read'}
    github_uploads = [step for step in job['steps'] if step.get('uses', '').startswith('actions/upload-artifact@')]
    if condition:
        assert len(github_uploads) == 1 and github_uploads[0]['if'] == "inputs.phase == 'finalized'"
    else:
        assert github_uploads == []


def test_devlake_retained_handoffs_match_the_authenticated_reader_contracts():
    proposal = json.loads((ROOT / 'docs/engineering-insights/devlake-poc/delivery-flow/proposal.authored.json').read_text())

    def objects(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from objects(child)

    declared = list(objects(proposal))
    pilot = next(item for item in declared if item.get('criterion_id') == 'pilot.workflow')
    delivery = next(item for item in declared if item.get('evidence_artifact') == 'devlake-pilot-delivery')
    workflow = yaml.safe_load((ROOT / pilot['path']).read_text())
    uploads = {step['with']['name']: step['with'] for step in workflow['jobs']['qualify']['steps']
               if step.get('uses', '').startswith('actions/upload-artifact@')}
    expected = pilot['artifacts'][0]
    pilot_name = expected['name'].replace('{run_id}', '${{ github.run_id }}').replace('{run_attempt}', '${{ github.run_attempt }}')
    assert expected['bind_workflow_provenance'] is True and pilot['required_jobs'] == ['qualify']
    assert uploads[pilot_name]['path'] == '${{ runner.temp }}/' + expected['path']
    assert uploads[delivery['evidence_artifact'] + '-${{ github.run_attempt }}']['path'] == '${{ runner.temp }}/delivery.json'
    assert len(uploads) == 2 and all(item['retention-days'] == 7 and item['if-no-files-found'] == 'error' for item in uploads.values())
    # Both reader contracts default to GitHub archives for this workflow. The
    # diagnostic uploader must not silently opt these handoffs into S3 transport.
    assert workflow.get('env', {}).get('ADP_REPOSITORY_EVIDENCE_STORE', 'github-artifact') == 'github-artifact'
    assert workflow.get('env', {}).get('ADP_DELIVERY_EVIDENCE_STORE', 'github-artifact') == 'github-artifact'

    source = ROOT / 'modules/insights/poc/devlake/source_reader.py'
    spec = importlib.util.spec_from_file_location('devlake_source_reader', source)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    infrastructure = yaml.safe_load((ROOT / '.github/workflows/devlake-poc-infrastructure-source.yml').read_text())
    step = next(step for step in infrastructure['jobs']['observe']['steps'] if step.get('uses', '').startswith('actions/upload-artifact@'))
    name = step['with']['name'].replace('${{ github.run_id }}', '{run_id}').replace('${{ github.run_attempt }}', '{run_attempt}')
    binding = {'workflow': '.github/workflows/devlake-poc-infrastructure-source.yml', 'job': 'observe', 'artifact': name}
    bindings = {'native': {'workflow': '.github/workflows/approved-native.yml', 'job': 'observe', 'artifact': 'native-{run_id}-{run_attempt}'},
                'providers': {'workflow': '.github/workflows/devlake-poc-provider-profile.yml',
                              'job': 'observe', 'artifact': 'devlake-provider-profile-{run_id}-{run_attempt}'},
                'delivery_cleanup': binding}
    assert step['if'] == "inputs.phase == 'finalized'"
    assert step['with']['path'] == '${{ runner.temp }}/observation.json' and step['with']['retention-days'] == 7
    # Exercise the real reader's binding admission, including the phase fence.
    assert callable(reader.approved_reader({'delivery_cleanup_receipt': {}}, {'criteria': []}, bindings))
    with pytest.raises(ValueError, match='inventory is incomplete'):
        reader.approved_reader({'delivery_cleanup_receipt': {}}, {'criteria': []},
                               {key: value for key, value in bindings.items() if key != 'providers'})
    bindings['delivery_cleanup'] = {**binding, 'artifact': name.replace('finalized', 'active')}
    with pytest.raises(ValueError, match='finalized infrastructure source'):
        reader.approved_reader({'delivery_cleanup_receipt': {}}, {'criteria': []}, bindings)


@pytest.mark.parametrize('job_name,step_name,relative_path', [
    ('adapter-handoff', 'Publish correlated generic handoff delivery evidence',
     'adp-adapter-handoff/release/delivery.json'),
    ('adapter-identity', 'Publish correlated generic delivery evidence',
     'adp-adapter-stage/delivery.json'),
])
def test_tools_delivery_handoffs_keep_signed_s3_publication(job_name, step_name, relative_path):
    workflow = yaml.safe_load((ROOT / '.github/workflows/tools-platform-deploy.yml').read_text())
    assert workflow['env']['ADP_DELIVERY_EVIDENCE_STORE'] == 's3-oidc-v1'
    step = next(step for step in workflow['jobs'][job_name]['steps'] if step.get('name') == step_name)
    assert not step.get('continue-on-error')
    assert 'uses' not in step
    assert step['env']['ADP_WORKFLOW_REVISION'] == '${{ github.workflow_sha }}'
    assert step['env']['ACCOUNT_ID'] == '${{ vars.ADP_TOOLS_ACCOUNT_ID }}'
    assert step['run'] == (
        '"$RUNNER_TEMP/adp-deployment-evidence/runtime/bin/python" '
        '"$RUNNER_TEMP/adp-deployment-evidence/scripts/publish-deployment-evidence.py" '
        f'delivery delivery-evidence "$RUNNER_TEMP/{relative_path}"'
    )


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
            checkouts = [step for step in uploads if step.get('name') == 'Retain checkout evidence in S3']
            assert {step['env']['CI_EVIDENCE_NAME'] for step in checkouts} == {
                'checked-out-revision-agent-control-tests',
                'checked-out-revision-agent-authority-${{ matrix.shard }}',
                'checked-out-revision-worker-control-tests',
                'checked-out-revision-control-evaluation-harness-tests',
            }
            assert len(checkouts) == 4
            for step in checkouts:
                assert 'git show "$GITHUB_WORKFLOW_SHA:' in step['run']
                assert 'git checkout' not in step['run']
                assert step['env']['CI_EVIDENCE_NAME'].startswith('checked-out-revision-')
            timings = [step for step in uploads if step not in checkouts]
            assert len(timings) == 1
            assert timings[0]['with']['name'] == 'authority-test-timing-${{ matrix.shard }}'
            assert timings[0]['if'] == "${{ always() && hashFiles('modules/gateway/tests/ci_sharding.py') != '' }}"


def test_atlassian_native_handoff_uses_only_exact_sanitized_summary(tmp_path):
    workspace, temporary = tmp_path / 'workspace', tmp_path / 'runner evidence'
    workspace.mkdir()
    temporary.mkdir()
    (temporary / 'identity-proof').mkdir()
    (temporary / 'identity-proof' / 'evidence.json').write_text('private-token-sentinel')
    (temporary / 'identity-proof' / 'junit.xml').write_text('private-site-sentinel')
    (temporary / 'atlassian-stage-summary.json').write_text('{"live_status":"unproven"}')
    workflow = yaml.load((ROOT / '.github/workflows/gateway-deploy.yml').read_text(), Loader=yaml.BaseLoader)
    steps = workflow['jobs']['identity-proof']['steps']
    upload = next(step for step in steps if step.get('name') == 'Publish bounded proof summary for native evaluation')
    assert not upload.get('continue-on-error')
    assert upload['env']['EXPECTED_SOURCE_REVISION'] == '${{ inputs.adp_source_revision }}'
    assert upload['env']['ADP_WORKFLOW_REVISION'] == '${{ github.workflow_sha }}'
    # Exercise the actual workflow shell with a local publisher recorder. No AWS
    # calls occur; the publisher's signed archive/reader checks have their own
    # tests. This boundary must pass only the exact sanitized summary, even when
    # the runner directory has spaces and adjacent files contain private data.
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    recorder = binaries / 'python'
    recorder.write_text(f'#!{os.sys.executable}\n' + '''import json, os, sys
from pathlib import Path
source = Path(sys.argv[-1])
Path(os.environ['CAPTURE']).write_text(json.dumps({'argv': sys.argv[1:], 'name': source.name, 'body': source.read_text()}))
''')
    recorder.chmod(0o755)
    capture = tmp_path / 'capture.json'
    env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
           'RUNNER_TEMP': str(temporary), 'CAPTURE': str(capture)}
    subprocess.run(['bash', '-euo', 'pipefail', '-c', upload['run']], cwd=workspace, env=env, check=True)
    result = json.loads(capture.read_text())
    assert result['argv'] == ['modules/gateway/scripts/publish-deployment-evidence.py', 'repository-artifact',
                              'identity-proof', str(temporary / 'atlassian-stage-summary.json')]
    assert result['name'] == 'atlassian-stage-summary.json'
    assert result['body'] == '{"live_status":"unproven"}'
    assert 'private-' not in capture.read_text()
    (temporary / 'atlassian-stage-summary.json').unlink()
    assert subprocess.run(['bash', '-euo', 'pipefail', '-c', upload['run']], cwd=workspace, env=env,
                          capture_output=True).returncode != 0
