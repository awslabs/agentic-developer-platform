#!/usr/bin/env python3
"""State-scoped ADP teardown. AWS CLI credentials; private plans and recovery evidence.

Never select resources by a name prefix. Terraform owns deletion; imperative cleanup
only operates on identifiers in a reviewed delete-only plan or their verified children.
"""
import argparse
import base64
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
ENGINE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
ORDER = ('superplane', 'agent_context', 'agent_factory', 'webhook_ingress', 'gateway', 'platform')
MODULES = {
    'superplane': ('modules/domain-apps/superplane/infra/control-plane', 'modules/superplane'),
    'agent_context': ('modules/agent-context/terraform', 'modules/agent-context'),
    'agent_factory': ('modules/agent-factory/infra', 'modules/agent-factory'),
    'webhook_ingress': ('modules/agent-factory/webhook-ingress/infra', 'modules/webhook-ingress'),
    'gateway': ('modules/gateway/infra', 'modules/gateway'),
    'platform': ('platform/infra', 'platform'),
}
# Consumers must disappear before providers, including phases omitted with --skip.
DEPENDENTS = {
    'agent_context': ('superplane',),
    'agent_factory': ('superplane',),
    'webhook_ingress': ('superplane', 'agent_factory'),
    'gateway': ('superplane', 'agent_context', 'agent_factory', 'webhook_ingress'),
    'platform': ORDER[:-1],
}


class TeardownError(RuntimeError):
    pass


def command(args, cwd=None, capture=True):
    result = subprocess.run([str(a) for a in args], cwd=cwd, text=True,
                            stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE)
    if result.returncode:
        raise TeardownError(f'{args[0]} {args[1]} failed: {result.stderr.strip()}')
    return result.stdout or ''


def aws(*args, absent=()):
    # Object keys can make a 1,000-object S3 batch exceed the OS argument limit.
    # CLI JSON files also preserve exact Unicode/newlines without shell quoting.
    with tempfile.TemporaryDirectory(prefix='adp-teardown-request-') as temporary:
        argv = list(map(str, args))
        for index in range(1, len(argv)):
            if argv[index - 1] in ('--delete', '--image-ids', '--ip-permissions', '--cli-input-json'):
                payload = Path(temporary) / f'{index}.json'
                payload.write_text(argv[index])
                payload.chmod(0o600)
                argv[index] = 'file://' + str(payload)
        result = subprocess.run(['aws', *argv, '--output', 'json'], text=True, capture_output=True)
    if result.returncode:
        match = re.search(r'An error occurred \(([^)]+)\)', result.stderr)
        if match and match[1] in absent:
            return None
        raise TeardownError(f'AWS {args[0]} {args[1]} failed: {result.stderr.strip()}')
    return json.loads(result.stdout) if result.stdout.strip() else {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def instances(state):
    for resource in state.get('resources', []):
        if resource.get('mode') != 'managed':
            continue
        base = '.'.join(filter(None, (resource.get('module'), resource['type'], resource['name'])))
        for item in resource.get('instances', []):
            address = base
            if 'index_key' in item:
                address += '[' + json.dumps(item['index_key']) + ']'
            yield address, resource['type'], item['attributes']


def protected(state, retain_vpc=False):
    """Keep credential containers, versions and decryptability in Terraform state."""
    rows = list(instances(state))
    keep = set()
    keys = set()
    secrets = set()
    for addr, kind, attrs in rows:
        if kind == 'aws_ecr_registry_scanning_configuration':
            keep.add(addr)
        if retain_vpc and kind in ('aws_vpc', 'aws_default_security_group'):
            keep.add(addr)
        if kind == 'aws_secretsmanager_secret':
            name = attrs.get('name', '')
            if (name.startswith(('adp/gh-app-', 'rds!')) or '/gh-app-' in name or '/github-app/' in name
                    or name.endswith('/github-webhook-secret')):
                keep.add(addr)
                secrets.update(filter(None, (attrs.get('id'), attrs.get('arn'))))
                keys.add(attrs.get('kms_key_id'))
    for addr, kind, attrs in rows:
        # Platform holds the shared webhook key in a different state.
        if kind == 'aws_kms_key' and (addr.endswith('.webhook_secrets')
                                     or attrs.get('arn') in keys or attrs.get('id') in keys):
            keep.add(addr)
            keys.update((attrs.get('id'), attrs.get('arn')))
    for addr, kind, attrs in rows:
        if kind == 'aws_secretsmanager_secret_version' and attrs.get('secret_id') in secrets:
            keep.add(addr)
        if kind == 'aws_kms_alias' and (addr.endswith('.webhook_secrets') or attrs.get('target_key_id') in keys):
            keep.add(addr)
    return keep


def deletions(plan, retained=()):
    rows = []
    for resource in plan.get('resource_changes', []):
        actions = resource['change']['actions']
        if actions in (['no-op'], ['read']):
            continue
        if actions != ['delete']:
            raise TeardownError(f"Non-delete action for {resource['address']}: {actions}")
        if resource['address'] in retained:
            raise TeardownError(f"Destroy plan includes retained resource {resource['address']}")
        rows.append(resource)
    return rows


def empty_bucket(bucket, account, call=aws):
    """Bounded, version-aware cleanup; API/per-object errors are fatal."""
    if call('s3api', 'head-bucket', '--bucket', bucket, '--expected-bucket-owner', account,
            absent=('404', 'NoSuchBucket')) is None:
        return
    for _ in range(10000):
        page = call('s3api', 'list-object-versions', '--bucket', bucket,
                    '--expected-bucket-owner', account, '--max-keys', '1000', '--no-paginate')
        objects = [{'Key': v['Key'], 'VersionId': v['VersionId']}
                   for k in ('Versions', 'DeleteMarkers') for v in page.get(k, [])]
        if not objects:
            page = call('s3api', 'list-objects-v2', '--bucket', bucket,
                        '--expected-bucket-owner', account, '--max-keys', '1000', '--no-paginate')
            objects = [{'Key': v['Key']} for v in page.get('Contents', [])]
        if not objects:
            uploads = call('s3api', 'list-multipart-uploads', '--bucket', bucket,
                           '--expected-bucket-owner', account, '--max-uploads', '1000', '--no-paginate')
            if not uploads.get('Uploads'):
                return
            for upload in uploads['Uploads']:
                call('s3api', 'abort-multipart-upload', '--bucket', bucket,
                     '--expected-bucket-owner', account, '--key', upload['Key'], '--upload-id', upload['UploadId'])
            continue
        print(f'S3 {bucket}: deleting {len(objects)} object versions/markers', flush=True)
        result = call('s3api', 'delete-objects', '--bucket', bucket, '--expected-bucket-owner', account,
                      '--delete', json.dumps({'Objects': objects, 'Quiet': True}))
        if result.get('Errors'):
            raise TeardownError(f'S3 deletion failed in {bucket}: {len(result["Errors"])} object errors')
    raise TeardownError(f'Bucket {bucket} did not empty; check active writers or object retention')


def empty_repository(name, account, call=aws):
    for _ in range(10000):
        result = call('ecr', 'list-images', '--repository-name', name, '--registry-id', account,
                      '--max-results', '100', '--no-paginate', absent=('RepositoryNotFoundException',))
        if result is None or not result['imageIds']:
            return
        print(f'ECR {name}: deleting {len(result["imageIds"])} image references', flush=True)
        result = call('ecr', 'batch-delete-image', '--repository-name', name, '--registry-id', account,
                      '--image-ids', json.dumps(result['imageIds']))
        failures = result.get('failures', [])
        if failures and (any(f.get('failureCode') != 'ImageReferencedByManifestList' for f in failures)
                         or not result.get('imageIds')):
            raise TeardownError(f'ECR deletion failed in {name}: {len(failures)} image errors')
        # Manifest-list deletion can free child digests within the same batch.
        # Retry only if AWS reports actual progress, never on permission failures.
    raise TeardownError(f'Repository {name} did not empty; check active builds')


class Run:
    def __init__(self, root, account, region, environment, retain_vpc=False, purge_deleted_secrets=False):
        self.root, self.account, self.region, self.environment = Path(root), account, region, environment
        self.retain_vpc = retain_vpc
        self.purge_deleted_secrets = purge_deleted_secrets
        self.bucket = f'adp-terraform-state-{account}'
        self.directory = Path(os.environ.get('ADP_TEARDOWN_DIR',
                              self.root / '.adp-teardown' / f'{account}-{region}-{environment}'))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.states = {}
        self.prepared = {}
        self.sequence = time.time_ns()

    def active(self, state):
        return {a for a, _, _ in instances(state)} - protected(state, self.retain_vpc)

    def load_states(self):
        for phase, (_, key) in MODULES.items():
            target = self.directory / f'{phase}-discovered.tfstate'
            result = aws('s3api', 'get-object', '--bucket', self.bucket,
                         '--key', f'{self.environment}/{key}/terraform.tfstate',
                         '--expected-bucket-owner', self.account, str(target),
                         absent=('NoSuchKey',))
            self.states[phase] = json.loads(target.read_text()) if result is not None else {}
            if target.exists():
                target.chmod(0o600)

    def bind_checkpoint(self):
        path = self.directory / 'identity.json'
        current = {'account': self.account, 'region': self.region, 'environment': self.environment,
                   'lineages': {p: s.get('lineage') for p, s in self.states.items() if s.get('lineage')}}
        if path.exists():
            previous = json.loads(path.read_text())
            for phase, lineage in current['lineages'].items():
                if previous.get('lineages', {}).get(phase, lineage) != lineage:
                    raise TeardownError('State lineage changed; use a new ADP_TEARDOWN_DIR for this installation')
        write_json(path, current)

    def assert_backend_empty(self):
        # Backend is shared across environments/modules. Checking only this run's
        # six module keys could erase another deployment's only state copy.
        objects = aws('s3api', 'list-objects-v2', '--bucket', self.bucket,
                      '--expected-bucket-owner', self.account).get('Contents', [])
        for item in objects:
            if not item['Key'].endswith('.tfstate'):
                continue
            with tempfile.TemporaryDirectory(prefix='adp-state-check-', dir=self.directory) as directory:
                path = Path(directory) / 'state.json'
                aws('s3api', 'get-object', '--bucket', self.bucket, '--key', item['Key'],
                    '--expected-bucket-owner', self.account, str(path))
                if list(instances(json.loads(path.read_text()))):
                    raise TeardownError('Backend still contains tracked resources in ' + item['Key'])

    def check_selection(self, phases):
        for phase in phases:
            for dependent in DEPENDENTS.get(phase, ()):
                if dependent not in phases and self.active(self.states[dependent]):
                    raise TeardownError(f'{phase} is still required by {dependent}; include that phase first')

    def kube(self, *args):
        return command(['kubectl', '--kubeconfig', self.directory / 'kubeconfig', *args])

    def setup_kube(self):
        clusters = [a for _, k, a in instances(self.states['platform']) if k == 'aws_eks_cluster']
        if not clusters:
            return
        cluster = clusters[0]
        live = aws('eks', 'describe-cluster', '--name', cluster['name'], absent=('ResourceNotFoundException',))
        if live is None:
            if any(k.startswith(('kubernetes_', 'helm_')) for state in self.states.values()
                   for _, k, _ in instances(state)):
                raise TeardownError('EKS is absent but Kubernetes resources remain in state; recovery is required')
            return
        if live['cluster']['arn'] != cluster['arn']:
            raise TeardownError('EKS identity differs from Terraform state')
        command(['aws', 'eks', 'update-kubeconfig', '--name', cluster['name'], '--region', self.region,
                 '--kubeconfig', self.directory / 'kubeconfig'])
        os.environ['KUBECONFIG'] = str(self.directory / 'kubeconfig')
        self.kube('get', 'namespaces', '-o', 'name')

    def check_network(self):
        state = self.states['platform']
        owned = {a.get('id') for s in self.states.values() for _, _, a in instances(s)}
        for _, kind, attrs in instances(state):
            if kind == 'aws_eks_cluster':
                owned.update(v.get('cluster_security_group_id') for v in attrs.get('vpc_config', []))
        for _, kind, attrs in instances(state):
            if kind != 'aws_vpc':
                continue
            groups = aws('ec2', 'describe-security-groups', '--filters', f'Name=vpc-id,Values={attrs["id"]}')
            cluster = f'adp-{self.environment}-eks-cluster'
            unknown = []
            for group in groups['SecurityGroups']:
                tags = {t['Key']: t['Value'] for t in group.get('Tags', [])}
                controller = tags.get('elbv2.k8s.aws/cluster') == cluster
                if group['GroupId'] not in owned and group['GroupName'] != 'default' and not controller:
                    unknown.append(group['GroupId'])
            if unknown and not self.retain_vpc:
                raise TeardownError('VPC has security groups outside ADP state: ' + ', '.join(unknown)
                                    + '. Resolve ownership or use --retain-vpc; no groups were deleted.')

    def check_account_logging(self):
        for _, kind, attrs in instances(self.states['platform']):
            if kind != 'aws_bedrock_model_invocation_logging_configuration':
                continue
            live = aws('bedrock', 'get-model-invocation-logging-configuration').get('loggingConfig', {})
            if not live:
                continue
            config = attrs['logging_config'][0]
            for tf_key, api_key, fields in (
                ('cloudwatch_config', 'cloudWatchConfig', {'log_group_name': 'logGroupName', 'role_arn': 'roleArn'}),
                ('s3_config', 's3Config', {'bucket_name': 'bucketName', 'key_prefix': 'keyPrefix'}),
            ):
                previous = (config.get(tf_key) or [{}])[0]
                current = live.get(api_key, {})
                if any((previous.get(k) or '') != (current.get(v) or '') for k, v in fields.items()):
                    raise TeardownError('Bedrock logging no longer matches ADP state; resolve ownership before teardown')

    def tf(self, phase, *args, capture=True):
        return command(['terraform', *args], cwd=self.root / MODULES[phase][0], capture=capture)

    def inputs(self, phase, state):
        directory = self.root / MODULES[phase][0]
        saved = state.get('outputs', {}).get('release_configuration', {}).get('value')
        if saved is None and phase == 'agent_factory':
            spec = importlib.util.spec_from_file_location('upgrade_state', self.root / 'platform/scripts/upgrade-state.py')
            tools = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tools)
            saved = tools.factory_settings(state)
            saved.update(gateway_deployed=bool(list(instances(self.states['gateway']))),
                         seed_agent_registry=False)
        if saved is not None:
            if not isinstance(saved, dict):
                raise TeardownError('Invalid recorded release configuration')
            values = dict(saved, environment=self.environment, aws_region=self.region)
            path = self.directory / f'{phase}.tfvars.json'
            write_json(path, values)
            args = ['-var-file=' + str(path)]
        else:
            path = (directory / 'terraform.tfvars' if phase in ('agent_factory', 'webhook_ingress') else
                    self.root / 'environments' / self.environment /
                    ('platform.tfvars' if phase == 'platform' else MODULES[phase][1] + '.tfvars'))
            if not path.exists():
                raise TeardownError(f'{phase}: original deployment inputs are required at {path}; no recorded inputs exist')
            args = ['-var-file=' + str(path)]
        # Target binding is always last. Never inherit another environment from checked-in examples.
        args += ['-var=environment=' + self.environment, '-var=aws_region=' + self.region]
        if phase == 'webhook_ingress':
            image_file = self.directory / 'webhook-image.json'
            image = (json.loads(image_file.read_text()) if image_file.exists() else
                     (saved or {}).get('agent_image', ''))
            if not image:
                result = self.kube('get', 'daemonset', 'agent-image-prepull', '-n', 'adp-agents',
                                   '--ignore-not-found', '-o', 'json')
                if result.strip():
                    spec = json.loads(result)['spec']['template']['spec']
                    containers = spec.get('initContainers', []) + spec.get('containers', [])
                    images = {c['image'] for c in containers if 'agent-runtime' in c['image']}
                    if len(images) != 1:
                        raise TeardownError('Cannot unambiguously recover the deployed worker image')
                    image = images.pop()
            if image:
                if not re.search(r'(@sha256:[0-9a-f]{64}|:[0-9a-f]{40})$', image):
                    match = re.fullmatch(r'([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/([^:]+):([^:]+)', image)
                    if not match or match[1] != self.account or match[2] != self.region:
                        raise TeardownError('Cannot recover worker image digest; supply original agent_image')
                    details = aws('ecr', 'describe-images', '--registry-id', self.account,
                                  '--repository-name', match[3], '--image-ids', json.dumps([{'imageTag': match[4]}]))
                    digest = details['imageDetails'][0]['imageDigest']
                    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                        raise TeardownError('Invalid ECR image digest')
                    image = image.rsplit(':', 1)[0] + '@' + digest
                write_json(image_file, image)
                args += ['-var=agent_image=' + image]
        if phase == 'agent_factory':
            artifact = directory / '.build/session-sweeper/index.js'
            if not artifact.exists():
                functions = [a for addr, kind, a in instances(state)
                             if kind == 'aws_lambda_function' and addr.endswith('.session_sweeper')]
                if not functions:
                    raise TeardownError('Session sweeper artifact missing; restore original deployment artifact')
                deployed = aws('lambda', 'get-function', '--function-name', functions[0]['function_name'])
                with urllib.request.urlopen(deployed['Code']['Location'], timeout=60) as response:
                    archive = response.read()
                digest = base64.b64encode(hashlib.sha256(archive).digest()).decode()
                if digest != deployed['Configuration']['CodeSha256']:
                    raise TeardownError('Deployed Lambda archive digest mismatch')
                with zipfile.ZipFile(io.BytesIO(archive)) as archive_file:
                    content = archive_file.read('index.js')
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_bytes(content)
        return args

    def prepare(self, phase):
        state = self.states[phase]
        if not self.active(state):
            print(f'{phase}: no resources selected for deletion')
            return False
        directory = self.root / MODULES[phase][0]
        backend = self.directory / f'{phase}-backend.json'
        write_json(backend, {'bucket': self.bucket, 'key': f'{self.environment}/{MODULES[phase][1]}/terraform.tfstate',
                             'region': self.region, 'encrypt': True, 'dynamodb_table': 'adp-terraform-locks'})
        override = directory / 'adp_teardown_target_override.tf.json'
        data = {'provider': {'aws': {'region': self.region, 'allowed_account_ids': [self.account]}}}
        if override.exists() and json.loads(override.read_text()) != data:
            raise TeardownError(f'Different teardown target override already exists at {override}')
        write_json(override, data)
        self.tf(phase, 'init', '-input=false', '-reconfigure', '-backend-config=' + str(backend))
        actual = json.loads(self.tf(phase, 'state', 'pull'))
        write_json(self.directory / f'{phase}-{self.sequence}-before.tfstate', actual)
        self.states[phase] = actual
        self.prepared[phase] = self.inputs(phase, actual)
        self.plan(phase, 'preflight')
        self.runtime_logs(phase)
        self.runtime_secrets(phase)
        self.runtime_parameters(phase)
        runtime_logs = json.loads((self.directory / f'{phase}-runtime-log-groups.json').read_text())
        print(f'{phase}: {len(runtime_logs)} parent-owned runtime log groups selected for cleanup', flush=True)
        return True

    def plan(self, phase, label, targets=None):
        state = json.loads(self.tf(phase, 'state', 'pull'))
        keep = protected(state, self.retain_vpc)
        selected = [a for a, _, _ in instances(state) if a not in keep] if targets is None else targets
        if not selected:
            return None, []
        path = self.directory / f'{phase}-{label}-{time.time_ns()}.tfplan'
        args = ['plan', '-destroy', '-input=false', '-lock-timeout=60s', '-out=' + str(path),
                *self.prepared[phase]]
        if keep or targets is not None:
            args.extend('-target=' + address for address in selected)
        # Keep Terraform's potentially sensitive output in the private evidence directory.
        output = self.tf(phase, *args)
        (path.with_suffix('.log')).write_text(output)
        (path.with_suffix('.log')).chmod(0o600)
        path.chmod(0o600)
        plan = json.loads(self.tf(phase, 'show', '-json', str(path)))
        write_json(path.with_suffix('.json'), plan)
        rows = deletions(plan, keep)
        write_json(self.directory / f'{phase}-retained.json',
                   [{'address': a, 'type': k, 'id': v.get('id')} for a, k, v in instances(state) if a in keep])
        print(f'{phase}/{label}: {len(rows)} deletions, {len(keep)} retained', flush=True)
        return path, rows

    def record_deletions(self, phase, rows):
        manifest = self.directory / f'{phase}-deletion-manifest.json'
        existing = json.loads(manifest.read_text()) if manifest.exists() else {}
        for resource in rows:
            before = resource['change']['before'] or {}
            existing[resource['address']] = {'type': resource['type'], 'id': before.get('id'),
                                             'arn': before.get('arn'), 'name': before.get('name')}
        write_json(manifest, existing)

    def apply(self, phase, plan):
        if plan:
            self.record_deletions(phase, deletions(json.loads(plan.with_suffix('.json').read_text())))
            self.tf(phase, 'apply', '-input=false', '-lock-timeout=60s', str(plan), capture=False)

    def verify(self, phase):
        path = self.directory / f'{phase}-deletion-manifest.json'
        manifest = json.loads(path.read_text()) if path.exists() else {}
        checks = {
            'aws_lambda_function': ('lambda', 'get-function-configuration', '--function-name', 'arn', ('ResourceNotFoundException',)),
            'aws_eks_cluster': ('eks', 'describe-cluster', '--name', 'id', ('ResourceNotFoundException',)),
            'aws_ecr_repository': ('ecr', 'describe-repositories', '--repository-names', 'name', ('RepositoryNotFoundException',)),
            'aws_db_instance': ('rds', 'describe-db-instances', '--db-instance-identifier', 'arn', ('DBInstanceNotFound',)),
            'aws_elasticache_replication_group': ('elasticache', 'describe-replication-groups', '--replication-group-id', 'id', ('ReplicationGroupNotFoundFault',)),
            'aws_lb': ('elbv2', 'describe-load-balancers', '--load-balancer-arns', 'arn', ('LoadBalancerNotFound',)),
            'aws_cloudfront_distribution': ('cloudfront', 'get-distribution', '--id', 'id', ('NoSuchDistribution',)),
        }
        verified, pending = [], []
        for address, attrs in manifest.items():
            kind = attrs['type']
            if kind in checks:
                service, operation, flag, field, absent = checks[kind]
                if not attrs.get(field):
                    raise TeardownError('Deletion receipt lacks identity for ' + address)
                if aws(service, operation, flag, attrs[field], absent=absent) is not None:
                    raise TeardownError('Resource still exists after Terraform deletion: ' + address)
                verified.append(address)
            if kind == 'aws_s3_bucket':
                if aws('s3api', 'head-bucket', '--bucket', attrs['id'], '--expected-bucket-owner', self.account,
                       absent=('404', 'NoSuchBucket')) is not None:
                    raise TeardownError('Bucket still exists after Terraform deletion: ' + address)
                verified.append(address)
            if kind == 'aws_kms_key':
                key = aws('kms', 'describe-key', '--key-id', attrs['id'], absent=('NotFoundException',))
                if key is not None:
                    if key['KeyMetadata']['KeyState'] != 'PendingDeletion':
                        raise TeardownError('KMS key is not pending deletion: ' + address)
                    pending.append({'address': address, 'kind': 'kms', 'id': attrs['id'],
                                    'deletion_date': key['KeyMetadata'].get('DeletionDate')})
            if kind == 'aws_secretsmanager_secret':
                secret = aws('secretsmanager', 'describe-secret', '--secret-id', attrs['id'],
                             absent=('ResourceNotFoundException',))
                if secret is not None:
                    if not secret.get('DeletedDate'):
                        raise TeardownError('Secret remains active after deletion: ' + address)
                    if self.purge_deleted_secrets:
                        self.purge_secret(address, attrs, secret)
                        verified.append(address)
                        continue
                    print(f'Secret name remains reserved until AWS deletion: {secret["Name"]}. '
                          'For a clean reinstall, review --purge-deleted-secrets or restore/import it.', flush=True)
                    pending.append({'address': address, 'kind': 'secret', 'id': attrs['id'],
                                    'deletion_date': secret['DeletedDate']})
        write_json(self.directory / f'{phase}-verification.json', {'verified_absent': verified, 'pending_deletion': pending})

    def purge_secret(self, address, attrs, secret):
        # Only exact identities already selected by a reviewed Terraform destroy
        # plan. Never sweep by prefix or delete a replacement with the same name.
        arn = secret['ARN']
        if (attrs.get('arn') or attrs['id']) != arn or not arn.startswith(
                f'arn:aws:secretsmanager:{self.region}:{self.account}:secret:'):
            raise TeardownError('Secret identity differs from deletion receipt: ' + address)
        fixture = {'resources': [{'mode': 'managed', 'type': 'aws_secretsmanager_secret',
                   'name': 'candidate', 'instances': [{'attributes': {'name': secret['Name']}}]}]}
        if protected(fixture):
            raise TeardownError('Refusing to purge retained credential: ' + address)
        aws('secretsmanager', 'delete-secret', '--secret-id', arn, '--force-delete-without-recovery',
            absent=('ResourceNotFoundException',))
        deadline = time.monotonic() + 120
        while aws('secretsmanager', 'describe-secret', '--secret-id', arn,
                  absent=('ResourceNotFoundException',)) is not None:
            if time.monotonic() >= deadline:
                raise TeardownError('Secret purge is still pending; retry before reinstalling: ' + address)
            time.sleep(5)

    def stage(self, phase, label, kinds):
        state = json.loads(self.tf(phase, 'state', 'pull'))
        targets = [a for a, k, _ in instances(state) if kinds(k)]
        if targets:
            plan, rows = self.plan(phase, label, targets)
            # Terraform can expand destroy targets to dependents. Never let a cleanup
            # stage remove its providers or permissions before asynchronous deletion.
            roles = {a.get('role') for _, k, a in instances(self.states[phase]) if k == 'aws_lambda_function'}
            role_names = {r.rsplit('/', 1)[-1] for r in roles if r}
            for resource in rows:
                before = resource['change']['before'] or {}
                if (resource['type'].startswith('aws_eks_')
                        or resource['type'] == 'aws_iam_role' and before.get('arn') in roles
                        or resource['type'] in ('aws_iam_role_policy', 'aws_iam_role_policy_attachment')
                        and before.get('role') in role_names):
                    raise TeardownError(f'{label} unexpectedly includes execution/access dependencies')
            self.apply(phase, plan)

    def ingress(self, phase, namespace):
        checkpoint = self.directory / f'{phase}-ingress.json'
        listing = json.loads(self.kube('get', 'ingress', '-n', namespace, '-o', 'json', '--ignore-not-found'))
        ingresses = listing.get('items', [])
        tracked = json.loads(checkpoint.read_text()) if checkpoint.exists() else []
        hosts = {item['hostname'] for ingress in ingresses
                 for item in ingress.get('status', {}).get('loadBalancer', {}).get('ingress', []) if 'hostname' in item}
        if hosts:
            lbs = aws('elbv2', 'describe-load-balancers')['LoadBalancers']
            for lb in lbs:
                if lb['DNSName'] in hosts and lb['LoadBalancerArn'] not in tracked:
                    tracked.append(lb['LoadBalancerArn'])
        write_json(checkpoint, tracked)
        # The VPC-link SG references controller-owned SGs. Remove only rules that
        # are present in this module's saved state AND point at this cluster.
        cluster = f'adp-{self.environment}-eks-cluster'
        for address, kind, attrs in instances(self.states[phase]):
            if kind != 'aws_security_group' or not address.endswith('.vpc_link'):
                continue
            live = aws('ec2', 'describe-security-groups', '--group-ids', attrs['id'],
                       absent=('InvalidGroup.NotFound',))
            if live is None:
                continue
            for rule in attrs.get('egress', []):
                for group in rule.get('security_groups', []):
                    target = aws('ec2', 'describe-security-groups', '--group-ids', group,
                                 absent=('InvalidGroup.NotFound',))
                    if target is None:
                        continue
                    tags = {t['Key']: t['Value'] for t in target['SecurityGroups'][0].get('Tags', [])}
                    if tags.get('elbv2.k8s.aws/cluster') != cluster:
                        continue
                    for permission in live['SecurityGroups'][0].get('IpPermissionsEgress', []):
                        if (str(permission['IpProtocol']) == str(rule['protocol'])
                                and permission.get('FromPort', 0) == rule['from_port']
                                and permission.get('ToPort', 0) == rule['to_port']
                                and any(p['GroupId'] == group for p in permission.get('UserIdGroupPairs', []))):
                            revoke = {k: v for k, v in permission.items() if k in ('IpProtocol', 'FromPort', 'ToPort')}
                            revoke['UserIdGroupPairs'] = [{'GroupId': group}]
                            aws('ec2', 'revoke-security-group-egress', '--group-id', attrs['id'],
                                '--ip-permissions', json.dumps([revoke]), absent=('InvalidPermission.NotFound',))
        if ingresses:
            self.kube('delete', 'ingress', '--all', '-n', namespace, '--wait=true', '--timeout=600s')
        deadline = time.monotonic() + 600
        while tracked:
            tracked = [arn for arn in tracked if aws('elbv2', 'describe-load-balancers',
                       '--load-balancer-arns', arn, absent=('LoadBalancerNotFound',)) is not None]
            if not tracked:
                break
            if time.monotonic() >= deadline:
                raise TeardownError('Controller load balancers remain; keeping lower dependencies')
            print(f'{phase}: waiting for {len(tracked)} controller load balancers', flush=True)
            time.sleep(10)

    def stop_keda(self, phase):
        # Some ScaledJobs are kubectl-managed through null_resource with
        # create_before_destroy, which suppresses destroy provisioners. Drain
        # their CRs explicitly while the webhook-owned KEDA operator still runs.
        namespace = {'agent_factory': 'adp-gateway-agents', 'webhook_ingress': 'adp-agents'}.get(phase)
        if not namespace or not self.kube('get', 'namespace', namespace, '--ignore-not-found', '-o', 'name').strip():
            return
        for resource in ('scaledjobs', 'scaledobjects', 'triggerauthentications'):
            if self.kube('get', 'customresourcedefinition', resource + '.keda.sh',
                         '--ignore-not-found', '-o', 'name').strip():
                self.kube('delete', resource, '--all', '-n', namespace, '--ignore-not-found',
                          '--cascade=foreground', '--wait=true', '--timeout=300s')

    def delete_namespace(self, name):
        if self.kube('get', 'namespace', name, '--ignore-not-found', '-o', 'name').strip():
            self.kube('delete', 'namespace', name, '--wait=true', '--timeout=300s')

    def lambda_cleanup(self, phase):
        checkpoint = self.directory / f'{phase}-lambda-enis.json'
        if not checkpoint.exists() and not any(k == 'aws_lambda_function' for _, k, _ in instances(self.states[phase])):
            return
        interfaces = set(json.loads(checkpoint.read_text())) if checkpoint.exists() else set()
        # Capture ENIs before deleting functions; a retry uses this checkpoint even
        # when the functions have already disappeared from Terraform state.
        for _, kind, attrs in instances(self.states[phase]):
            if kind != 'aws_lambda_function':
                continue
            for config in attrs.get('vpc_config', []):
                groups = config.get('security_group_ids', [])
                subnets = config.get('subnet_ids', [])
                if not groups or not subnets:
                    continue
                result = aws('ec2', 'describe-network-interfaces', '--filters',
                             'Name=group-id,Values=' + ','.join(groups),
                             'Name=subnet-id,Values=' + ','.join(subnets))
                interfaces.update(i['NetworkInterfaceId'] for i in result['NetworkInterfaces']
                                  if i.get('InterfaceType') == 'lambda')
        write_json(checkpoint, sorted(interfaces))
        # Terraform target expansion can include entire dependent modules. Delete
        # only functions in the validated full destroy plan; keep state intact so
        # the next refreshed plan reconciles absence without state surgery.
        _, changes = self.plan(phase, 'lambda-predelete')
        self.record_deletions(phase, [r for r in changes if r['type'] == 'aws_lambda_function'])
        for resource in changes:
            if resource['type'] != 'aws_lambda_function':
                continue
            attrs = resource['change']['before']
            live = aws('lambda', 'get-function-configuration', '--function-name', attrs['arn'],
                       absent=('ResourceNotFoundException',))
            if live is None:
                continue
            if live['FunctionArn'] != attrs['arn'] or live['Role'] != attrs['role']:
                raise TeardownError('Lambda ownership/configuration changed since planning')
            aws('lambda', 'delete-function', '--function-name', attrs['arn'],
                absent=('ResourceNotFoundException',))
        deadline = time.monotonic() + 1800
        while interfaces:
            remaining = set()
            for interface in interfaces:
                result = aws('ec2', 'describe-network-interfaces', '--network-interface-ids', interface,
                             absent=('InvalidNetworkInterfaceID.NotFound',))
                if result is not None:
                    remaining.add(interface)
            interfaces = remaining
            if not interfaces:
                break
            if time.monotonic() >= deadline:
                raise TeardownError('Lambda ENIs remain; execution roles and permissions retained for retry')
            print(f'{phase}: waiting for AWS to release {len(interfaces)} Lambda ENIs; keeping IAM permissions', flush=True)
            time.sleep(15)

    def runtime_logs(self, phase, delete=False):
        path = self.directory / f'{phase}-runtime-log-groups.json'
        recorded = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(recorded, dict):
            raise TeardownError('Unversioned runtime log checkpoint; inspect it before retrying')
        if not delete:
            names = set()
            for address, kind, attrs in instances(self.states[phase]):
                if kind == 'aws_lambda_function':
                    names.add('/aws/lambda/' + attrs['function_name'])
                if kind == 'aws_codebuild_project':
                    names.add('/aws/codebuild/' + attrs['name'])
                if kind == 'aws_eks_cluster':
                    names.add('/aws/eks/' + attrs['name'] + '/cluster')
                    prefix = '/aws/containerinsights/' + attrs['name'] + '/'
                    names.update(g['logGroupName'] for g in aws('logs', 'describe-log-groups',
                                 '--log-group-name-prefix', prefix)['logGroups'])
                if kind == 'aws_api_gateway_stage':
                    names.add('API-Gateway-Execution-Logs_' + attrs['rest_api_id'] + '/' + attrs['stage_name'])
                if kind in ('kubernetes_config_map', 'kubernetes_config_map_v1') and re.search(r'\.otel_collector_config(?:\[|$)', address):
                    for config in (attrs.get('data') or {}).values():
                        for name in re.findall(r'^\s*log_group_name:\s*([^\s]+)', config, re.MULTILINE):
                            name = name.strip('"\'')
                            if name in (f'/adp/{self.environment}/agent-factory/otel/logs',
                                        f'/adp/{self.environment}/agent-factory/otel/metrics'):
                                names.add(name)
            for name in names:
                for group in aws('logs', 'describe-log-groups', '--log-group-name-prefix', name)['logGroups']:
                    if group['logGroupName'] == name:
                        recorded.setdefault(name, group['creationTime'])
            write_json(path, recorded)
        else:
            for name, created in sorted(recorded.items()):
                groups = aws('logs', 'describe-log-groups', '--log-group-name-prefix', name)['logGroups']
                for group in groups:
                    if group['logGroupName'] != name:
                        continue
                    if group['creationTime'] != created:
                        raise TeardownError('Runtime log group was replaced since discovery: ' + name)
                    aws('logs', 'delete-log-group', '--log-group-name', name, absent=('ResourceNotFoundException',))

    def runtime_secrets(self, phase, delete=False):
        # Exact names created by deploy-all.sh, not a prefix sweep of user vaults
        # or GitHub/OAuth credentials. Bind deletion to the discovered ARN suffix.
        if phase != 'gateway':
            return
        path = self.directory / 'gateway-runtime-secrets.json'
        recorded = json.loads(path.read_text()) if path.exists() else {}
        if not delete:
            for suffix in ('token-secret-key', 'internal-api-key', 'magic-link-secret'):
                name = f'adp/{self.environment}/gateway/{suffix}'
                secret = aws('secretsmanager', 'describe-secret', '--secret-id', name,
                             absent=('ResourceNotFoundException',))
                if secret is not None:
                    recorded.setdefault(name, secret['ARN'])
            write_json(path, recorded)
        else:
            for name, arn in recorded.items():
                secret = aws('secretsmanager', 'describe-secret', '--secret-id', name,
                             absent=('ResourceNotFoundException',))
                if secret is None:
                    continue
                if secret['ARN'] != arn:
                    raise TeardownError('Runtime secret replaced since discovery: ' + name)
                aws('secretsmanager', 'delete-secret', '--secret-id', arn, '--force-delete-without-recovery',
                    absent=('ResourceNotFoundException',))

    def runtime_parameters(self, phase, delete=False):
        if phase != 'gateway':
            return
        path = self.directory / 'gateway-runtime-parameters.json'
        recorded = json.loads(path.read_text()) if path.exists() else {}
        suffixes = ('frontend-bucket', 'cloudfront-id', 'cloudfront-domain', 'internal-alb-arn',
                    'internal-alb-dns', 'internal-alb-security-group-ids')
        if not delete:
            for suffix in suffixes:
                name = f'/adp/{self.environment}/gateway/{suffix}'
                response = aws('ssm', 'get-parameter', '--name', name, absent=('ParameterNotFound',))
                if response is not None:
                    parameter = response['Parameter']
                    recorded.setdefault(name, {'version': parameter['Version'], 'modified': parameter['LastModifiedDate']})
            write_json(path, recorded)
        else:
            for name, identity in recorded.items():
                response = aws('ssm', 'get-parameter', '--name', name, absent=('ParameterNotFound',))
                if response is None:
                    continue
                parameter = response['Parameter']
                if {'version': parameter['Version'], 'modified': parameter['LastModifiedDate']} != identity:
                    raise TeardownError('Discovery parameter changed since planning: ' + name)
                aws('ssm', 'delete-parameter', '--name', name, absent=('ParameterNotFound',))

    def stop_builds(self, phase):
        for _, kind, attrs in instances(self.states[phase]):
            if kind != 'aws_codebuild_project':
                continue
            ids = aws('codebuild', 'list-builds-for-project', '--project-name', attrs['name'],
                      absent=('ResourceNotFoundException',))
            if ids is None:
                continue
            active = []
            for offset in range(0, len(ids['ids']), 100):
                builds = aws('codebuild', 'batch-get-builds', '--ids', *ids['ids'][offset:offset + 100])
                active.extend(b['id'] for b in builds['builds'] if b['buildStatus'] == 'IN_PROGRESS')
            for build in active:
                aws('codebuild', 'stop-build', '--id', build)
            deadline = time.monotonic() + 300
            while active:
                active = [b['id'] for b in aws('codebuild', 'batch-get-builds', '--ids', *active)['builds']
                          if b['buildStatus'] == 'IN_PROGRESS']
                if active and time.monotonic() >= deadline:
                    raise TeardownError('CodeBuild writers did not stop; keeping artifact stores')
                if active:
                    time.sleep(5)

    def quiesce(self, phases):
        # Stop schedules across the selected graph before removing downstream
        # consumers. Phase Terraform/data dependencies stay available for destroy.
        for phase in phases:
            for _, kind, attrs in instances(self.states[phase]):
                if kind == 'aws_cloudwatch_event_rule':
                    rule = aws('events', 'describe-rule', '--name', attrs['name'],
                               '--event-bus-name', attrs.get('event_bus_name') or 'default',
                               absent=('ResourceNotFoundException',))
                    if rule is not None and rule['State'] != 'DISABLED':
                        aws('events', 'disable-rule', '--name', attrs['name'],
                            '--event-bus-name', attrs.get('event_bus_name') or 'default')
        # The gateway can write factory chat artifacts even though factory must
        # precede gateway infrastructure. Stop the application, retain its RDS,
        # APIs and Terraform outputs until consumer teardown has finished.
        if 'gateway' in phases and self.active(self.states['gateway']):
            namespace = 'adp-gateway'
            if self.kube('get', 'namespace', namespace, '--ignore-not-found', '-o', 'name').strip():
                self.kube('delete', 'horizontalpodautoscalers', '--all', '-n', namespace, '--ignore-not-found')
                for kind in ('deployments', 'statefulsets'):
                    names = self.kube('get', kind, '-n', namespace, '-o', 'name').split()
                    if names:
                        self.kube('scale', *names, '-n', namespace, '--replicas=0')
                listing = json.loads(self.kube('get', 'pods', '-n', namespace, '-o', 'json'))
                pods = ['pod/' + pod['metadata']['name'] for pod in listing.get('items', [])
                        if any(owner['kind'] in ('ReplicaSet', 'StatefulSet')
                               for owner in pod['metadata'].get('ownerReferences', []))]
                if pods:
                    self.kube('wait', '--for=delete', *pods, '-n', namespace, '--timeout=300s')

    def execute(self, phase):
        if phase not in self.prepared:
            if not self.prepare(phase):
                self.runtime_logs(phase, delete=True)
                self.runtime_secrets(phase, delete=True)
                self.runtime_parameters(phase, delete=True)
                self.verify(phase)
                return
        self.runtime_logs(phase)
        # Phase preflight has succeeded before any namespace/data mutation.
        if phase == 'superplane':
            names = [a['value'] for _, k, a in instances(self.states[phase]) if k == 'aws_ssm_parameter'
                     and a.get('name', '').endswith(('/superplane/namespace', '/superplane/skypilot-namespace'))]
            for namespace in names:
                self.ingress(phase, namespace)
                self.delete_namespace(namespace)
        if phase == 'agent_context':
            self.ingress(phase, 'agent-context')
        if phase == 'gateway':
            self.ingress(phase, 'adp-gateway')
        # Lambdas stop writing first, without removing their ENI cleanup roles.
        self.lambda_cleanup(phase)
        self.stop_keda(phase)
        # K8s objects must be gone while access entries, KEDA and EKS still exist.
        # Keep Helm until the CR stage completes (webhook owns KEDA).
        self.stage(phase, 'kubernetes', lambda k: k.startswith('kubernetes_')
                   and k not in ('kubernetes_namespace', 'kubernetes_namespace_v1'))
        self.stage(phase, 'helm', lambda k: k == 'helm_release')
        self.stage(phase, 'namespaces', lambda k: k in ('kubernetes_namespace', 'kubernetes_namespace_v1'))
        namespaces = {
            'agent_factory': ('adp-gateway-agents', 'arc-runners'),
            'webhook_ingress': ('adp-agents',),
            'gateway': ('adp-gateway',),
            'agent_context': ('agent-context',),
        }
        if (self.directory / 'kubeconfig').exists():
            for namespace in namespaces.get(phase, ()):
                self.delete_namespace(namespace)
        # Bedrock log delivery and CodeBuild are writers too. Stop them before
        # emptying their stores. No asynchronous Lambda role deletion in this stage.
        self.stop_builds(phase)
        self.stage(phase, 'writers', lambda k: k in (
            'aws_bedrock_model_invocation_logging_configuration', 'aws_codebuild_project'))
        plan, rows = self.plan(phase, 'final')
        for resource in rows:
            before = resource['change']['before']
            if resource['type'] == 'aws_s3_bucket':
                if before['id'] == self.bucket:
                    raise TeardownError('Refusing to empty Terraform backend during a module phase')
                empty_bucket(before['id'], self.account)
            if resource['type'] == 'aws_ecr_repository':
                empty_repository(before['name'], self.account)
        self.apply(phase, plan)
        after = json.loads(self.tf(phase, 'state', 'pull'))
        write_json(self.directory / f'{phase}-{self.sequence}-after.tfstate', after)
        remaining = {a for a, _, _ in instances(after)} - protected(after, self.retain_vpc)
        if remaining:
            raise TeardownError(f'{phase}: unexpected resources remain: ' + ', '.join(sorted(remaining)))
        self.runtime_logs(phase, delete=True)
        self.runtime_secrets(phase, delete=True)
        self.runtime_parameters(phase, delete=True)
        self.verify(phase)
        self.states[phase] = after
        print(f'{phase}: selected resources removed; retained resources recorded separately', flush=True)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--environment', default=os.environ.get('ADP_ENVIRONMENT', os.environ.get('ENVIRONMENT', 'dev')))
    result.add_argument('--region', default=os.environ.get('ADP_REGION', os.environ.get('AWS_REGION', 'us-east-1')))
    result.add_argument('--root', type=Path, default=ROOT)
    result.add_argument('--dry-run', action='store_true', default=os.environ.get('DRY_RUN') == 'true')
    result.add_argument('--skip', action='append', default=[], choices=ORDER)
    result.add_argument('--from', dest='from_phase', choices=ORDER)
    result.add_argument('--phase', choices=ORDER, help=argparse.SUPPRESS)
    result.add_argument('--yes', action='store_true', help='Skip general prompt; typed account confirmation remains required')
    result.add_argument('--check-backend-empty', action='store_true', help=argparse.SUPPRESS)
    result.add_argument('--bootstrap', action='store_true', help='Delete backend after all module states are empty (separate confirmation)')
    result.add_argument('--retain-vpc', action='store_true', help='Keep VPC/default security group in state for independent resources')
    result.add_argument('--purge-deleted-secrets', action='store_true',
                        help='Permanently remove plan-selected secrets after Terraform deletion, without a recovery window; retained credentials are excluded')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.environment):
        raise TeardownError('Invalid environment')
    # Inherited Terraform flags could bypass refresh, add targets or change a plan.
    if any(key.startswith('TF_CLI_ARGS') and value for key, value in os.environ.items()):
        raise TeardownError('Unset TF_CLI_ARGS* before using the teardown orchestrator')
    os.environ.update(AWS_REGION=args.region, AWS_DEFAULT_REGION=args.region)
    identity = aws('sts', 'get-caller-identity')
    account = identity['Account']
    expected = os.environ.get('ADP_ACCOUNT_ID')
    if expected and expected != account:
        raise TeardownError('Configured account differs from active AWS identity')
    phases = [p for p in ORDER if p not in args.skip and
              (not args.from_phase or ORDER.index(p) >= ORDER.index(args.from_phase))]
    if args.phase:
        phases = [args.phase]
    print(f"Target: {account}, {args.region}, {args.environment}; caller: {identity['Arn']}", flush=True)
    if args.purge_deleted_secrets:
        print('Selected secrets will be permanently purged without recovery; retained credentials are excluded.', flush=True)
    if not args.dry_run and not args.phase and not args.check_backend_empty:
        print('Delete ADP runtime and data; keep backend, GitHub credentials and encryption keys.', flush=True)
        if input('Type the full AWS account ID to confirm: ').strip() != account:
            raise TeardownError('Account confirmation did not match')
    run = Run(args.root, account, args.region, args.environment, args.retain_vpc, args.purge_deleted_secrets)
    # Local lock avoids concurrent cleanup and checkpoint corruption. Terraform
    # retains its backend lock independently. A killed run leaves an inspectable lock.
    lock = run.directory / 'running.lock'
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise TeardownError(f'Teardown lock exists: {lock}; verify previous process exited before removing it')
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    try:
        run.load_states()
        if args.check_backend_empty:
            run.assert_backend_empty()
            print('All backend state files contain no managed resources')
            return
        run.bind_checkpoint()
        run.check_selection(phases)
        if 'platform' in phases:
            run.check_network()
            run.check_account_logging()
        if any(run.active(run.states[p]) for p in phases):
            run.setup_kube()
        # Validate every selected module before the first destructive action.
        for phase in phases:
            run.prepare(phase)
        if args.dry_run:
            print(f'Dry-run complete. No AWS/Kubernetes resources changed. Private evidence: {run.directory}')
            return
        journal = run.directory / 'journal.json'
        receipt = {'account': account, 'region': args.region, 'environment': args.environment,
                   'source': command(['git', 'rev-parse', 'HEAD'], cwd=args.root).strip(),
                   'engine_sha256': ENGINE_SHA256,
                   'retain_vpc': args.retain_vpc, 'purge_deleted_secrets': args.purge_deleted_secrets,
                   'selected_phases': phases, 'phases': {}}
        write_json(run.directory / f'run-{run.sequence}.json', receipt)
        receipt['quiesce'] = 'running'
        write_json(journal, receipt)
        try:
            run.quiesce(phases)
        except Exception:
            receipt['quiesce'] = 'failed'
            write_json(journal, receipt)
            raise
        receipt['quiesce'] = 'complete'
        write_json(journal, receipt)
        for phase in phases:
            receipt['phases'][phase] = 'running'
            write_json(journal, receipt)
            try:
                run.execute(phase)
            except Exception:
                receipt['phases'][phase] = 'failed'
                write_json(journal, receipt)
                raise
            receipt['phases'][phase] = 'complete'
            write_json(journal, receipt)
        if args.bootstrap:
            run.load_states()
            run.assert_backend_empty()
            if input('Type destroy-state to permanently delete the Terraform backend: ').strip() != 'destroy-state':
                raise TeardownError('Backend deletion not confirmed')
            command(['bash', args.root / 'platform/scripts/bootstrap-destroy.sh'], capture=False)
        print(f'Selected phases complete. Retention and private evidence: {run.directory}')
    finally:
        lock.unlink()


if __name__ == '__main__':
    try:
        main()
    except (TeardownError, OSError, ValueError, KeyError) as error:
        print(f'Teardown stopped: {error}', file=sys.stderr)
        sys.exit(1)
