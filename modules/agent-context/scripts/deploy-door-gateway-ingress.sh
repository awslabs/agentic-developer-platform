#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DOOR_MCP_HOST:-}${DOOR_MCP_CERTIFICATE_ARN:-}${DOOR_MCP_ALB_SOURCE_CIDR:-}" ]]; then
  exit 0
fi

python3 - <<'PY'
import ipaddress
import os
import re

host = os.environ.get("DOOR_MCP_HOST", "")
certificate = os.environ.get("DOOR_MCP_CERTIFICATE_ARN", "")
cidr = os.environ.get("DOOR_MCP_ALB_SOURCE_CIDR", "")
region = os.environ.get("AWS_REGION", "")
account = os.environ.get("ACCOUNT_ID", "")
if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or "." not in host:
    raise SystemExit("Door ingress needs a concrete DNS hostname")
if not re.fullmatch(rf"arn:aws:acm:{re.escape(region)}:{re.escape(account)}:certificate/[a-f0-9-]{{36}}", certificate):
    raise SystemExit("Door ingress needs a TLS certificate in the target account and region")
try:
    network = ipaddress.ip_network(cidr, strict=True)
except ValueError:
    raise SystemExit("Door ingress needs a valid private ALB source CIDR") from None
if not isinstance(network, ipaddress.IPv4Network) or not network.is_private or network.prefixlen < 16:
    raise SystemExit("Door ingress needs a bounded private IPv4 source CIDR")
PY

source "$(dirname "$0")/_common.sh"
template_file "$(dirname "$0")/../manifests/context-mcp-gateway-ingress.yaml" | kubectl apply -f -
