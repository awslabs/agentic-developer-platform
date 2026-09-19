# Webhook WAF source restrictions and logging

The defaults preserve the existing stage: requests are allowed unless the
per-source-IP rate limit blocks them, WAF logging is off, and request sampling
retains its existing setting. These options do not activate themselves on merge.

To restrict sources, supply all three REGIONAL WAFv2 IP-set ARNs:

- `github_hooks_ipv4_ip_set_arn`: GitHub's published webhook IPv4 ranges.
- `github_hooks_ipv6_ip_set_arn`: GitHub's published webhook IPv6 ranges, in a
  different IP set because each WAF IP set has one address family.
- `internal_callers_ip_set_arn`: the outbound addresses used by internal callers,
  including agents calling `/agent/trigger` (usually NAT egress addresses).

All three absent means unrestricted. Supplying only some is a hard plan error.
The rate-limit Block rule runs at priority 1, before the terminating source Allow
rule at priority 2. Requests outside the configured sets then hit default Block.
Source approval does not replace the route's IAM, HMAC or shared-token checks.

The ACL covers the entire API stage, including `/gitlab` when enabled. Therefore,
when `gitlab_webhook_enabled` is true, source restriction also requires
`gitlab_webhook_ip_set_arns`. Supply the GitLab server's outbound IPv4/IPv6 sets
as appropriate. Explicitly reusing `internal_callers_ip_set_arn` in this list is
valid if GitLab uses the same NAT egress. The union applies to the whole stage;
the existing API resource policy and route authentication still apply.

The IP sets must already exist in the stage's region and account. Terraform
checks configuration completeness and ARN shape, not IP-set contents or actual
caller egress. Keep the sets current and verify every enabled caller before
activation; a stale set can block legitimate traffic.

`enable_waf_logging = true` creates a KMS-encrypted `aws-waf-logs-*` CloudWatch
group, with `waf_log_retention_days = 30` by default. Logs redact `Authorization`,
`Cookie`, `X-Api-Key`, `X-Amz-Security-Token` and `X-Gitlab-Token`. WAF log
redaction does not affect request samples, so enabling logging also disables
sampling at the ACL and every rule. Metrics remain enabled. When logging is off,
the pre-existing request sampling remains enabled. No other headers are redacted.

The regression tests use the complete production WAF files with a mocked AWS
provider. They validate declared rules and logging settings, not live IP-set
membership, rate counters or CloudWatch delivery:

```sh
python -m pytest modules/agent-factory/webhook-ingress/tests/test_webhook_waf.py -q -s
```

Terraform 1.14 or newer is required for this test harness. The production
preconditions remain compatible with this module's Terraform 1.5 minimum.

Before a rollout, follow the [deployment guide](../../../../docs/adp-platform-deployment/deploy-with-agent.md)
and the repository's webhook infrastructure deployment hold. Validate a scoped
plan, test delivery from every enabled caller, confirm unknown sources are
blocked and verify log delivery/redaction. Reverting the source options to empty
restores the prior default-Allow mode; logging can be switched independently.
