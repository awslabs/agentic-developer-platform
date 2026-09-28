"""Rendered fixture configuration must satisfy the real control transport guard."""
from pathlib import Path
import pytest
from conftest import live_deployment
from render_fixture import render_gateway, RenderError
from test_reviewed_images import GATEWAY


def render(live, cidrs=None):
    return render_gateway(live, run_id='w2-cidrs', nonce='deadbeefcafe0123', name='w2-cidrs',
                          namespace='adp-gateway', image=GATEWAY, queue_url='https://example/fixture.fifo',
                          cluster_pod_cidrs=cidrs)[0]


def without_inline():
    live = live_deployment()
    env = live['spec']['template']['spec']['containers'][0]['env']
    env[:] = [entry for entry in env if entry['name'] != 'AGENT_CONTROL_CLUSTER_POD_CIDRS']
    return live


def test_envfrom_alone_cannot_prove_cidrs_exist():
    with pytest.raises(RenderError, match='supply --cluster-pod-cidrs'):
        render(without_inline())


@pytest.mark.parametrize('cidrs', ['', '0.0.0.0/0', '127.0.0.0/8', '169.254.0.0/16', '8.8.8.8/32', '10.0.0.1/16', '10.0.0.0/16,', '::/0'])
def test_invalid_or_nonprivate_ranges_refused(cidrs):
    with pytest.raises(RenderError):
        render(without_inline(), cidrs)


def test_explicit_target_overrides_only_fixture():
    live = without_inline()
    rendered = render(live, '10.0.11.152/32')
    env = {entry['name']: entry.get('value') for entry in rendered['spec']['template']['spec']['containers'][0]['env']}
    assert env['AGENT_CONTROL_CLUSTER_POD_CIDRS'] == '10.0.11.152/32'
    assert not any(entry['name']=='AGENT_CONTROL_CLUSTER_POD_CIDRS' for entry in live['spec']['template']['spec']['containers'][0]['env'])
