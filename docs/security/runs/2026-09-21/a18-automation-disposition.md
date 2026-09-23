# A18 automation disposition — #5674

Both composite findings (`f-51343273-7105-40c2-90e7-8011a508e996` and
`f-c47fb5bc-a9e9-4927-972d-768be914c047`) were confirmed in the earlier automation
configuration. The implementation removes build administration, runner identity
mutation/assumption, broad secret reads, existing-service escalation and ordinary
runner Kubernetes deployment access. It does not claim live remediation.

- CodeBuild uses a separate role for each project, an explicit action ceiling,
  exact service SourceArn/SourceAccount trust and project-specific source paths.
  Arbitrary PR input can run only the nonpublishing gateway smoke project.
  Image-building privilege is restricted to the documented managed CodeBuild
  projects that actually need Docker; no privileged ARC sidecar is retained.
- Active and legacy runners share the same action/resource ceiling and explicit
  IAM/STS/tenant-vault denies. Repository onboarding consumes the rendered
  Terraform policy and reapplies its boundary on an authorized rerun. Legacy
  engine transport uses only inventoried exact secret ARNs and the nonsecret
  gateway endpoint parameter; transport is not switched by this change.
- Infrastructure, publishing, maintenance and real Terraform plans move to
  protected GitHub environments and dedicated tokenless deployment runners.
  Group restrictions bind exact main workflow refs, with independent environment
  approval and no admin bypass. Source must belong to reviewed main history.
  PR Terraform validation uses no state backend. Scanning has a separate scoped
  identity; its existing long-run duration and downstream transport are retained.
- Domain Kubernetes/migration lanes, CAPE image maintenance and live evaluations
  also use the protected deployment path. Existing database probes use an explicit
  IAM database username. Public-rule ingestion has its own public-prefix-only
  identity and stays off deployment nodes while parsing upstream rules.

The two external dependency dispositions were consumed, without duplicating
implementation or claiming new live evidence:

| Dependency | Accepted implementation and evidence |
|---|---|
| A19 #5684, controller RBAC | PR #5741, merge `aacfabfac15fad38f16cfc7bff6f5d51f10d7f00`: removed cluster-wide Secret access and unused heartbeat; 28 RBAC checks plus controller Go tests. |
| A24 #5687, customer role defaults | PR #5708, merge `4457a3b4535a48e6ab2d417e8826544a6ebda866`: scoped deployment role, four default-off capabilities, exact gateway/ExternalID trust; 66 role/template/loader checks plus cfn-lint. |

Validation commands are `terraform test` in runner-iam, CodeBuild and
platform/automation-infra; `pytest platform/automation-infra/tests`; the webhook
unit suite; and the affected scan/maintenance/worker Terraform contract tests.
The PR records final counts and CI results. All are offline or mocked policy
checks. They do not prove a live account's effective permissions.

Follow [the ordered cutover](../../../../platform/automation-infra/README.md)
under separate rollout authorization. Bootstrap and verify the protected identities
before narrowing old runners. Preserve exact existing engine transport inputs,
review custom IAM/RBAC bindings, and retain the operator identity through canaries.
No account, IAM, cluster, secret, engine budget or scan schedule is changed by
merging these files. No additional paid scan is required for code acceptance.
