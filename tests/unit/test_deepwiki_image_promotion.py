"""Exercise image-only promotion guards and rollback without a cluster."""
import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('promotion', ROOT / 'modules/agent-context/scripts/promote-deepwiki-image.py')
promotion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promotion)
PREFIX = '123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@'
OLD, NEW = (PREFIX + 'sha256:' + c * 64 for c in ('a', 'b'))


def document(image=OLD):
    return {'metadata': {'resourceVersion': '7', 'generation': 4}, 'status': {'readyReplicas': 1},
            'spec': {'template': {'spec': {'containers': [
                {'name': 'sidecar', 'image': 'untouched'},
                {'name': 'deepwiki', 'image': image, 'env': [{'name': 'KEEP', 'value': 'yes'}]},
            ]}}}}


def test_patch_guards_version_and_image_and_preserves_sidecar():
    before = document()
    patch = promotion.image_patch(before, OLD, NEW)
    assert patch[:2] == [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '7'},
        {'op': 'test', 'path': '/spec/template/spec/containers/1/image', 'value': OLD},
    ]
    assert patch[2] == {'op': 'replace', 'path': '/spec/template/spec/containers/1/image', 'value': NEW}
    assert before == document()
    with pytest.raises(ValueError, match='Live image changed'):
        promotion.image_patch(document(NEW), OLD, NEW)


@pytest.mark.parametrize('failure', [False, True])
def test_promotion_checks_health_and_rolls_back_failure(monkeypatch, tmp_path, failure):
    monkeypatch.chdir(tmp_path)
    for k, v in {'DEEPWIKI_IMAGE': NEW, 'DEEPWIKI_EXPECTED_IMAGE': OLD,
                 'ECR_REGISTRY': PREFIX.split('/')[0], 'ENVIRONMENT': 'dev'}.items():
        monkeypatch.setenv(k, v)
    live = document()
    patches, calls = [], []
    monkeypatch.setattr(promotion, 'deployment', lambda: copy.deepcopy(live))
    def patch(doc, expected, candidate, dry_run=False):
        promotion.image_patch(doc, expected, candidate)
        patches.append((expected, candidate, dry_run))
        if not dry_run:
            live['spec']['template']['spec']['containers'][1]['image'] = candidate
    def run(*args):
        calls.append(args)
        if failure and 'exec' in args:
            raise RuntimeError('health failure')
        return 'passed'
    monkeypatch.setattr(promotion, 'patch', patch)
    monkeypatch.setattr(promotion, 'run', run)
    if failure:
        with pytest.raises(RuntimeError, match='health failure'):
            promotion.main()
        assert patches[-1] == (NEW, OLD, False)
        assert not Path('deepwiki-promotion-receipt.json').exists()
    else:
        promotion.main()
        assert len(patches) == 2
        assert Path('deepwiki-promotion-receipt.json').exists()
    assert any('exec' in c for c in calls)


def test_wrong_repository_is_rejected_before_cluster_access(monkeypatch):
    monkeypatch.setenv('DEEPWIKI_IMAGE', 'other/repo@sha256:' + 'b' * 64)
    monkeypatch.setenv('DEEPWIKI_EXPECTED_IMAGE', OLD)
    monkeypatch.setenv('ECR_REGISTRY', PREFIX.split('/')[0])
    monkeypatch.setenv('ENVIRONMENT', 'dev')
    monkeypatch.setattr(promotion, 'deployment', lambda: pytest.fail('cluster access'))
    with pytest.raises(ValueError, match='exact digests'):
        promotion.main()
