#!/bin/bash
# Only the disposable image builder starts this shutdown cleanup service.
set -euo pipefail
umask 077
[[ $EUID == 0 && -f /run/adp-gitlab-image-builder ]]
if systemctl is-active --quiet amazon-ssm-agent.service; then
  echo 'Builder cleanup requires the SSM agent to be stopped' >&2
  exit 1
fi
# The transport is now stopped. Persist the mask for the candidate without
# reloading systemd's unit graph during the shutdown transaction.
systemctl --no-reload mask amazon-ssm-agent.service
rm -rf /root/.aws /root/.docker /home/ec2-user/.aws /home/ec2-user/.docker
rm -rf /var/lib/amazon/ssm /var/log/amazon/ssm
rm -f /root/.ssh/authorized_keys /home/ec2-user/.ssh/authorized_keys /etc/ssh/ssh_host_*
# AL2023's cloud-init 22.2 lacks the newer --machine-id option.
cloud-init clean --logs --seed
# AL2023 initially mounts the root read-only. systemd needs the empty file
# already present so it can bind-mount a fresh identity during early boot.
install -m 0644 /dev/null /etc/machine-id
rm -f /var/lib/dbus/machine-id
rm -f /tmp/adp-job-host-runtime.json
install -d -m 0700 /var/lib/adp-image-build
printf '%s\n' 'ssm-stopped-and-builder-state-removed' > /var/lib/adp-image-build/sanitized
