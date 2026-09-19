"""Live promotion gate. Emit only allowlisted results, never tokens or state."""
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile

from common import *
from artifacts import runtime_config


def require(condition, message):
    if not condition:
        raise ValueError(message)


def kube(kind, name, namespace):
    return json.loads(run(['kubectl', 'get', kind, name, '-n', namespace, '-o', 'json'], capture=True))


def http(url, data=None, headers=None):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def check(directory, environment, upgrade_directory):
    manifest = load(directory)
    account = ACCOUNTS[environment]
    identity(account)
    checks = []
    gateway = kube('deployment', 'bedrockgateway', 'adp-gateway')
    desired = gateway['spec']['replicas']
    require(desired > 0 and gateway['status'].get('observedGeneration', 0) >= gateway['metadata']['generation']
            and gateway['status'].get('updatedReplicas') == gateway['status'].get('availableReplicas') == desired,
            'Gateway rollout is incomplete')
    require(gateway['spec']['template']['spec']['containers'][0]['image'] == image_uri(manifest, 'gateway', account), 'Gateway image differs from release')
    checks.append('gateway_ready_at_release_digest')
    for name, namespace, image in [('agent-scaledjob', 'adp-agents', 'agent-runtime'),
                                   ('agent-gateway-worker', 'adp-gateway-agents', 'agent-gateway'),
                                   ('chat-agent-worker', 'adp-gateway-agents', 'chat-agent')]:
        scaled = kube('scaledjob', name, namespace)
        require(any(c['type'] == 'Ready' and c['status'] == 'True' for c in scaled['status']['conditions']), f'{name} is not Ready')
        require(scaled['spec']['jobTargetRef']['template']['spec']['containers'][0]['image'] == image_uri(manifest, image, account), f'{name} image differs from release')
    for name, namespace, image in [('agent-image-prepull', 'adp-agents', 'agent-runtime'),
                                   ('chat-agent-image-prepull', 'adp-gateway-agents', 'chat-agent')]:
        daemon = kube('daemonset', name, namespace)
        require(daemon['status'].get('numberReady', 0) == daemon['status']['desiredNumberScheduled'] > 0, f'{name} is not Ready')
        require(daemon['spec']['template']['spec']['containers'][0]['image'] == image_uri(manifest, image, account), f'{name} image differs from release')
    config = kube('configmap', 'chat-agent-config', 'adp-gateway-agents')['data']
    require(not any('REPLACE_WITH_' in value for value in config.values()) and config['ADP_BEDROCK_VIA'] == 'gateway', 'Incomplete chat configuration')
    checks.append('required_factory_workers_ready_at_release_digests')
    functions = {name: deployed for name, (_, _, deployed) in LAMBDAS.items()}
    functions.update({'github-auth-broker': 'bedrockgw-dev-github-auth-broker', 'webhook-github': 'adp-dev-github-webhook'})
    # GitLab is optional; validate its package if Terraform says it is installed.
    state = json.loads((upgrade_directory / 'webhook-ingress-after.tfstate').read_text())
    if any(r['type'] == 'aws_lambda_function' and r['name'] == 'gitlab_webhook' and r.get('instances') for r in state['resources']):
        functions['webhook-gitlab'] = 'adp-dev-gitlab-webhook'
    for name, deployed in functions.items():
        function = aws('lambda', 'get-function-configuration', '--function-name', deployed)
        require(function['State'] == 'Active' and function['LastUpdateStatus'] == 'Successful', f'{name} is not Active')
        require(function['CodeSha256'] == code_hash(directory / 'lambda' / f'{name}.zip'), f'{name} code differs from release')
        layer = {'api-authorizer': 'pyjwt-py313', 'pricing-refresh': 'psycopg2-py312', 'budget-usage-tracker': 'psycopg2-py312'}.get(name)
        if layer:
            matches = [x['Arn'] for x in function.get('Layers', []) if layer in x['Arn']]
            require(len(matches) == 1, f'{name} layer missing')
            installed = aws('lambda', 'get-layer-version-by-arn', '--arn', matches[0])
            require(installed['Content']['CodeSha256'] == code_hash(directory / 'layers' / f'{layer}.zip'), f'{name} layer differs from release')
    tick = aws('lambda', 'get-function', '--function-name', 'adp-dev-orchestration-tick')
    require(tick['Configuration']['State'] == 'Active' and tick['Configuration']['LastUpdateStatus'] == 'Successful'
            and tick['Code']['ResolvedImageUri'] == image_uri(manifest, 'gateway', account), 'Orchestration Lambda differs from release')
    checks.append('lambda_code_and_layers_match_release')
    preservation = json.loads((upgrade_directory / 'integration-verification.json').read_text())
    modules = json.loads((upgrade_directory / 'module-verification.json').read_text())
    require(preservation['preserved'] and {'gateway', 'webhook-ingress', 'agent-factory', 'platform'} <= set(modules['verified']), 'Integration preservation or required modules failed')
    checks.append('github_credentials_settings_and_installations_preserved')
    databases = [db['DBInstanceStatus'] for db in aws('rds', 'describe-db-instances')['DBInstances'] if db['DBInstanceIdentifier'].startswith('bedrockgw')]
    require(databases and all(status == 'available' for status in databases), 'Database is not available')

    def parameter(name):
        return aws('ssm', 'get-parameter', '--name', f'/adp/dev/gateway/{name}')['Parameter']['Value']

    base = 'https://' + parameter('cloudfront-domain')
    status, health = http(base + '/api/health')
    require(status == 200 and json.loads(health).get('status') == 'healthy', 'Public API is unhealthy')
    with zipfile.ZipFile(directory / 'frontend.zip') as archive:
        for name in ('index.html', 'cfn-templates/aws_role_v1.yaml', 'cfn-templates/aws_role_v2.yaml'):
            status, body = http(base + '/' + name)
            require(status == 200 and body == archive.read(name), f'Published {name} differs from release')
    status, body = http(base + '/runtime-config.js')
    require(status == 200 and body.startswith(b'window.__ADP_CONFIG__ = '), 'Runtime configuration missing')
    public_config = json.loads(body.decode().removeprefix('window.__ADP_CONFIG__ = ').strip().removesuffix(';'))
    for name, parameter_name in {'VITE_COGNITO_USER_POOL_ID': 'cognito-user-pool-id', 'VITE_COGNITO_CLIENT_ID': 'cognito-client-id',
                                  'VITE_COGNITO_DOMAIN': 'cognito-domain', 'VITE_GITHUB_AUTH_BROKER_URL': 'github-auth-broker-url', 'VITE_AGENT_WS_URL': 'agent-ws-url'}.items():
        require(public_config[name] == parameter(parameter_name), f'Runtime {name} does not match target account')
    require(public_config['VITE_API_URL'] == '/api' and public_config['VITE_COGNITO_REGION'] == REGION, 'Runtime API/region mismatch')
    checks.append('frontend_and_public_api_match_release_and_account')
    status, _ = http(state['outputs']['webhook_url']['value'], b'{}')
    require(status == 401, 'Unsigned webhook was not rejected')
    checks.append('unsigned_webhook_rejected')
    credentials = json.loads(aws('secretsmanager', 'get-secret-value', '--secret-id', 'adp/dev/gateway/test-admin-credentials')['SecretString'])
    with tempfile.NamedTemporaryFile(mode='w') as payload:
        json.dump({'AuthFlow': 'USER_PASSWORD_AUTH', 'ClientId': credentials['cognito_client_id'],
                   'AuthParameters': {'USERNAME': credentials['username'], 'PASSWORD': credentials['password']}}, payload)
        payload.flush()
        auth = aws('cognito-idp', 'initiate-auth', '--cli-input-json', 'file://' + payload.name)
    token = auth['AuthenticationResult']['IdToken']
    status, body = http(base + '/api/auth/me', headers={'Authorization': 'Bearer ' + token})
    user = json.loads(body)
    require(status == 200 and user.get('user_id') and user.get('is_admin') is True, 'Existing administrator cannot sign in')
    run(['node', ROOT / 'platform/scripts/release/websocket-smoke.mjs'], capture=True,
        input=json.dumps({'url': parameter('agent-ws-url'), 'token': token}))
    checks.append('existing_admin_login_and_authenticated_websocket')
    return {'status': 'passed', 'account': account, 'environment': environment, 'source_sha': manifest['source_sha'],
            'release_id': manifest['release_id'], 'manifest_sha256': sha256(directory / 'manifest.json'),
            'frontend_url': base, 'checks': checks}
