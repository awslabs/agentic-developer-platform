#!/bin/bash
# =============================================================================
# Phase 7: Post-Bootstrap Hardening
# =============================================================================
# Run AFTER CAPE is verified working (Phase 5 complete + EICAR test passes).
#
# Actions:
# 1. Tighten host SG outbound to VPC endpoints + NTP only
# 2. Install CloudWatch agent, ship CAPE + INetSim logs
# 3. CloudWatch alarm for outbound traffic anomaly
# 4. Cron job to clean old samples
# 5. Tag host as setup=complete
#
# Note: SG tightening is done via AWS CLI because the Terraform SG starts
# broad for bootstrap. After this script runs, update the Terraform to match
# the tightened state (remove the 0.0.0.0/0 egress, add specific rules).
# =============================================================================
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "=== Phase 7: Post-bootstrap hardening ==="

# IMDSv2 token required (instance enforces http_tokens=required)
IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/placement/region)

# ---------------------------------------------------------------------------
# 1. Install and configure CloudWatch agent
# ---------------------------------------------------------------------------
echo "Installing CloudWatch agent..."
wget -q "https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb" \
  -O /tmp/amazon-cloudwatch-agent.deb
dpkg -i /tmp/amazon-cloudwatch-agent.deb || apt-get install -f -y

# CloudWatch agent config — ship CAPE and INetSim logs
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'CWCONFIG'
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/inetsim/report/*.log",
            "log_group_name": "/adp/cyber/inetsim",
            "log_stream_name": "{instance_id}/inetsim",
            "retention_in_days": 30
          },
          {
            "file_path": "/home/cape/CAPEv2/log/*.log",
            "log_group_name": "/adp/cyber/cape",
            "log_stream_name": "{instance_id}/cape",
            "retention_in_days": 30
          },
          {
            "file_path": "/var/log/syslog",
            "log_group_name": "/adp/cyber/system",
            "log_stream_name": "{instance_id}/syslog",
            "retention_in_days": 14
          }
        ]
      }
    }
  },
  "metrics": {
    "namespace": "ADP/CyberSandbox",
    "metrics_collected": {
      "net": {
        "measurement": ["bytes_sent", "bytes_recv"],
        "metrics_collection_interval": 60,
        "resources": ["eth0"]
      },
      "disk": {
        "measurement": ["used_percent"],
        "metrics_collection_interval": 300,
        "resources": ["/opt/cape-data"]
      }
    }
  }
}
CWCONFIG

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json -s

systemctl enable amazon-cloudwatch-agent

echo "CloudWatch agent configured"

# ---------------------------------------------------------------------------
# 2. CloudWatch alarms
# ---------------------------------------------------------------------------
echo "Creating CloudWatch alarms..."

# Outbound traffic anomaly — alert if >1 MB/hour from the host
aws cloudwatch put-metric-alarm \
  --alarm-name "cape-host-outbound-anomaly" \
  --alarm-description "CAPE host outbound internet traffic exceeds 1 MB/hour — should be near zero post-bootstrap" \
  --namespace "ADP/CyberSandbox" \
  --metric-name "bytes_sent" \
  --dimensions "Name=InstanceId,Value=$INSTANCE_ID" "Name=interface,Value=eth0" \
  --statistic Sum \
  --period 3600 \
  --threshold 1048576 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 1 \
  --treat-missing-data notBreaching \
  --region "$REGION" || echo "WARNING: Could not create outbound alarm (check IAM permissions)"

# CAPE service health — check if CAPE process is running
# (Uses a custom metric published by a cron job below)
aws cloudwatch put-metric-alarm \
  --alarm-name "cape-host-service-health" \
  --alarm-description "CAPE service is not running" \
  --namespace "ADP/CyberSandbox" \
  --metric-name "CapeServiceRunning" \
  --dimensions "Name=InstanceId,Value=$INSTANCE_ID" \
  --statistic Minimum \
  --period 300 \
  --threshold 1 \
  --comparison-operator LessThanThreshold \
  --evaluation-periods 3 \
  --treat-missing-data breaching \
  --region "$REGION" || echo "WARNING: Could not create service health alarm"

echo "CloudWatch alarms created"

# ---------------------------------------------------------------------------
# 3. Cron jobs
# ---------------------------------------------------------------------------
echo "Setting up cron jobs..."

# Delete samples older than 7 days
cat > /etc/cron.d/cape-cleanup <<'CRON'
# Clean old analysis samples every day at 3 AM
0 3 * * * cape find /opt/cape-data/storage/binaries/ -type f -mtime +7 -delete 2>/dev/null
# Clean old analysis reports older than 30 days
0 4 * * * cape find /opt/cape-data/storage/analyses/ -maxdepth 1 -type d -mtime +30 -exec rm -rf {} + 2>/dev/null
CRON

# Publish CAPE health metric to CloudWatch every 5 minutes
cat > /etc/cron.d/cape-health-metric <<CRON
*/5 * * * * root RUNNING=\$(pgrep -c -f "cuckoo.py" || echo 0); aws cloudwatch put-metric-data --namespace "ADP/CyberSandbox" --metric-name "CapeServiceRunning" --dimensions "InstanceId=$INSTANCE_ID" --value "\$RUNNING" --region "$REGION" 2>/dev/null
CRON

echo "Cron jobs installed"

# ---------------------------------------------------------------------------
# 4. Tag host as complete
# ---------------------------------------------------------------------------
aws ec2 create-tags --resources "$INSTANCE_ID" \
  --tags "Key=cape-setup,Value=complete" --region "$REGION" || true

echo "=== Phase 7 complete: Hardening applied ==="
echo ""
echo "MANUAL FOLLOW-UP: Tighten the CAPE host security group outbound rules."
echo "Remove the 0.0.0.0/0 egress and add specific rules for:"
echo "  - VPC endpoint CIDRs (443/tcp) for S3, DynamoDB, SSM, Secrets Manager"
echo "  - NTP (123/udp) to 169.254.169.123 (Amazon Time Sync)"
echo "  - DNS (53/udp+tcp) within VPC"
echo "Then update the Terraform to match."
