"""Exercise ordered worker promotion without AWS or Kubernetes access."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('rollout', Path(__file__).parents[1] / 'rollout-worker-image.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
OLD, NEW = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64
REGISTRY = '123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime'
REVISION = 'c' * 40


def obj(name, spec=None, **kw):
    return {'metadata': {'name': name, 'resourceVersion': '1'}, 'spec': spec or {}, **kw}


class Fake(m.Rollout):
    def __init__(self):
        super().__init__('123456789012', 'us-east-1', 'dev')
        self.events = []
        self.saved = None
        self.fail_gateway = False
        self.worker = obj('agent-scaledjob', {'jobTargetRef': {'template': {'spec': {'containers': [{'name': 'agent-worker', 'image': REGISTRY + '@' + OLD}]}}}})
        self.cm = obj('adp-worker-authority-config', data={k: OLD for k in m.KEYS})
        self.gateway = obj('bedrockgateway', {'selector': {'matchLabels': {'app': 'gateway'}}, 'template': {'spec': {'containers': [{'name': 'bedrockgateway', 'envFrom': [{'configMapRef': {'name': 'adp-worker-authority-config'}}]}]}}})

    def release(self):
        return self.saved

    def aws(self, *args):
        if args[:2] == ('ecr', 'describe-images'):
            return {'imageDetails': [{'imageDigest': NEW}]}
        if args[:2] == ('ssm', 'get-parameter'):
            return {'Parameter': {'Value': OLD + ',' + NEW, 'Type': 'SecureString'}}
        raise AssertionError(args)

    def get(self, namespace, kind, name):
        return copy.deepcopy({'scaledjob': self.worker, 'configmap': self.cm, 'deployment': self.gateway}.get(kind)) if name not in ('agent-warm-pool', 'agent-image-prepull') else None

    def put(self, name, value, kind='String'):
        self.events.append(('put', name, value))

    def patch(self, namespace, kind, obj, changes):
        self.events.append(('patch', kind, changes))

    def kube(self, namespace, *args):
        self.events.append(('kube', args))
        if args[:2] == ('rollout', 'status') and self.fail_gateway:
            raise RuntimeError('gateway unhealthy')
        if args[:2] == ('get', 'pods'):
            return json.dumps({'items': [{'metadata': {'name': 'gateway-pod'}}]})
        return ''


class WorkerRolloutTests(unittest.TestCase):
    def test_trust_and_gateway_precede_worker_and_retained_pin(self):
        r = Fake()
        r.deploy(REVISION)
        worker_index = next(i for i, e in enumerate(r.events) if e[:2] == ('patch', 'scaledjob'))
        trust_index = next(i for i, e in enumerate(r.events) if e[0] == 'put' and 'authority-worker-images' in e[1])
        verify_index = next(i for i, e in enumerate(r.events) if e[0] == 'kube' and e[1][0] == 'exec')
        self.assertLess(trust_index, verify_index)
        self.assertLess(verify_index, worker_index)
        trust = r.events[trust_index][2].split(',')
        self.assertIn(OLD, trust)
        self.assertIn(NEW, trust)
        changes = r.events[worker_index][2]
        self.assertEqual(changes[0]['value'], REGISTRY + '@' + NEW)
        self.assertEqual(changes[1]['value']['strategy'], 'gradual')
        self.assertEqual(r.events[-1][1], r.release_name)

    def test_failed_gateway_never_switches_workers_or_persists_pin(self):
        r = Fake()
        r.fail_gateway = True
        with self.assertRaisesRegex(RuntimeError, 'gateway unhealthy'):
            r.deploy(REVISION)
        self.assertFalse(any(e[:2] == ('patch', 'scaledjob') for e in r.events))
        self.assertFalse(any(e[:2] == ('put', r.release_name) for e in r.events))

    def test_stale_build_cannot_revert_newer_deployment(self):
        r = Fake()
        r.saved = {'revision': 'd' * 40}
        with patch.object(m.subprocess, 'run') as run:
            run.return_value.returncode = 0
            r.deploy(REVISION)
        self.assertEqual(r.events, [])

    def test_domain_worker_is_not_overwritten(self):
        r = Fake()
        r.worker['spec']['jobTargetRef']['template']['spec']['containers'][0]['image'] = 'example/cyber@' + OLD
        with self.assertRaisesRegex(RuntimeError, 'domain worker'):
            r.deploy(REVISION)
        self.assertEqual(r.events, [])

    def test_terraform_overlay_retains_live_pin_and_trust(self):
        r = Fake()
        self.assertEqual(r.terraform_vars(), {})
        r.saved = {'revision': REVISION, 'image': REGISTRY + '@' + NEW}
        values = r.terraform_vars()
        self.assertEqual(values['agent_image'], r.saved['image'])
        self.assertEqual(values['agent_authority_worker_image_digests'], [OLD, NEW])

    def test_foreign_persisted_pin_is_rejected(self):
        r = Fake()
        r.saved = {'image': 'foreign/repository@' + NEW}
        with self.assertRaisesRegex(RuntimeError, 'target repository'):
            r.terraform_vars()


if __name__ == '__main__':
    unittest.main()
