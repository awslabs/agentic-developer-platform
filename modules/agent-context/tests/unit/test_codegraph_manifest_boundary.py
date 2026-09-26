"""Offline receipt/render/deploy boundary; every kubectl call uses a local stub."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

MODULE = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('codegraph_renderer', MODULE / 'scripts/render-codegraph.py')
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)
DIGEST = 'a' * 64
IMAGE = 'example.test/codegraph@sha256:' + DIGEST


@pytest.fixture
def receipt(tmp_path):
    value = {'fixture': 'codegraph-runtime-v1', 'result': 'passed', 'revision': 'b' * 40,
             'source_archive_sha256': 'c' * 64,
             'image_id': 'sha256:' + 'd' * 64,
             'image_repo_digests': ['local-fixture@sha256:' + DIGEST]}
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize('variant', ['current', 'legacy'])
def test_rendered_workload_has_only_prebaked_startup_and_explicit_storage(receipt, variant):
    rendered = RENDERER.render(IMAGE, receipt, 'fixture-ns', 'fixture-sa', variant)
    assert '${' not in rendered
    deployment, service = list(yaml.safe_load_all(rendered))
    assert deployment['metadata']['namespace'] == service['metadata']['namespace'] == 'fixture-ns'
    pod = deployment['spec']['template']['spec']
    assert pod['serviceAccountName'] == 'fixture-sa'
    assert pod['automountServiceAccountToken'] is False
    assert not pod.get('initContainers')
    assert pod['securityContext'] == {
        'runAsUser': 10001, 'runAsGroup': 10001, 'fsGroup': 10001,
        'fsGroupChangePolicy': 'OnRootMismatch', 'seccompProfile': {'type': 'RuntimeDefault'},
    }
    container, = pod['containers']
    assert container['image'] == IMAGE
    assert container['command'] == ['sleep', 'infinity']
    assert container['securityContext'] == {
        'runAsNonRoot': True, 'readOnlyRootFilesystem': True, 'privileged': False,
        'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']},
    }
    assert {'name': 'HOME', 'value': '/data'} in container['env']
    assert all(item['name'] != 'CGC_HOME' for item in container['env'])
    mounts = {item['mountPath']: item for item in container['volumeMounts']}
    assert '/data' in mounts and '/tmp' in mounts
    assert 'import codegraphcontext' in container['readinessProbe']['exec']['command'][-1]
    if variant == 'current':
        assert mounts['/data']['subPath'] == 'codegraph'
    else:
        assert '/workspace' in mounts
        assert any(item['name'] == 'GITHUB_TOKEN' for item in container['env'])


@pytest.mark.parametrize('image', ['', 'python:3.13-slim', 'example.test/codegraph:latest',
                                  'example.test/codegraph@sha256:abc',
                                  'example.test/codegraph@sha256:' + 'd' * 64,
                                  'example.test/codegraph@sha256:' + 'f' * 64])
def test_unreviewed_images_refused_including_config_id(receipt, image):
    with pytest.raises((ValueError, TypeError)):
        RENDERER.render(image, receipt, 'fixture', 'fixture')


@pytest.mark.parametrize('change', [{'result': 'incomplete'}, {'fixture': 'other-image'},
                                   {'revision': ''}, {'source_archive_sha256': ''},
                                   {'image_repo_digests': []}, {'image_repo_digests': 'bad'}])
def test_invalid_or_wrong_purpose_evidence_refused(receipt, change):
    value = json.loads(receipt.read_text())
    value.update(change)
    receipt.write_text(json.dumps(value))
    with pytest.raises((ValueError, TypeError)):
        RENDERER.render(IMAGE, receipt, 'fixture', 'fixture')


@pytest.fixture
def deployment_fixture(tmp_path, receipt):
    root = tmp_path / 'module'
    (root / 'scripts').mkdir(parents=True)
    (root / 'manifests').mkdir()
    for name in ['deploy-codegraph.sh', 'render-codegraph.py', '_common.sh']:
        shutil.copyfile(MODULE / 'scripts' / name, root / 'scripts' / name)
    shutil.copyfile(MODULE / 'manifests/codegraph.yaml', root / 'manifests/codegraph.yaml')
    (root / 'config.env').write_text('NAMESPACE=fixture\nSERVICE_ACCOUNT=fixture\n')
    binary = tmp_path / 'bin'
    binary.mkdir()
    kubectl = binary / 'kubectl'
    kubectl.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$FIXTURE_CALLS"
if [ "$1" = apply ]; then cat > "$FIXTURE_APPLIED"; fi
if [ "$1" = get ]; then printf fixture-pod; fi
''')
    kubectl.chmod(0o755)
    env = {'PATH': str(binary) + os.pathsep + os.environ['PATH'], 'HOME': str(tmp_path),
           'CODEGRAPH_IMAGE': IMAGE, 'CODEGRAPH_VALIDATION_RECEIPT': str(receipt),
           'FIXTURE_CALLS': str(tmp_path / 'calls'), 'FIXTURE_APPLIED': str(tmp_path / 'applied')}
    return root / 'scripts/deploy-codegraph.sh', env


@pytest.mark.parametrize('failure', ['image-missing', 'receipt-missing', 'tagged', 'mismatch', 'invalid-json'])
def test_preflight_failure_never_reaches_kubernetes(deployment_fixture, receipt, failure):
    script, env = deployment_fixture
    if failure == 'image-missing':
        del env['CODEGRAPH_IMAGE']
    elif failure == 'receipt-missing':
        del env['CODEGRAPH_VALIDATION_RECEIPT']
    elif failure == 'tagged':
        env['CODEGRAPH_IMAGE'] = 'python:3.13-slim'
    elif failure == 'mismatch':
        env['CODEGRAPH_IMAGE'] = 'example.test/codegraph@sha256:' + 'e' * 64
    else:
        receipt.write_text('{')
    result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert not Path(env['FIXTURE_CALLS']).exists()
    assert not Path(env['FIXTURE_APPLIED']).exists()


def test_render_only_never_reaches_kubernetes(deployment_fixture):
    script, env = deployment_fixture
    result = subprocess.run(['bash', str(script), '--render-only'], env=env, capture_output=True, text=True, check=True)
    assert IMAGE in result.stdout
    assert not Path(env['FIXTURE_CALLS']).exists()


def test_normal_mode_uses_validated_render_and_preserves_cli_checks(deployment_fixture):
    script, env = deployment_fixture
    subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, check=True)
    applied = list(yaml.safe_load_all(Path(env['FIXTURE_APPLIED']).read_text()))
    assert applied[0]['spec']['template']['spec']['containers'][0]['image'] == IMAGE
    calls = Path(env['FIXTURE_CALLS']).read_text()
    assert 'scale deploy/codegraph-context' in calls
    assert 'apply -f -' in calls
    assert '-- cgc --version' in calls
    assert '-- python3 -c import codegraphcontext' in calls


@pytest.mark.parametrize('repository', ['example.test/:', 'example.test/', 'example.test//codegraph',
                                      'example.test/codegraph:', 'example.test:bad/codegraph',
                                      'example..test/codegraph', 'example.test:65536/codegraph'])
def test_malformed_repository_refused_even_with_matching_digest(receipt, repository):
    with pytest.raises(ValueError):
        RENDERER.render(repository + '@sha256:' + DIGEST, receipt, 'fixture', 'fixture')


def test_bare_config_id_in_repository_digest_list_is_not_evidence(receipt):
    value = json.loads(receipt.read_text())
    value['image_repo_digests'] = ['sha256:' + DIGEST]
    receipt.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        RENDERER.render(IMAGE, receipt, 'fixture', 'fixture')


@pytest.mark.parametrize('script', ['validate-codegraph-image.py', 'validate-codegraph-manifests.py'])
def test_acceptance_clis_refuse_optimized_python(script):
    result = subprocess.run([sys.executable, '-O', str(MODULE / 'scripts' / script)],
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert 'do not use Python -O' in result.stderr
