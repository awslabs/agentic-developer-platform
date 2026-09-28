"""Refuse mismatched or incomplete measurements before a live rejection probe."""
import importlib.util
import json
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'operator/wave3'))
spec = importlib.util.spec_from_file_location('security_observer_test', ROOT / 'operator/wave3/collect_security.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def inputs():
    config = dict(live_run_id='live', fixture_run_id='live', runtime_run_id='fixture', runtime_revision='a'*40,
                  runtime_generation=2, runtime_pod_uid='pod')
    observation = dict(pod_uid='pod', observed_by='actual local snapshot and listener state',
        observed_at='2026-09-24T18:00:00Z',
        progress=dict(invocation_id='live', run_id='fixture', source_revision='a'*40,
            generation=2, counters_complete=True, dropped_events=0, active_tools=0,
            sdk_queries=1, tool_starts=2),
        state=dict(generation=2, commands=[{'command_id':'pause-command'}]))
    return config, observation

@pytest.mark.parametrize('field,value', [('invocation_id','foreign'),('run_id','live'),('source_revision','b'*40),
    ('generation',3),('counters_complete',False),('dropped_events',1),('active_tools',1),
    ('sdk_queries',True),('tool_starts',-1)])
def test_reject_invalid_runtime_snapshot(field,value):
    config, observed = inputs(); observed['progress'][field] = value
    with pytest.raises(ValueError): m.runtime_counters(config, observed)

def test_counts_actual_journal_and_requires_unique_ids():
    config, observed = inputs()
    counts, ids = m.runtime_counters(config, observed)
    assert counts == dict(accepted_commands=1,sdk_queries=1,tool_starts=2)
    assert ids == {'pause-command'}
    observed['state']['commands'] *= 2
    with pytest.raises(ValueError,match='identities'):m.runtime_counters(config,observed)

@pytest.mark.parametrize('failure_at',[1,2])
def test_measurement_failure_preserves_responses_and_stops_probing(monkeypatch,failure_at):
    config, observed = inputs()
    config.update(gateway_url='https://fixture.invalid',fixture_identity={'run_id':'live'},
        identity_env={role:role for role in ['owner','nonowner','other_tenant']},
        terminal_run_id='terminal',unknown_run_id='unknown')
    for role in config['identity_env']:monkeypatch.setenv(role,'token')
    observations=[];requests=[]
    def observe():
        observations.append(True)
        if len(observations)==failure_at:raise RuntimeError('observation unavailable')
        return observed
    def request(*args):requests.append(args);return 401,{'error':'unauthorized'}
    result=m.collect_gateway(config,request,observe)
    assert len(requests)==failure_at-1
    assert len(result['auth_matrix'])==len(requests)
    assert len(result['observation_errors'])==1
    assert 'token' not in json.dumps(result)

@pytest.mark.parametrize('bad_uid,running', [(False,True),(True,True),(False,False)])
def test_observer_requires_owned_running_paused_worker(bad_uid,running):
    spec=importlib.util.spec_from_file_location('runtime_observer',ROOT/'operator/wave3/observe_runtime.py')
    observer=importlib.util.module_from_spec(spec);spec.loader.exec_module(observer)
    config,observed=inputs()
    config.update(gateway_url='https://fixture.invalid',runtime_progress_path='/tmp/runtime.progress.json',
        kubeconfig='/tmp/kube',runtime_namespace='fixture-ns',runtime_pod_name='worker')
    pod={'metadata':{'uid':'foreign' if bad_uid else 'pod','name':'worker','namespace':'fixture-ns',
         'labels':{'adp.io/w2-fixture':'fixture'}},'status':{'containerStatuses':[{'name':'agent-worker',
         'restartCount':0,'state':{'running':{}} if running else {'terminated':{}}}]}}
    def run(argv):return json.dumps(pod if 'get' in argv else observed['progress'])
    def request(url):return {**observed['state'],'run_id':'live','available':True,'state':'paused','active_tool_count':0}
    if bad_uid or not running:
        with pytest.raises(ValueError):observer.observe(config,run,request)
    else:assert observer.observe(config,run,request)['progress']['tool_starts']==2
