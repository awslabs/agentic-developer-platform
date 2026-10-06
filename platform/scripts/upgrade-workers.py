#!/usr/bin/env python3
"""Worker cutover stages owned by deploy.sh --update.

The input is a reviewed live qualification record, not a canary runner. Never
manufacture this record from Terraform assertions or unit-test results.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('worker_upgrade_state', Path(__file__).with_name('upgrade-state.py'))
state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state)
PAUSE = 'autoscaling.keda.sh/paused'
CHECKS = {
    'coding', 'github_renewal_over_one_hour', 'cancellation', 'tool_tokens',
    'logs', 'marker_and_door', 'vault_raw_proxy_file', 'customer_role_refresh_chaining',
    'victim_substitution_denied', 'all_cluster_source_isolation',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE)


def aws(region, *args):
    return json.loads(command('aws', *args, '--region', region, '--output', 'json'))


def kube(*args, kubeconfig=None):
    return json.loads(command('kubectl', *(['--kubeconfig', str(kubeconfig)] if kubeconfig else []),
                              *args, '--request-timeout=30s', '-o', 'json'))


def scaledjob():
    return kube('get', 'scaledjob', 'agent-scaledjob', '-n', 'adp-agents')


def qualification(path, account, region, environment):
    path = Path(path).resolve()
    record = json.loads(path.read_text())
    require(record.get('schema') == 1, 'Unsupported worker qualification record')
    require((record.get('account'), record.get('region'), record.get('environment')) ==
            (account, region, environment), 'Worker qualification belongs to another target')
    require(record.get('source_sha') == command('git', '-C', str(ROOT), 'rev-parse', 'HEAD').strip(),
            'Worker qualification does not cover this source')
    require(isinstance(record.get('reviewed_by'), str) and record['reviewed_by'].strip(),
            'Worker qualification requires an attributed review')
    require(set(record.get('checks', {})) == CHECKS and
            all(value == 'passed' for value in record['checks'].values()),
            'Missing live worker compatibility or isolation qualification')
    # Keep the real report next to the record, including fixture identities,
    # timestamps, run IDs and the all-cluster/implicit-creator inventory.
    report = record.get('report', {})
    report_path = (path.parent / report.get('file', '')).resolve()
    require(report_path.parent == path.parent and report_path != path,
            'Qualification report must be a separate adjacent file')
    require(report_path.is_file() and report_path.stat().st_size > 0 and
            hashlib.sha256(report_path.read_bytes()).hexdigest() == report.get('sha256'),
            'Worker qualification report is missing or changed')
    for key, repository in [('gateway_image', 'adp-gateway'), ('worker_image', 'adp-agent-runtime')]:
        image = record.get(key, '')
        require(re.fullmatch(rf'{account}\.dkr\.ecr\.{re.escape(region)}\.amazonaws\.com/{repository}@sha256:[0-9a-f]{{64}}', image),
                f'Qualification {key} must pin a target-account digest')
    for variable, key in [('ADP_RELEASE_GATEWAY_IMAGE', 'gateway_image'),
                          ('ADP_RELEASE_AGENT_RUNTIME_IMAGE', 'worker_image')]:
        require(not os.environ.get(variable) or os.environ[variable] == record[key],
                'Worker qualification differs from selected release artifacts')
    return record


def read_record(directory):
    return json.loads((directory / 'worker-migration.json').read_text())


def save(directory, record):
    state.write_json(directory / 'worker-migration.json', record)


def configure(directory, module, values):
    path = directory / (module + '.tfvars.json')
    current = json.loads(path.read_text())
    current.update(values)
    state.write_json(path, current)


def validate(directory, evidence, account, region, environment):
    proof = qualification(evidence, account, region, environment)
    identity = aws(region, 'sts', 'get-caller-identity')
    require(identity['Account'] == account, 'Worker migration AWS account mismatch')
    require((directory / 'webhook-ingress-preflight.tfstate').exists(), 'Migration requires an installed webhook stack')
    for key in ('gateway_image', 'worker_image'):
        repository = proof[key].split('/', 1)[1].split('@')[0]
        digest = proof[key].split('@')[1]
        images = aws(region, 'ecr', 'describe-images', '--repository-name', repository,
                     '--image-ids', 'imageDigest=' + digest)['imageDetails']
        require(len(images) == 1 and images[0]['imageDigest'] == digest, 'Qualified runtime image is unavailable')
    previous = read_record(directory) if (directory / 'worker-migration.json').exists() else None
    immutable = {key: proof[key] for key in ('account', 'region', 'environment', 'source_sha',
                                             'gateway_image', 'worker_image')}
    if previous:
        require(all(previous.get(k) == v for k, v in immutable.items()),
                'Cannot resume a worker migration with different target or images')
    else:
        previous = dict(immutable, phase='validated')
    previous['qualification_sha256'] = hashlib.sha256(Path(evidence).read_bytes()).hexdigest()
    previous['clusters'] = proof.get('clusters')
    require(isinstance(previous['clusters'], list) and all(isinstance(c, str) for c in previous['clusters']),
            'Qualification requires the reviewed all-region cluster ARN inventory')
    save(directory, previous)


def check_queue(record):
    url = aws(record['region'], 'sqs', 'get-queue-url', '--queue-name',
              f"adp-{record['environment']}-agent-submit.fifo")['QueueUrl']
    counts = aws(record['region'], 'sqs', 'get-queue-attributes', '--queue-url', url,
                 '--attribute-names', 'ApproximateNumberOfMessages',
                 'ApproximateNumberOfMessagesNotVisible', 'ApproximateNumberOfMessagesDelayed')['Attributes']
    require(len(counts) == 3 and all(int(value) == 0 for value in counts.values()),
            'Legacy queue still contains work; keep it intact and reconcile before retrying')


def check_drained():
    # Check the whole namespace so unlabeled legacy Jobs cannot escape the gate.
    jobs = kube('get', 'jobs', '-n', 'adp-agents')['items']
    require(all(any(c.get('type') in ('Complete', 'Failed') and c.get('status') == 'True'
                    for c in job.get('status', {}).get('conditions', [])) for job in jobs),
            'Legacy worker Jobs have not finished; no Jobs were deleted')
    pods = kube('get', 'pods', '-n', 'adp-agents')['items']
    require(not any(p.get('status', {}).get('phase') not in ('Succeeded', 'Failed') and
                    p.get('spec', {}).get('serviceAccountName') == 'agent-scaledjob-sa'
                    for p in pods), 'Legacy worker pods are still running')


def check_source_isolation(record):
    # The reviewed report covers every platform cluster and implicit creator.
    # Recheck the inventory and visible grants before source sessions activate.
    # The report must also identify historical/implicit creator access, which
    # list-access-entries and aws-auth alone cannot establish.
    import yaml
    region, environment, account = (record[k] for k in ('region', 'environment', 'account'))
    arn = f'arn:aws:iam::{account}:role/adp-{environment}-agent-scaledjob-role'
    clusters = []
    for item in aws(region, 'ec2', 'describe-regions')['Regions']:
        location = item['RegionName']
        for cluster in aws(location, 'eks', 'list-clusters')['clusters']:
            clusters.append((location, cluster, f'arn:aws:eks:{location}:{account}:cluster/{cluster}'))
    require(sorted(c[2] for c in clusters) == sorted(record['clusters']) and
            f'arn:aws:eks:{region}:{account}:cluster/adp-{environment}-eks-cluster' in record['clusters'],
            'Cluster inventory differs from reviewed source-isolation evidence')
    for location, cluster, cluster_arn in clusters:
        description = aws(location, 'eks', 'describe-cluster', '--name', cluster)['cluster']
        require(description.get('arn') == cluster_arn, 'Source isolation check reached another cluster')
        mode = description['accessConfig']['authenticationMode']
        require(mode in ('API', 'API_AND_CONFIG_MAP'), 'CONFIG_MAP-only cluster needs a reviewed access migration')
        require(arn not in aws(location, 'eks', 'list-access-entries', '--cluster-name', cluster)['accessEntries'],
                'Legacy source still has an EKS access entry; reconcile its owning state before migration')
        if mode == 'API_AND_CONFIG_MAP':
            with tempfile.TemporaryDirectory(prefix='adp-source-isolation-') as temporary:
                config = Path(temporary) / 'kubeconfig'
                command('aws', 'eks', 'update-kubeconfig', '--name', cluster, '--region', location,
                        '--kubeconfig', str(config))
                maps = kube('get', 'configmap', 'aws-auth', '-n', 'kube-system', kubeconfig=config)['data']
            roles = yaml.safe_load(maps.get('mapRoles', '[]')) or []
            accounts = yaml.safe_load(maps.get('mapAccounts', '[]')) or []
            require(not any(row.get('rolearn') == arn for row in roles) and account not in {str(x) for x in accounts},
                    'Legacy source still has aws-auth access; no mappings were removed')


def pause(directory):
    record = read_record(directory)
    job = scaledjob()
    annotations = job['metadata'].get('annotations', {})
    if 'original_paused' not in record:
        record['original_paused'] = any(k.startswith('autoscaling.keda.sh/paused') for k in annotations)
        record['scaledjob_uid'] = job['metadata']['uid']
        save(directory, record)  # Persist before mutation so an interrupted call is resumable.
    require(job['metadata']['uid'] == record['scaledjob_uid'], 'Worker ScaledJob was replaced outside this upgrade')
    check_source_isolation(record)
    check_queue(record)  # Never strand an existing queue by pausing its consumers.
    subprocess.run(['python3', str(ROOT / 'modules/gateway/scripts/sync-gateway-engine.py'),
                    '--quiesce', '--account', record['account'], '--region', record['region'],
                    '--environment', record['environment']], check=True)
    record['pause_started'] = True
    save(directory, record)
    patch = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': job['metadata']['resourceVersion']},
             {'op': 'add', 'path': '/metadata/annotations', 'value': dict(annotations, **{PAUSE: 'true'})}]
    kube('patch', 'scaledjob', 'agent-scaledjob', '-n', 'adp-agents', '--type=json', '-p', json.dumps(patch))
    deadline = time.monotonic() + 600
    empty_since = None
    while True:
        current = scaledjob()
        require(current['metadata']['uid'] == record['scaledjob_uid'], 'Worker ScaledJob changed during drain')
        paused = any(c.get('type') == 'Paused' and c.get('status') == 'True'
                     for c in current.get('status', {}).get('conditions', []))
        problem = None
        try:
            require(paused, 'Waiting for KEDA to acknowledge pause')
            check_drained()
            check_queue(record)
        except ValueError as error:
            problem = str(error)
            empty_since = None
        now = time.monotonic()
        if problem is None:
            empty_since = now if empty_since is None else empty_since
            # SQS counts are approximate. Require a quiet minute after KEDA's
            # pause and the last active Job, not one potentially stale zero.
            if now - empty_since >= 60:
                break
            problem = 'Waiting for stable empty queue and drained workers'
        require(now < deadline, f'{problem}; admission remains paused. Retry the same upgrade after reconciliation')
        print(problem, flush=True)
        time.sleep(10)
    record['phase'] = 'drained'
    save(directory, record)
    configure(directory, 'webhook-ingress', {
        'agent_authority_prepared': True, 'agent_authority_enabled': True,
        'agent_authority_runtime_ready': True, 'agent_authority_legacy_workers_drained': True,
        'agent_task_source_isolation_confirmed': True, 'agent_legacy_worker_admin_retired': True,
        'agent_worker_admission_paused': True, 'agent_image': record['worker_image'],
        'agent_authority_worker_image_digests': [record['worker_image'].split('@')[1]],
    })
    # Keep the old gateway/tick parity through the first gateway rollout. After
    # webhook activates its ConfigMap, tick() enables the matching Lambda input.
    config = kube('get', 'configmap', 'adp-worker-authority-config', '-n', 'adp-gateway')['data']
    configure(directory, 'gateway', {'orchestration_agent_authority_enabled':
                                     config.get('AGENT_AUTHORITY_ENABLED') == 'true'})
    configure(directory, 'platform', {'agent_authority_legacy_workers_drained': True,
                                      'agent_legacy_worker_admin_retired': True})


def tick(directory):
    record = read_record(directory)
    require(record['phase'] in ('drained', 'configured', 'verified', 'admitted'), 'Workers must be drained first')
    require(scaledjob()['metadata'].get('annotations', {}).get(PAUSE) == 'true', 'Admission lost its migration pause')
    configure(directory, 'gateway', {'orchestration_agent_authority_enabled': True})
    record['phase'] = 'configured'
    save(directory, record)


def verify(directory):
    record = read_record(directory)
    check_source_isolation(record)
    check_drained()
    check_queue(record)
    job = scaledjob()
    spec = job['spec']['jobTargetRef']['template']['spec']
    require(job['metadata'].get('annotations', {}).get(PAUSE) == 'true', 'Worker admission must remain paused')
    require(job['metadata']['uid'] == record['scaledjob_uid'], 'Worker ScaledJob changed outside this migration')
    require(spec['serviceAccountName'] == 'agent-authority-worker-sa', 'Protected worker service account is not selected')
    require(next(c['image'] for c in spec['containers'] if c['name'] == 'agent-worker') == record['worker_image'],
            'Worker template differs from qualified image')
    role = f"adp-{record['environment']}-agent-scaledjob-role"
    iam = aws(record['region'], 'iam', 'get-role', '--role-name', role)['Role']
    boundary = f"arn:aws:iam::{record['account']}:policy/adp-{record['environment']}-agent-task-source-boundary"
    require(iam.get('PermissionsBoundary', {}).get('PermissionsBoundaryArn') == boundary,
            'Legacy customer-source role is missing its restricted boundary')
    version = aws(record['region'], 'iam', 'get-policy', '--policy-arn', boundary)['Policy']['DefaultVersionId']
    document = aws(record['region'], 'iam', 'get-policy-version', '--policy-arn', boundary,
                   '--version-id', version)['PolicyVersion']['Document']
    sts = ['sts:AssumeRole', 'sts:TagSession', 'sts:SetSourceIdentity', 'sts:GetCallerIdentity']
    expected = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': sts, 'Resource': '*'},
        {'Effect': 'Deny', 'NotAction': sts, 'Resource': '*'},
        {'Effect': 'Deny', 'Action': sts[:3], 'Resource': f"arn:aws:iam::{record['account']}:role/*"},
    ]}
    require(document == expected, 'Legacy source boundary differs from the customer-STS-only policy')
    require(not aws(record['region'], 'iam', 'list-attached-role-policies', '--role-name', role)['AttachedPolicies'],
            'Legacy role still has managed policy attachments')
    subprocess.run(['python3', str(ROOT / 'modules/gateway/scripts/sync-gateway-engine.py'),
                    '--verify-only', '--image', record['gateway_image'], '--account', record['account'],
                    '--region', record['region'], '--environment', record['environment']], check=True)
    flags = aws(record['region'], 'lambda', 'get-function-configuration', '--function-name',
                f"adp-{record['environment']}-orchestration-tick")['Environment']['Variables']
    require(all(flags.get(k) == 'true' for k in ('AGENT_AUTHORITY_ENABLED', 'ADP_WORK_CLAIMS_ENABLED')),
            'Protected gateway/tick activation is incomplete')
    record['phase'] = 'verified'
    save(directory, record)


def admit(directory):
    verify(directory)  # Recheck live state, even when resuming from a checkpoint.
    record = read_record(directory)
    require(record['original_paused'] is False,
            'Workers were operator-paused before migration; leave them paused and review admission separately')
    configure(directory, 'webhook-ingress', {'agent_worker_admission_paused': False})
    record['phase'] = 'admitting'
    save(directory, record)


def check(directory):
    record = read_record(directory)
    require(scaledjob()['metadata'].get('annotations', {}).get(PAUSE) == 'true', 'Worker admission lost its pause')
    check_drained()
    check_queue(record)


def complete(directory):
    record = read_record(directory)
    deadline = time.monotonic() + 120
    while True:
        job = scaledjob()
        require(job['metadata']['uid'] == record['scaledjob_uid'], 'Worker ScaledJob changed during admission')
        require(not any(k.startswith('autoscaling.keda.sh/paused') for k in job['metadata'].get('annotations', {})),
                'Terraform did not remove the worker admission pause')
        conditions = job.get('status', {}).get('conditions', [])
        if (any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in conditions) and
                not any(c.get('type') == 'Paused' and c.get('status') == 'True' for c in conditions)):
            break
        require(time.monotonic() < deadline, 'KEDA has not resumed ready admission')
        time.sleep(5)
    record['phase'] = 'admitted'
    save(directory, record)


def fail_closed(directory):
    if not (directory / 'worker-migration.json').exists():
        return
    record = read_record(directory)
    if not record.get('pause_started'):
        return
    job = scaledjob()
    require(job['metadata']['uid'] == record['scaledjob_uid'],
            'Cannot pause a ScaledJob replaced outside this migration')
    annotations = dict(job['metadata'].get('annotations', {}), **{PAUSE: 'true'})
    kube('patch', 'scaledjob', 'agent-scaledjob', '-n', 'adp-agents', '--type=json', '-p', json.dumps([
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': job['metadata']['resourceVersion']},
        {'op': 'add', 'path': '/metadata/annotations', 'value': annotations},
    ]))
    subprocess.run(['python3', str(ROOT / 'modules/gateway/scripts/sync-gateway-engine.py'),
                    '--quiesce', '--account', record['account'], '--region', record['region'],
                    '--environment', record['environment']], check=True)
    record['phase'] = 'failed_paused'
    save(directory, record)
    configure(directory, 'webhook-ingress', {'agent_worker_admission_paused': True})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('validate', 'pause', 'check', 'tick', 'verify', 'admit', 'complete', 'fail_closed'))
    parser.add_argument('--directory', type=Path, required=True)
    for name in ('evidence', 'account', 'region', 'environment'):
        parser.add_argument('--' + name)
    args = parser.parse_args()
    try:
        if args.stage == 'validate':
            validate(args.directory, args.evidence, args.account, args.region, args.environment)
        else:
            if (args.directory / 'worker-migration.json').exists():
                record = read_record(args.directory)
                require(aws(record['region'], 'sts', 'get-caller-identity')['Account'] == record['account'],
                        'Worker migration AWS account changed')
            globals()[args.stage](args.directory)
    except (ValueError, KeyError, OSError, TypeError, subprocess.SubprocessError) as error:
        raise SystemExit('Worker migration stopped: ' + str(error))
