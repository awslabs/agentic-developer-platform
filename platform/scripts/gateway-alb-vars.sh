#!/usr/bin/env bash
# Keep every gateway Terraform pass on the same edge and internal-plane ALBs.
gateway_alb_vars() {
  GATEWAY_ALB_ARGS=()
  local arn dns groups prefix
  for prefix in internal-alb internal-plane-alb; do
    local parameters fields
    parameters=$(aws ssm get-parameters --names "/adp/$ENVIRONMENT/gateway/$prefix-arn" "/adp/$ENVIRONMENT/gateway/$prefix-dns" "/adp/$ENVIRONMENT/gateway/$prefix-security-group-ids" --output json --region "$AWS_REGION") \
      || fail "Cannot read $prefix configuration"
    fields=$(python3 -c '
import json, sys
p=json.load(sys.stdin)
values=p["Parameters"]
if not values:
    print("ABSENT")
else:
    if len(values)!=3 or p.get("InvalidParameters"):
        sys.exit("Incomplete ALB cache")
    by_name={v["Name"]:v["Value"] for v in values}
    prefix=sys.argv[1]
    arn,dns,groups=[next(v for k,v in by_name.items() if k.endswith(prefix+suffix)) for suffix in ("-arn","-dns","-security-group-ids")]
    if not arn or arn=="None" or not dns or dns=="None" or not json.loads(groups):
        sys.exit("Invalid ALB cache")
    print(arn+"\t"+dns+"\t"+json.dumps(json.loads(groups),separators=(",",":")))
' "$prefix" <<< "$parameters") || fail "Incomplete $prefix configuration"
    if [ "$fields" = ABSENT ]; then
      [ "$prefix" = internal-plane-alb ] && continue
      fail "Edge ALB is not cached; run wire-gateway-alb.sh"
    fi
    IFS=$'\t' read -r arn dns groups <<< "$fields"
    if [ "$prefix" = internal-alb ]; then
      GATEWAY_ALB_ARGS+=(-var "internal_alb_arn=$arn" -var "internal_alb_dns=$dns" -var "alb_security_group_ids=$groups" -var enable_vpc_origin=true)
    else
      GATEWAY_ALB_ARGS+=(-var "internal_plane_alb_arn=$arn" -var "internal_plane_alb_dns=$dns" -var "internal_plane_alb_security_group_ids=$groups")
    fi
  done
}
