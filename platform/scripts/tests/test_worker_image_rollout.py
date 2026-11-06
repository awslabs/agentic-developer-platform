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
        if args[:2] == ('ssm', 'describe-parameters'):
            return {'Parameters': [{'KeyId': 'existing-key'}]}
        if args[:2] == ('ssm', 'get-parameter'):
            return {'Parameter': {'Value': OLD + ',' + NEW, 'Type': 'SecureString'}}
        raise AssertionError(args)

    def get(self, namespace, kind, name):
        return copy.deepcopy({'scaledjob': self.worker, 'configmap': self.cm, 'deployment': self.gateway}.get(kind)) if name not in ('agent-warm-pool', 'agent-image-prepull') else None

    def put(self, name, value, kind='String', key_id=None):
        if kind == 'SecureString':
            assert key_id == 'existing-key'
        self.events.append(('put', name, value))

    def patch(self, namespace, kind, obj, changes):
        self.events.append(('patch', kind, changes))

    def gateway_writers(self, gateway):
        parent = self

        class Writers:
            trust = None

            def run(self):
                for name in ('gateway-pod', 'chat-pod', 'operator-pod'):
                    parent.events.append(('kube', ('exec', name)))
                    if parent.fail_gateway:
                        raise RuntimeError('gateway unhealthy')

        return Writers()

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
        verify_indices = [i for i, e in enumerate(r.events) if e[0] == 'kube' and e[1][0] == 'exec']
        verify_index = max(verify_indices)
        self.assertEqual(len(verify_indices), 3)
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

    def test_auxiliary_verification_failure_prevents_worker_promotion(self):
        r = Fake()

        class Writers:
            trust = None

            def run(self):
                r.events.append(('verified', 'primary'))
                raise RuntimeError('auxiliary trust mismatch')

        r.gateway_writers = lambda gateway: Writers()
        with self.assertRaisesRegex(RuntimeError, 'auxiliary trust mismatch'):
            r.deploy(REVISION)
        self.assertFalse(any(e[:2] == ('patch', 'scaledjob') for e in r.events))
        self.assertFalse(any(e[:2] == ('put', r.release_name) for e in r.events))

    def test_permission_preflight_failure_never_changes_trust_or_workers(self):
        for error in (ValueError('Release identity lacks list replicasets'), RuntimeError('Unable to check release permission')):
            with self.subTest(error=type(error).__name__):
                r = Fake()
                with patch.object(r, 'gateway_writers', side_effect=error):
                    with self.assertRaises(type(error)):
                        r.deploy(REVISION)
                self.assertEqual(r.events, [])

    def test_worker_workflow_uses_protected_writer_inventory(self):
        workflow = Path(__file__).parents[3] / '.github/workflows/agent-worker-image.yml'
        source = workflow.read_text()
        self.assertIn("ADP_GATEWAY_WRITER_INVENTORY: ${{ vars.ADP_GATEWAY_WRITER_INVENTORY || '[]' }}", source)
        self.assertIn("'modules/gateway/scripts/sync-gateway-writers.py'", source)

    def test_worker_loads_the_canonical_writer_identity_and_trust_implementation(self):
        module = m.writer_module()
        self.assertEqual(module.TRUST_KEYS, m.KEYS)
        self.assertTrue(issubclass(module.WorkerTrust, module.Writers))
        with self.assertRaisesRegex(ValueError, 'Writer binding requires'):
            module.bindings('[{"deployment":"unbound"}]')


if __name__ == '__main__':
    unittest.main()



class ConflictingFake(Fake):
    """The first patch loses the optimistic lock, as a reconciling DaemonSet does."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures
        self.gets = 0
        self.worker['spec']['jobTargetRef']['template']['spec']['containers'][0]['name'] = 'agent-worker'

    def get(self, namespace, kind, name):
        self.gets += 1
        return super().get(namespace, kind, name)

    def patch(self, namespace, kind, obj, changes):
        if self.failures:
            self.failures -= 1
            raise m.subprocess.CalledProcessError(1, ['kubectl', 'patch'])
        super().patch(namespace, kind, obj, changes)


class PatchConflictTests(unittest.TestCase):
    def test_lost_optimistic_lock_is_retried_with_a_fresh_read(self):
        rollout = ConflictingFake(failures=2)
        with patch.object(m.time, 'sleep'):
            rollout.image('scaledjob', 'agent-scaledjob', 'agent-worker', REGISTRY + '@' + NEW)
        self.assertEqual(rollout.gets, 3)
        self.assertEqual([e for e in rollout.events if e[0] == 'patch'][0][1], 'scaledjob')

    def test_persistent_conflict_still_fails(self):
        rollout = ConflictingFake(failures=99)
        with patch.object(m.time, 'sleep'):
            with self.assertRaises(m.subprocess.CalledProcessError):
                rollout.image('scaledjob', 'agent-scaledjob', 'agent-worker', REGISTRY + '@' + NEW)
        self.assertEqual(rollout.gets, m.Rollout.PATCH_ATTEMPTS)


class TrustBoundTests(unittest.TestCase):
    def test_trust_keeps_new_live_and_recent_digests_only(self):
        rollout = Fake()
        stale = ['sha256:' + format(i, '064x') for i in range(1, 150)]
        recent = 'sha256:' + 'd' * 64
        now = m.datetime.now(m.timezone.utc)

        def aws(*args):
            if args[:2] == ('ecr', 'describe-images') and '--image-ids' not in args:
                return {'imageDetails': [
                    {'imageDigest': recent, 'imagePushedAt': now.isoformat()},
                    {'imageDigest': stale[0], 'imagePushedAt': (now - m.timedelta(days=400)).isoformat()},
                ]}
            return Fake.aws(rollout, *args)

        rollout.aws = aws
        trust = rollout.bounded_trust(set(stale) | {recent, OLD, NEW}, NEW, REGISTRY + '@' + OLD)
        self.assertIn(NEW, trust)
        self.assertIn(OLD, trust)  # still running on the ScaledJob
        self.assertIn(recent, trust)
        self.assertNotIn(stale[0], trust)  # pushed outside the retention window
        self.assertLessEqual(len(trust), m.Rollout.TRUST_MAX_ENTRIES)
        self.assertLess(len(','.join(sorted(trust))), 8192)
