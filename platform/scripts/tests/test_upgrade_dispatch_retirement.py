"""Global repository retirement retains SSM before gateway/schema cutover."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parents[1]
spec = importlib.util.spec_from_file_location('dispatch_retirement_policy', SCRIPTS / 'upgrade-plan-policy.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
ACCOUNT = '123456789012'
ADDRESS = 'aws_ssm_parameter.orchestration_dispatch_repo'


def fixture(value='disabled', environment='dev', region='us-east-1', account=ACCOUNT):
    name = f'/adp/{environment}/gateway/orchestration-dispatch-repo'
    return {
        'variables': {'environment': {'value': environment}, 'aws_region': {'value': region}},
        'resource_changes': [{
            'address': ADDRESS, 'mode': 'managed', 'type': 'aws_ssm_parameter',
            'provider_name': 'registry.terraform.io/hashicorp/aws',
            'change': {'actions': ['forget'], 'after': None, 'before': {
                'name': name, 'id': name, 'arn': f'arn:aws:ssm:{region}:{account}:parameter{name}',
                'region': region, 'type': 'SecureString', 'value': value,
                'tags': {'Project': 'adp', 'Environment': environment, 'Module': 'gateway',
                         'ManagedBy': 'terraform', 'Owner': 'gateway-team', 'CostCenter': 'engineering'},
            }},
        }],
    }


class DispatchRetirementTests(unittest.TestCase):
    def assert_blocked(self, plan, module='gateway', account=ACCOUNT):
        result = policy.evaluate(plan, module, account)
        self.assertEqual(result['routine'], [])
        self.assertEqual(result['blocked'], [item['address'] for item in plan['resource_changes']])

    def test_existing_values_survive_each_gateway_pass_without_runtime_or_binding_clearance(self):
        # A first pass has no new schema/image or binding-migration receipt yet.
        # ['forget'] leaves SSM intact even when the old gateway uses the value.
        for value in ('disabled', 'example/retained-project'):
            for module in ('gateway', 'gateway-alb-wire', 'gateway-final'):
                for environment, region, account in (('dev', 'us-east-1', ACCOUNT),
                                                     ('staging', 'eu-west-1', '999999999999'),
                                                     ('prod', 'ap-southeast-2', ACCOUNT)):
                    with self.subTest(value=value, module=module, environment=environment):
                        plan = fixture(value, environment, region, account)
                        original = copy.deepcopy(plan)
                        self.assertEqual(policy.evaluate(plan, module, account), {
                            'routine': [ADDRESS], 'blocked': [], 'protected': [],
                        })
                        self.assertEqual(plan, original)

    def test_value_is_never_inspected_to_authorize_retention(self):
        class UnreadableValue:
            def __eq__(self, other):
                raise AssertionError('the value is not migration authority')

        plan = fixture(UnreadableValue())
        self.assertEqual(policy.evaluate(plan, 'gateway', ACCOUNT)['routine'], [ADDRESS])

    def test_remote_deletion_and_replacement_are_never_retirement(self):
        for value in ('disabled', 'example/retained-project'):
            for actions in (['delete'], ['delete', 'create'], ['create', 'delete']):
                with self.subTest(value=value, actions=actions):
                    plan = fixture(value)
                    plan['resource_changes'][0]['change']['actions'] = actions
                    self.assert_blocked(plan)
        plan = fixture()
        plan['resource_changes'][0]['change']['after'] = {}
        self.assert_blocked(plan)

    def test_wrong_resource_or_provenance_cannot_be_forgotten(self):
        for field, value in (
            ('address', 'aws_ssm_parameter.other'),
            ('address', ADDRESS + '[0]'),
            ('mode', 'data'), ('type', 'aws_ssm_parameter_other'),
            ('provider_name', 'registry.terraform.io/other/aws'),
            ('module_address', 'module.other'), ('deposed', 'old-instance'),
        ):
            with self.subTest(field=field, value=value):
                plan = fixture()
                plan['resource_changes'][0][field] = value
                self.assert_blocked(plan)
        for field, value in (
            ('name', '/adp/dev/gateway/other'), ('id', '/adp/dev/gateway/other'),
            ('arn', f'arn:aws:ssm:us-east-1:{ACCOUNT}:parameter/adp/dev/gateway/other'),
            ('arn', f'arn:aws:ssm:us-east-1:{ACCOUNT}:parameter/adp/prod/gateway/orchestration-dispatch-repo'),
            ('arn', f'arn:aws:ssm:eu-west-1:{ACCOUNT}:parameter/adp/dev/gateway/orchestration-dispatch-repo'),
            ('arn', 'arn:aws:ssm:us-east-1:999999999999:parameter/adp/dev/gateway/orchestration-dispatch-repo'),
            ('region', 'eu-west-1'), ('type', 'String'), ('tags', {}), ('tags', ['invalid']),
        ):
            with self.subTest(field=field, value=value):
                plan = fixture()
                plan['resource_changes'][0]['change']['before'][field] = value
                self.assert_blocked(plan)
        for tag in ('Project', 'Environment', 'Module', 'ManagedBy', 'Owner'):
            with self.subTest(tag=tag):
                plan = fixture()
                plan['resource_changes'][0]['change']['before']['tags'][tag] = 'other'
                self.assert_blocked(plan)
        plan = fixture()
        plan['resource_changes'] *= 2
        self.assert_blocked(plan)

    def test_gate_is_tied_to_supported_gateway_target(self):
        for module in ('platform', 'webhook-ingress', 'agent-factory', 'gateway-other'):
            with self.subTest(module=module):
                self.assert_blocked(fixture(), module)
        for account in ('', '123', '999999999999'):
            with self.subTest(account=account):
                self.assert_blocked(fixture(), account=account)
        for key, value in (('environment', ''), ('environment', '../dev'), ('environment', 'prod'),
                           ('aws_region', ''), ('aws_region', '../us-east-1'), ('aws_region', 'eu-west-1')):
            with self.subTest(key=key, value=value):
                plan = fixture()
                plan['variables'][key]['value'] = value
                self.assert_blocked(plan)
        plan = fixture()
        plan.pop('variables')
        self.assert_blocked(plan)

    def test_older_provider_state_and_operator_metadata_are_preserved(self):
        plan = fixture('example/retained-project')
        before = plan['resource_changes'][0]['change']['before']
        before.pop('region')  # Older provider versions omit this computed field.
        before['tags'].update(CostCenter='customer-cost-center', OperatorLabel='preserve')
        self.assertEqual(policy.evaluate(plan, 'gateway', ACCOUNT)['routine'], [ADDRESS])

    def test_unrelated_parameter_and_key_deletions_still_close_gate(self):
        plan = fixture()
        for address, kind in (('aws_ssm_parameter.other', 'aws_ssm_parameter'), ('aws_kms_key.secrets', 'aws_kms_key')):
            plan['resource_changes'].append({
                'address': address, 'type': kind,
                'change': {'actions': ['delete'], 'before': {'id': 'existing'}, 'after': None},
            })
        result = policy.evaluate(plan, 'gateway', ACCOUNT)
        self.assertEqual(result['routine'], [ADDRESS])
        self.assertEqual(result['blocked'], ['aws_ssm_parameter.other', 'aws_kms_key.secrets'])

    def test_terraform_declares_only_remote_retention(self):
        source = (ROOT / 'modules/gateway/infra/retired-dispatch-repository.tf').read_text()
        declaration = re.sub(r'#[^\n]*', '', source)
        self.assertEqual(' '.join(declaration.split()),
                         'removed { from = ' + ADDRESS + ' lifecycle { destroy = false } }')

    def test_shell_gate_applies_saved_forget_plan_without_destructive_flag(self):
        for scenario in ('disabled', 'populated', 'delete', 'foreign', 'unrelated', 'fresh', 'retry'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan = fixture('private-example/retained-project' if scenario == 'populated' else 'disabled')
                if scenario == 'delete':
                    plan['resource_changes'][0]['change']['actions'] = ['delete']
                if scenario == 'foreign':
                    plan['variables']['environment']['value'] = 'prod'
                if scenario == 'unrelated':
                    other = copy.deepcopy(plan['resource_changes'][0])
                    other['address'] = 'aws_ssm_parameter.other'
                    other['change']['actions'] = ['delete']
                    plan['resource_changes'].append(other)
                if scenario in ('fresh', 'retry'):
                    plan['resource_changes'] = []
                (root / 'plan.json').write_text(json.dumps(plan))
                terraform = root / 'terraform'
                terraform.write_text('''#!/bin/bash
echo "$*" >> "$REVIEW_ROOT/terraform.calls"
case "$1" in
  plan) exit 2;;
  show) cat "$REVIEW_ROOT/plan.json";;
  apply) exit 0;;
esac
''')
                terraform.chmod(0o755)
                env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", REVIEW_ROOT=directory,
                           ACCOUNT_ID=ACCOUNT, TMPDIR=directory, CONFIRM_DESTRUCTIVE='false')
                for key in ('UPGRADE_RUN_DIR', 'UPGRADE_CHECK_ONLY', 'ADP_CHAT_RETIREMENT_DIR', 'ADP_WORKER_MIGRATION_EVIDENCE'):
                    env.pop(key, None)
                command = '''set -euo pipefail
ok() { :; }
fail() { echo "$*" >&2; exit 1; }
source "$1"
terraform_update_apply gateway /dev/null
'''
                result = subprocess.run(['bash', '-c', command, 'test', str(SCRIPTS / 'terraform-update.sh')],
                                        env=env, text=True, capture_output=True)
                allowed = scenario in ('disabled', 'populated', 'fresh', 'retry')
                self.assertEqual(result.returncode == 0, allowed, result.stdout + result.stderr)
                calls = (root / 'terraform.calls').read_text().splitlines()
                self.assertIn('-var orchestration_tick_upgrade_hold=true', calls[0])
                self.assertEqual(any(c.startswith('apply ') for c in calls), allowed)
                if allowed:
                    saved_plan = next(arg[5:] for arg in calls[0].split() if arg.startswith('-out='))
                    self.assertEqual(calls[-1], 'apply ' + saved_plan)
                self.assertNotIn('private-example/retained-project', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
