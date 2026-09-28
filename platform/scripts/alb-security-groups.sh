#!/usr/bin/env bash
# Fail closed when ELB briefly returns no security groups for a discovered ALB.
read_alb_security_groups() {
  local arn="$1" response="" validated="" attempt
  for attempt in 1 2 3 4 5; do
    response=$(aws elbv2 describe-load-balancers \
      --load-balancer-arns "$arn" --region "$AWS_REGION" \
      --query 'LoadBalancers[0].SecurityGroups' --output json 2>/dev/null) || response=""
    validated=$(python3 -c '
import json, re, sys
groups = json.loads(sys.argv[1])
if not isinstance(groups, list) or not groups or not all(
    isinstance(group, str) and re.fullmatch(r"sg-[0-9a-f]+", group) for group in groups
):
    sys.exit(1)
print(json.dumps(groups, separators=(",", ":")))
' "$response" 2>/dev/null) && { printf '%s\n' "$validated"; return 0; }
    [ "$attempt" -eq 5 ] || sleep 2
  done
  echo "ERROR: ALB $arn has no valid security groups after 5 reads; refusing to overwrite its cached rules" >&2
  return 1
}
