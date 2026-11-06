#!/usr/bin/env bash
set -euo pipefail

rpm -q openssh-server ec2-instance-connect amazon-ssm-agent >/dev/null
id ec2-user >/dev/null
test -x /opt/aws/bin/eic_run_authorized_keys

install -d -m 0755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/00-adp-dev-box.conf <<'EOF'
ListenAddress 127.0.0.1
PasswordAuthentication no
PermitRootLogin no
AllowUsers ec2-user
PubkeyAuthentication yes
LoginGraceTime 60
ClientAliveInterval 300
ClientAliveCountMax 2
MaxSessions 8
PermitTunnel no
EOF
chmod 0644 /etc/ssh/sshd_config.d/00-adp-dev-box.conf

cat > /etc/profile.d/adp-dev-box-idle.sh <<'EOF'
if [ -n "${TMUX:-}" ]; then
  unset TMOUT
else
  TMOUT=900
  readonly TMOUT
  export TMOUT
fi
EOF
chmod 0644 /etc/profile.d/adp-dev-box-idle.sh

/usr/sbin/sshd -t
settings="$(/usr/sbin/sshd -T -C user=ec2-user,addr=127.0.0.1,host=localhost)"
grep -Eq '^authorizedkeyscommand /opt/aws/bin/eic_run_authorized_keys( |$)' <<< "$settings"
grep -qx 'passwordauthentication no' <<< "$settings"
grep -qx 'permitrootlogin no' <<< "$settings"
if [ "$(grep -c '^listenaddress ' <<< "$settings")" -ne 1 ] || ! grep -Eq '^listenaddress 127\.0\.0\.1(:22)?$' <<< "$settings"; then
  exit 1
fi
systemctl enable sshd >/dev/null
