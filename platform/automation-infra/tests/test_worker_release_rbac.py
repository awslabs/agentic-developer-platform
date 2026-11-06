"""Effective worker permissions cover discovery but constrain all mutations."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
DOCUMENTS = list(yaml.safe_load_all((ROOT / 'platform/automation-infra/worker-release-rbac.yaml').read_text()))


def role(namespace):
    return next(doc for doc in DOCUMENTS if doc['kind'] == 'Role' and doc['metadata']['namespace'] == namespace)


def allows(namespace, verb, group, resource, name=None):
    return any(
        verb in rule['verbs'] and group in rule['apiGroups'] and resource in rule['resources']
        and ('resourceNames' not in rule or name in rule['resourceNames'])
        for rule in role(namespace)['rules']
    )


def test_gateway_discovery_can_find_unenrolled_writers_without_mutating_them():
    for group, resource in [('apps', 'deployments'), ('apps', 'replicasets'), ('', 'pods')]:
        for verb in ('get', 'list'):
            assert allows('adp-gateway', verb, group, resource)
        for verb in ('patch', 'update', 'create', 'delete', 'deletecollection'):
            assert not allows('adp-gateway', verb, group, resource)
            assert not allows('adp-gateway', verb, group, resource, 'unenrolled-writer')
    for name in ('bedrockgateway', 'chat-sandbox-gateway'):
        assert allows('adp-gateway', 'patch', 'apps', 'deployments', name)
    assert not allows('adp-gateway', 'patch', 'apps', 'deployments', 'operator-gateway')


def test_no_exec_secret_rbac_or_other_workload_permission_expansion():
    gateway = role('adp-gateway')['rules']
    assert [rule for rule in gateway if 'pods/exec' in rule['resources']] == [
        {'apiGroups': [''], 'resources': ['pods/exec'], 'verbs': ['get', 'create']}
    ]
    assert [rule for rule in gateway if 'configmaps' in rule['resources']] == [
        {'apiGroups': [''], 'resources': ['configmaps'], 'resourceNames': ['adp-worker-authority-config'], 'verbs': ['get', 'patch']}
    ]
    assert {resource for rule in gateway for resource in rule['resources']} == {
        'deployments', 'replicasets', 'pods', 'pods/exec', 'configmaps'
    }
    assert all('*' not in values for rule in gateway for values in rule.values())
    assert role('adp-agents')['rules'] == [
        {'apiGroups': ['keda.sh'], 'resources': ['scaledjobs'], 'resourceNames': ['agent-scaledjob'], 'verbs': ['get', 'patch']},
        {'apiGroups': ['apps'], 'resources': ['deployments'], 'resourceNames': ['agent-warm-pool'], 'verbs': ['get', 'patch']},
        {'apiGroups': ['apps'], 'resources': ['daemonsets'], 'resourceNames': ['agent-image-prepull'], 'verbs': ['get', 'patch']},
    ]
    assert len(DOCUMENTS) == 4
    assert all(doc['kind'] in ('Role', 'RoleBinding') for doc in DOCUMENTS)
    for binding in (doc for doc in DOCUMENTS if doc['kind'] == 'RoleBinding'):
        assert binding['subjects'] == [{'kind': 'Group', 'name': 'adp:worker-release'}]
        assert binding['roleRef'] == {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'adp-worker-release'}


def test_documented_supplement_grants_only_explicit_named_deployment_patch():
    source = (ROOT / 'platform/automation-infra/README.md').read_text()
    section = source.split('For each additional writer in the protected', 1)[1].split('Successful rollout records', 1)[0]
    documents = list(yaml.safe_load_all(section.split('```yaml\n', 1)[1].split('```', 1)[0]))
    assert len(documents) == 2
    grant, binding = documents
    assert grant['kind'] == 'Role' and grant['metadata']['namespace'] == 'adp-gateway'
    assert grant['rules'] == [{'apiGroups': ['apps'], 'resources': ['deployments'], 'resourceNames': ['operator-gateway'], 'verbs': ['patch']}]
    assert binding['kind'] == 'RoleBinding' and binding['metadata']['namespace'] == 'adp-gateway'
    assert binding['subjects'] == [{'kind': 'Group', 'name': 'adp:worker-release'}]
    assert binding['roleRef'] == {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': grant['metadata']['name']}
