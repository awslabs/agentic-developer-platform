# Verify DevOps Lambda and log access

After [disabled-space setup](devops-setup-activation.md), a Product owner can
register one exact Lambda function and CloudWatch log group in that same AWS
account and region. Verification is a separate purpose from space creation or
adoption and normal investigation consent. It does not run a task, enable native goals, open
the native UI, or create or change an IAM role.

The initial path requires a distinct, existing resource role. Its trust must
allow only `aidevops.amazonaws.com`, with `aws:SourceAccount` equal to the hosting
account and `aws:SourceArn` equal to the exact
`arn:aws:aidevops:REGION:ACCOUNT:agentspace/SPACE` ARN. Its inline policies must
allow only `lambda:GetFunction` on the selected function and
`logs:FilterLogEvents` on the selected log group. Additional identity grants,
managed policies, permissions boundaries, conditions, and denied permissions
need separate review; this path does not silently widen or rewrite them.

The selected ADP setup connection is separate from that resource role. During
the retained verification operation its broker session permits exact-space
reads and AWS association creation, read-only inspection/simulation of the
selected resource role, the exact Lambda/log reads, and `iam:PassRole` for only
that role with `iam:PassedToService=aidevops.amazonaws.com`. Every provider call
rechecks the current Product owner, configuration, connection, consent, durable
work, lease owner, and fence. It has no task, goal-write, deletion, unrelated
integration, or IAM-write authority.

## Supported API sequence

Use the Product/project DevOps service prefix:
`/v1/products/{product_id}/projects/{project_id}/services/devops`.
The configuration must be a disabled investigation draft with its original
ADP-created or exactly linked customer binding, a healthy Product AWS environment,
and native prevention disabled. A separately saved SSO policy remains unchanged.

1. Revoke earlier consent if still active. `POST /delegation-scope` with the
   binding ID, `for_control: true`,
   `control_action: "devops_resource_verification"`, and `resource_selection`.
   The selection contains `account_id`, `region`, `role_arn` for the resource
   role, `setup_role_arn` for the selected connection, its pinned credential
   `connection_ref`, `workload_kind: "lambda"`, the exact `resource_arn`, and
   the exact `telemetry_arn` ending in `:*`. Kubernetes fields are null.
2. Review the returned canonical selection and scope. `POST /delegations`
   supplies that same selection, binding, purpose, scope digest, explicit
   expiry, configuration `If-Match`, and a new `Idempotency-Key`.
3. `POST /binding-operations` supplies `action: "check"`, current
   `configuration_revision`, `binding_id`, and the reviewed canonical
   `resource_selection`, with binding `If-Match` and a new idempotency key.
   This persists normal setup intent/work before the worker contacts AWS.
4. Read the operation and binding. Failed verification retains a specific gap
   such as `resource_role_trust_outside_selected_space`,
   `resource_role_requires_inline_policy`, or `association_readback_mismatch`.
   Successful verification retains the actual association, policy snapshot,
   function/log read observations, and a fresh disabled-goal readback.
5. `GET /bindings/{binding_id}/resources` returns content-addressed
   `resource_refs` and `telemetry_refs`. Select those exact references in the
   account environment and manual trigger. The Product environment remains an
   environment; its account/region reference is not relabeled as a workload.
6. Explicitly activate the bounded manual configuration, with no budget setup required,
   and authorize a fresh ordinary execution grant for its new revision.
   Verification consent cannot authorize that run.

A successful registration reports `native_readiness: "verifying"`. It proves
current resource selection/access, not successful investigation, diagnosis,
release verification, or native readiness. Those still require their own
bounded live acceptance evidence.

Readback references expire after 15 minutes; the provider credential expiry is
retained separately and never extended. Changes to the selected Product
environment, connection, binding, policy observation, or reference revision
invalidate admission and subsequent execution authority. To refresh after
activation, disable the configuration, clear the old workload references and
manual trigger, review fresh resource-verification consent, and check the same
space again. The original creation receipt or successful exact-space link is
retained. Adopted spaces remain customer-owned, and verification never changes
goals or adds native deletion rights. Re-select the
new references and explicitly activate/authorize the new revision.

## Recovery and fixture prerequisites

A lost association response records `association_outcome_unknown`. Revoke the
old verification grant and authorize the next check generation. It first
reconciles the exact association and never blindly repeats an uncertain
creation. A conflicting association or a missing uncertain result needs
operator review; the worker does not modify or delete an existing association.

Lambda and log resources must already exist. A queue-only fixture with logging
disabled does not satisfy this prerequisite. Use a separately reviewed,
isolated investigation fixture with a precreated exact log group, minimal log
writes, deterministic synthetic failure/recovery inputs, and actual telemetry
readback. No fixture deployment or native task is implied by registration.

AWS documents the native ARN and association permissions in the
[DevOps Agent authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_devops-agent.html).
