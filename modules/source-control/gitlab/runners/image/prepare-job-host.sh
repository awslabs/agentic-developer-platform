#!/bin/bash
# Runs only on the disposable, trusted Packer builder. Never run on an ADP host.
set -euo pipefail
umask 077
[[ $EUID == 0 ]]
[[ "$ADP_BUILD_ACCOUNT" =~ ^[0-9]{12}$ ]]
[[ "$ADP_BUILD_REGION" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]]
[[ "$ADP_BOOTSTRAP_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ "$ADP_IMAGE_VERIFIER_SHA256" =~ ^[0-9a-f]{64}$ ]]
printf '%s  %s\n' "$ADP_BOOTSTRAP_SHA256" /tmp/adp-job-host-source | sha256sum --check --status
printf '%s  %s\n' "$ADP_IMAGE_VERIFIER_SHA256" /tmp/adp-job-image-verifier | sha256sum --check --status
python3 - "$ADP_JOB_IMAGE" "$ADP_HELPER_IMAGE" "$ADP_BUILD_ACCOUNT" "$ADP_BUILD_REGION" <<'PY'
import re, sys
for image, name in zip(sys.argv[1:3], ('job', 'helper')):
    prefix = f'{sys.argv[3]}.dkr.ecr.{sys.argv[4]}.amazonaws.com/adp-gitlab-runner-{name}'
    if not re.fullmatch(re.escape(prefix) + r':[0-9a-f]{40}@sha256:[0-9a-f]{64}', image):
        raise SystemExit('Unreviewed job/helper image scope')
PY
source /etc/os-release
[[ "$ID" == amzn && "$VERSION_ID" == 2023 ]]
dnf install -y docker ec2-instance-connect nftables python3 awscli-2 >/dev/null
test -x /opt/aws/bin/eic_run_authorized_keys
id ec2-instance-connect >/dev/null
usermod -aG docker ec2-user
systemctl enable --now docker.service
install -D -m 0500 /tmp/adp-job-image-verifier /usr/local/libexec/adp-job-images

# The builder profile can pull only these two image repositories. Authentication
# lives in tmpfs and is removed before the root volume is snapshotted.
task_docker_auth=$(mktemp -d /run/adp-image-auth.XXXXXX)
trap 'rm -rf -- "$task_docker_auth"' EXIT
task_registry="$ADP_BUILD_ACCOUNT.dkr.ecr.$ADP_BUILD_REGION.amazonaws.com"
aws ecr get-login-password --region "$ADP_BUILD_REGION" |
  docker --config "$task_docker_auth" login --username AWS --password-stdin "$task_registry" >/dev/null 2>&1
docker --config "$task_docker_auth" pull "$ADP_JOB_IMAGE" >/dev/null
docker --config "$task_docker_auth" pull "$ADP_HELPER_IMAGE" >/dev/null
rm -rf -- "$task_docker_auth"
trap - EXIT
systemctl restart docker.service
python3 /usr/local/libexec/adp-job-images "$ADP_JOB_IMAGE" "$ADP_HELPER_IMAGE"
python3 - <<'PY'
import json, subprocess
from pathlib import Path
def output(*args):
    return subprocess.check_output(args, text=True).strip()
record = {'docker_engine_version': output('docker', 'version', '--format', '{{.Server.Version}}'),
          'eic_package': output('rpm', '-q', 'ec2-instance-connect'),
          'docker_package': output('rpm', '-q', 'docker'),
          'nft_package': output('rpm', '-q', 'nftables'),
          'image_check_after_docker_restart': True,
          'post_snapshot_boot_verified': False}
path = Path('/tmp/adp-job-host-runtime.json')
path.write_text(json.dumps(record, indent=2) + '\n')
path.chmod(0o644)
PY

# Bake last: enforcement starts before networking on every subsequent boot.
# Do not open destinations or fabricate a job-ready marker on the image builder.
bash /tmp/adp-job-host-source --bake
# The observer becomes ready only after subscribing to Docker's event stream,
# before SSH allows the manager to create its first container. It uses the
# EC2 serial console and a private root journal, without instance credentials.
install -d -m 0700 /usr/local/libexec/adp-job-observer /var/lib/adp-job-observations
install -m 0500 /tmp/adp-host-observation.py /usr/local/libexec/adp-job-observer/host_observation.py
install -m 0500 /tmp/adp-host-journal.py /usr/local/libexec/adp-job-observer/host_journal.py
install -m 0644 /tmp/adp-job-observer.service /etc/systemd/system/adp-job-observer.service
cat > /etc/systemd/system/sshd.service.d/20-adp-job-observer.conf <<'OBSERVER'
[Unit]
Requires=adp-job-observer.service
After=adp-job-observer.service
BindsTo=adp-job-observer.service
OBSERVER
systemctl enable adp-job-observer.service
rm -f /tmp/adp-host-observation.py /tmp/adp-host-journal.py /tmp/adp-job-observer.service
# Keep the active unit loaded until shutdown: masking it here can discard the
# ordering that makes the SSM writer stop before the cleanup service.
systemctl disable amazon-ssm-agent.service
install -m 0500 /tmp/adp-image-builder-cleanup /usr/local/libexec/adp-image-builder-cleanup
install -m 0644 /tmp/adp-image-builder-cleanup.service /etc/systemd/system/adp-image-builder-cleanup.service
touch /run/adp-gitlab-image-builder
systemctl daemon-reload
systemctl start adp-image-builder-cleanup.service
rm -f /tmp/adp-job-host-source /tmp/adp-job-image-verifier
rm -f /tmp/adp-image-builder-cleanup /tmp/adp-image-builder-cleanup.service
# Packer downloads the runtime receipt while the SSM channel remains alive.
# Its normal graceful instance stop then stops SSM before cleanup runs, so
# the agent cannot rewrite credentials after deletion. The AMI must still
# pass post-snapshot inspection of the marker and absence of identity/cache.
