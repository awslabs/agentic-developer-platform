import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'modules/agent-context/scripts/{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


promotion = load('promote-ingestion-image')
resolver = load('resolve-auto-ingestion-image')
PREFIX = '123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-ingestion@'
OLD, NEW = (PREFIX + 'sha256:' + c * 64 for c in ('a', 'b'))


def document(target, image=OLD):
    kind, name, path, container = target
    spec = {'template': {'spec': {'containers': [{'name': container, 'image': image, 'env': [{'name': 'KEEP', 'value': 'yes'}]}]}}}
    return {'kind': 'ScaledJob' if kind == 'scaledjob' else 'CronJob', 'metadata': {'resourceVersion': '7'},
            'spec': {'jobTargetRef': spec} if kind == 'scaledjob' else {'schedule': '0 0 * * *', 'jobTemplate': {'spec': spec}}}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for key, value in {'INGESTION_IMAGE': NEW, 'INGESTION_EXPECTED_IMAGE': OLD, 'ECR_REGISTRY': PREFIX.split('/')[0],
                       'ENVIRONMENT': 'dev', 'AWS_REGION': 'us-east-1', 'TRIGGER_SOURCE_SHA': 'c' * 40}.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize('failure', [None, 'preflight', 'second-write', 'ambiguous-write'])
def test_all_targets_preflight_and_partial_failure_rollback(monkeypatch, tmp_path, failure):
    monkeypatch.chdir(tmp_path)
    live = {t[1]: document(t) for t in promotion.TARGETS}
    calls = []
    monkeypatch.setattr(promotion, 'get', lambda kind, name: copy.deepcopy(live[name]))
    def apply(t, d, expected, candidate, dry_run=False):
        patch = promotion.image_patch(d, t[2], t[3], expected, candidate)
        assert patch[0]['path'] == '/metadata/resourceVersion'
        calls.append((t[1], dry_run, candidate))
        if failure == 'preflight' and t[1] == 'vuln-scan':
            raise RuntimeError('preflight')
        if failure == 'second-write' and not dry_run and t[1] == 'vuln-scan' and candidate == NEW:
            raise RuntimeError('second-write')
        if not dry_run:
            promotion.value_at(live[t[1]], t[2].rsplit('/', 1)[0])['image'] = candidate
            if t[0] == 'scaledjob':
                live[t[1]]['spec']['rollout'] = {'strategy': 'gradual'}
            if failure == 'ambiguous-write' and t[1] == 'vuln-scan' and candidate == NEW:
                raise RuntimeError('ambiguous-write')
    monkeypatch.setattr(promotion, 'apply', apply)
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            promotion.main()
        assert all(promotion.value_at(live[t[1]], t[2]) == OLD for t in promotion.TARGETS)
        assert not Path('ingestion-promotion-receipt.json').exists()
    else:
        promotion.main()
        assert all(c[1] for c in calls[:3])
        assert live['ingestion-worker']['spec']['rollout']['strategy'] == 'gradual'
        assert Path('ingestion-promotion-receipt.json').exists()


def test_stale_image_or_container_fails():
    t = promotion.TARGETS[0]
    with pytest.raises(ValueError):
        promotion.image_patch(document(t, NEW), t[2], t[3], OLD, NEW)
    with pytest.raises(ValueError):
        promotion.image_patch(document(t), t[2], 'other', OLD, NEW)


@pytest.mark.parametrize('mode', ['built', 'unbuilt', 'denied', 'mixed', 'wrong-repo'])
def test_auto_resolution_never_selects_latest(mode):
    def run(args, **kwargs):
        assert 'sort_by' not in str(args)
        if args[0] == 'aws':
            assert 'imageTag=' + 'c' * 40 in args
            return SimpleNamespace(returncode=0 if mode == 'built' else 1, stdout=NEW.split('@')[1],
                                   stderr='AccessDeniedException' if mode == 'denied' else 'ImageNotFoundException')
        target = next(t for t in promotion.TARGETS if t[1] == args[5])
        image = NEW if mode == 'mixed' and target[1] == 'vuln-scan' else OLD
        if mode == 'wrong-repo':
            image = 'other/repo@sha256:' + 'a' * 64
        return SimpleNamespace(returncode=0, stdout=json.dumps(document(target, image)))
    if mode in ['denied', 'mixed', 'wrong-repo']:
        with pytest.raises((ValueError, RuntimeError)):
            resolver.resolve(run)
    else:
        assert resolver.resolve(run) == (NEW if mode == 'built' else OLD)


def test_workflow_has_exclusive_guarded_promotion_and_auto_resolution():
    text = (ROOT / '.github/workflows/agent-context-deploy.yml').read_text()
    assert 'inputs.ingestion_only == true && inputs.deepwiki_only != true' in text
    assert 'inputs.deepwiki_only != true && inputs.ingestion_only != true' in text
    assert text.index('resolve-auto-ingestion-image.py') < text.index('Run DB migrations')
    assert 'MIGRATION_IMAGE="$ADP_RESOLVED_INGESTION_IMAGE"' in text
