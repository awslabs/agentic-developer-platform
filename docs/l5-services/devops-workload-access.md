# DevOps access to an existing Lambda workload

`modules/L5-services/infra/devops-workload-access/` supplies operator-managed
prerequisites for investigating an existing Lambda and its CloudWatch log group.
It has independent Terraform state and does not modify the workload, log group,
existing connection trust or other connection policies. It is separate from the
synthetic investigation fixture and the general provider-access root.

Select the verified account, region, connection role, exact unqualified Lambda
ARN and log-group ARN ending in `:*`. Keep populated variables, plans and state
private. The provider rejects a different account; the plan rejects a function
or log group outside the selected account and region.

For a new space, first retain a disabled manual-investigation draft, owner setup
consent and create operation through ADP. Supply that operation's actual
`SpaceSelection.fingerprint()` as `setup_fingerprint`. Review the saved plan
before applying. Creation requires that exact request tag; reads and goal
changes require its matching resource tag. Discovery is region-scoped. This
stage creates no resource reader and permits no investigation submission.

After ADP reports the actual created space, set `native_space_id` to its returned
ID and review/apply a second plan. This replaces creation authority with access
to the exact space, its AWS association and its investigation tasks. It creates
one distinct resource reader with only `lambda:GetFunction` and
`logs:FilterLogEvents` for the selected resources. Trust is restricted to DevOps
Agent, the hosting account and the exact native space ARN. The connection can
inspect and pass only this reader to DevOps Agent. Goal changes are absent from
this stage. For an already linked space, begin directly with the workload stage.

Complete [resource verification](devops-resource-verification.md) through ADP,
select the returned resource references, activate manual investigation and grant
execution consent. The IAM plan alone does not authorize a run or prove native
readiness. Manual investigation requires no budget setup; its cost value is a
planning estimate, not an AWS billing cap. Current access, consent, time limits
and duplicate-submission checks still apply.

The worker rollout/resume path remains the canonical selected-service lifecycle
workflow. Verify the real investigation result and retained artifact before
claiming completion. An existing space's schedules remain unchanged when a
separate manual-investigation space is used.
