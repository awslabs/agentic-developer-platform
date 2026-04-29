#!/bin/bash
# =============================================================================
# Phase 5b: Start CAPE daemons as systemd services
# =============================================================================
# Runs AFTER 01-cape-deps.sh (CAPE installed) and 02-sandbox-network.sh
# (sandbox net + INetSim ready). Replaces any placeholder HTTP servers on :8000
# with the real CAPE stack.
#
# CAPE has three long-running components:
#   cuckoo.py    — the main controller + REST API (serves /apiv2/*)
#   process.py   — result processor
#   rooter.py    — root-required network helper (iptables, routing)
#
# Each runs under the `cape` user in CAPE's venv, launched by systemd.
# =============================================================================
set -euo pipefail

echo "=== Phase 5b: Install + start CAPE systemd services ==="

CAPE_HOME=/home/cape/CAPEv2
VENV_PY=${CAPE_HOME}/venv/bin/python3

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: CAPE venv not found at $VENV_PY — run 01-cape-deps.sh first"
  exit 1
fi

# ---------------------------------------------------------------------------
# Kill any placeholder HTTP server on :8000 (from earlier bring-up attempts)
# ---------------------------------------------------------------------------
STUB_PID=$(pgrep -f 'python3 -m http.server 8000' || true)
if [ -n "$STUB_PID" ]; then
  echo "Killing placeholder http.server (pid=$STUB_PID)"
  kill -9 "$STUB_PID" || true
  sleep 2
fi

# ---------------------------------------------------------------------------
# CAPE web API wants a DB migration applied once. Idempotent.
# ---------------------------------------------------------------------------
su - cape -c "cd $CAPE_HOME/web && source ../venv/bin/activate && python manage.py migrate --noinput" || \
  echo "WARN: CAPE web migrate failed (non-fatal if schema already up to date)"

# ---------------------------------------------------------------------------
# systemd units
# ---------------------------------------------------------------------------

cat > /etc/systemd/system/cape.service <<'UNIT'
[Unit]
Description=CAPE Sandbox — main controller
After=network.target libvirtd.service postgresql.service mongod.service inetsim.service
Requires=libvirtd.service postgresql.service

[Service]
Type=simple
User=cape
Group=cape
WorkingDirectory=/home/cape/CAPEv2
ExecStart=/home/cape/CAPEv2/venv/bin/python3 /home/cape/CAPEv2/cuckoo.py
Restart=on-failure
RestartSec=10
StandardOutput=append:/home/cape/CAPEv2/log/cape.log
StandardError=append:/home/cape/CAPEv2/log/cape.log

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/cape-processor.service <<'UNIT'
[Unit]
Description=CAPE Sandbox — result processor
After=cape.service
Requires=cape.service

[Service]
Type=simple
User=cape
Group=cape
WorkingDirectory=/home/cape/CAPEv2
ExecStart=/home/cape/CAPEv2/venv/bin/python3 /home/cape/CAPEv2/utils/process.py -p7 auto
Restart=on-failure
RestartSec=10
StandardOutput=append:/home/cape/CAPEv2/log/processor.log
StandardError=append:/home/cape/CAPEv2/log/processor.log

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/cape-rooter.service <<'UNIT'
[Unit]
Description=CAPE Sandbox — rooter (root-privileged network helper)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/cape/CAPEv2
ExecStart=/home/cape/CAPEv2/venv/bin/python3 /home/cape/CAPEv2/utils/rooter.py -g cape
Restart=on-failure
RestartSec=10
StandardOutput=append:/home/cape/CAPEv2/log/rooter.log
StandardError=append:/home/cape/CAPEv2/log/rooter.log

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/cape-web.service <<'UNIT'
[Unit]
Description=CAPE Sandbox — REST API (Django on :8000)
After=cape.service
Requires=cape.service

[Service]
Type=simple
User=cape
Group=cape
WorkingDirectory=/home/cape/CAPEv2/web
ExecStart=/home/cape/CAPEv2/venv/bin/python3 manage.py runserver 0.0.0.0:8000 --insecure --noreload
Restart=on-failure
RestartSec=10
StandardOutput=append:/home/cape/CAPEv2/log/web.log
StandardError=append:/home/cape/CAPEv2/log/web.log

[Install]
WantedBy=multi-user.target
UNIT

mkdir -p /home/cape/CAPEv2/log
chown -R cape:cape /home/cape/CAPEv2/log

systemctl daemon-reload
systemctl enable cape-rooter.service cape.service cape-processor.service cape-web.service
systemctl restart cape-rooter.service
sleep 2
systemctl restart cape.service
sleep 5
systemctl restart cape-processor.service cape-web.service

# ---------------------------------------------------------------------------
# Smoke test — the real CAPE API returns JSON at /apiv2/tasks/list/
# ---------------------------------------------------------------------------
sleep 10
RESP=$(curl -sk --max-time 10 http://127.0.0.1:8000/apiv2/tasks/list/ || echo "")
if echo "$RESP" | head -c 1 | grep -q '{'; then
  echo "CAPE API smoke test: JSON response — OK"
else
  echo "ERROR: CAPE API did not return JSON. First 500 chars:"
  echo "$RESP" | head -c 500
  systemctl status cape.service cape-web.service --no-pager || true
  exit 1
fi

# ---------------------------------------------------------------------------
# Tag
# ---------------------------------------------------------------------------
IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
aws ec2 create-tags --resources "$INSTANCE_ID" \
  --tags "Key=cape-setup,Value=services-up" --region "$REGION" || true

echo "=== Phase 5b complete: CAPE services running ==="
echo "Verify with:"
echo "  systemctl is-active cape cape-processor cape-rooter cape-web"
echo "  curl -s http://127.0.0.1:8000/apiv2/tasks/list/ | head -c 200"
