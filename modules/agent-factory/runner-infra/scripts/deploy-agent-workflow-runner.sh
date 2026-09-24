#!/usr/bin/env bash
# Install only the dedicated developer pool after its Terraform identity exists.
set -euo pipefail
infra_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../infrastructure" && pwd)"
: "${AGENT_WORKFLOW_RUNNER_IMAGE:?Set the reviewed runner image before installation}"
config_file=$(mktemp)
values_file=$(mktemp)
trap 'rm -f "$config_file" "$values_file"' EXIT
terraform -chdir="$infra_dir" output -json agent_workflow_runner > "$config_file"
python3 - "$config_file" "$values_file" <<'PY'
import json, os, re, sys
config = json.load(open(sys.argv[1]))
if not config or config['namespace'] != 'arc-runners' or config['service_account'] != 'agent-workflow-sa' or config['runner_label'] != 'arc-runner-agent':
    raise SystemExit('Enable and apply the dedicated Terraform identity first')
if not re.fullmatch(r'arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]+-agent-workflow', config['role_arn']):
    raise SystemExit('Refusing a non-dedicated runner role')
values = {
    'controllerServiceAccount': {'namespace': os.environ.get('ARC_CONTROLLER_NAMESPACE', 'arc-systems'),
                                 'name': os.environ.get('ARC_CONTROLLER_SERVICE_ACCOUNT', 'arc-gha-rs-controller')},
    'githubConfigUrl': config['github_config_url'], 'githubConfigSecret': 'github-arc-secret',
    'runnerScaleSetName': 'arc-runner-agent', 'minRunners': 0, 'maxRunners': 5,
    'template': {'spec': {'serviceAccountName': 'agent-workflow-sa', 'containers': [{
        'name': 'runner', 'image': os.environ['AGENT_WORKFLOW_RUNNER_IMAGE'],
        'command': ['/home/runner/run.sh'],
        'env': [{'name': 'GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT', 'value': '120'}],
        'resources': {'requests': {'cpu': '4', 'memory': '4Gi'}, 'limits': {'cpu': '4', 'memory': '8Gi'}}
    }]}}
}
json.dump(values, open(sys.argv[2], 'w'))
PY
# The existing ARC registration secret is never printed or copied to disk.
kubectl get secret github-arc-secret -n arc-runners -o name >/dev/null
role_arn=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["role_arn"])' "$config_file")
kubectl create serviceaccount agent-workflow-sa -n arc-runners --dry-run=client -o yaml | kubectl apply -f -
kubectl annotate serviceaccount agent-workflow-sa -n arc-runners "eks.amazonaws.com/role-arn=$role_arn" --overwrite
helm upgrade --install arc-runner-agent \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
  --version 0.13.1 --namespace arc-runners --values "$values_file" --wait --timeout 10m
