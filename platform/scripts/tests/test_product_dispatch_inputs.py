"""Retain dispatch intent across old state, live overrides and CI precedence."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / 'platform/scripts'
sys.path.insert(0, str(SCRIPTS))
import product_dispatch_inputs as dispatch

SPEC = importlib.util.spec_from_file_location('dispatch_upgrade_state', SCRIPTS / 'upgrade-state.py')
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


def tick(value='true', module=dispatch.MODULE):
    return {'mode': 'managed', 'module': module, 'type': 'aws_lambda_function', 'name': 'tick',
            'instances': [{'attributes': {'environment': [{'variables': {dispatch.ENVIRONMENT: value}}]}}]}


class ProductDispatchInputsTests(unittest.TestCase):
    def test_old_saved_output_preserves_exact_owned_tick_in_both_upgrade_modes(self):
        for mode in ('false', 'true'):
            for enabled in (False, True):
                for saved in ({'cost_center': 'engineering'}, {dispatch.INPUT: not enabled}):
                    state = {'outputs': {'release_configuration': {'value': saved}},
                             'resources': [tick(str(enabled).lower())]}
                    with self.subTest(mode=mode, enabled=enabled, saved=saved), \
                            patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG=mode):
                        self.assertIs(upgrade.release_settings(state, 'gateway')[dispatch.INPUT], enabled)

    def test_fresh_and_unrelated_engines_cannot_enable_dispatch(self):
        for state in ({}, {'resources': [tick(module='module.other')]},
                      {'resources': [tick(module='module.orchestration_tick')]}):
            self.assertEqual(dispatch.product_dispatch_settings(state), {})
            self.assertEqual(dispatch.effective_settings(state, None), {dispatch.INPUT: False})

    def test_invalid_and_ambiguous_retained_values_refuse(self):
        for state in ({'resources': [tick(), tick()]}, {'resources': [tick('yes')]},
                      {'resources': [tick(True)]},
                      {'outputs': {'release_configuration': {'value': {dispatch.INPUT: 'true'}}}}):
            with self.subTest(state=state), self.assertRaises(ValueError):
                dispatch.product_dispatch_settings(state)

    def test_live_intent_wins_stale_state_and_protected_explicit_choice_wins_live(self):
        state = {'resources': [tick('false')]}
        self.assertEqual(dispatch.effective_settings(state, True), {dispatch.INPUT: True})
        self.assertEqual(dispatch.effective_settings(state, True, 'false'), {dispatch.INPUT: False})
        self.assertEqual(dispatch.effective_settings(state, False, 'true'), {dispatch.INPUT: True})
        with self.assertRaises(ValueError):
            dispatch.effective_settings(state, True, 'yes')

    def test_live_read_only_treats_explicit_not_found_as_fresh(self):
        for error, tracked, accepted in (('(ResourceNotFoundException)', False, True),
                                         ('(ResourceNotFoundException)', True, False),
                                         ('(AccessDeniedException)', False, False)):
            with self.subTest(error=error, tracked=tracked), patch.object(dispatch.subprocess, 'check_output', side_effect=[
                    '{"Account":"123456789012"}', subprocess.CalledProcessError(254, ['aws'], stderr=error)]):
                if accepted:
                    self.assertIsNone(dispatch.live_setting(account='123456789012', region='us-east-1', environment='dev', tracked=tracked))
                else:
                    with self.assertRaises(subprocess.CalledProcessError):
                        dispatch.live_setting(account='123456789012', region='us-east-1', environment='dev', tracked=tracked)

    def test_workflow_step_reads_live_state_without_leaking_and_overlays_every_plan(self):
        document = {'format_version': '1.0', 'values': {
            'outputs': {'unrelated_secret': {'value': 'must-never-be-printed'}},
            'root_module': {'child_modules': [{'resources': [{
                'address': dispatch.ADDRESS, 'mode': 'managed',
                'values': tick('false')['instances'][0]['attributes']}]}]}}}
        for workflow in ('gateway-infra-plan.yml', 'gateway-infra-apply.yml'):
            source = (ROOT / '.github/workflows' / workflow).read_text()
            start = source.index('      - name: Retain Product dispatch input')
            step = source[start:source.index('\n      - name:', start + 1)]
            self.assertIn('${{ vars.ADP_PRODUCT_STORY_DISPATCH_ENABLED }}', step)
            script = textwrap.dedent(step.split('        run: |\n', 1)[1])
            # Every saved-plan/convergence invocation puts this file after the
            # repository file, so stale tfvars and TF_VAR cannot reset dispatch.
            plans = [line.strip() for line in source.replace('\\\n', '').splitlines()
                     if line.strip().startswith('terraform plan ')]
            self.assertTrue(plans)
            for plan in plans:
                command = plan
                self.assertLess(command.index('${{ env.VAR_FILE }}'), command.index('$ADP_DISPATCH_INPUTS'))
                self.assertLess(command.index('$ADP_NATIVE_INPUTS'), command.index('$ADP_DISPATCH_INPUTS'))
            for requested, expected in (('', True), ('false', False)):
                with self.subTest(workflow=workflow, requested=requested), tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    (directory / 'fixture.json').write_text(json.dumps(document))
                    (directory / 'terraform').write_text('#!/bin/sh\ncat "$RUNNER_TEMP/fixture.json"\n')
                    (directory / 'aws').write_text('''#!/usr/bin/env python3
import json, sys
if sys.argv[1] == 'sts':
    print('123456789012' if '--query' in sys.argv else json.dumps({'Account': '123456789012'}))
else:
    print(json.dumps({'FunctionArn': 'arn:aws:lambda:us-east-1:123456789012:function:adp-dev-orchestration-tick',
        'State': 'Active', 'LastUpdateStatus': 'Successful',
        'Environment': {'Variables': {'ADP_PRODUCT_STORY_DISPATCH_ENABLED': 'true', 'SECRET': 'must-never-be-printed'}}}))
''')
                    for executable in ('terraform', 'aws'):
                        (directory / executable).chmod(0o700)
                    env = dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'], RUNNER_TEMP=tmp,
                               GITHUB_ENV=str(directory / 'github-env'), AWS_REGION='us-east-1', TF_VAR_environment='dev',
                               ADP_PRODUCT_STORY_DISPATCH_ENABLED=requested)
                    result = subprocess.run(['bash', '-c', script], cwd=ROOT / 'modules/gateway/infra',
                                            env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn('must-never-be-printed', result.stdout + result.stderr)
                    retained = directory / 'gateway-dispatch-inputs.tfvars.json'
                    self.assertEqual(json.loads(retained.read_text()), {dispatch.INPUT: expected})
                    self.assertEqual(retained.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
