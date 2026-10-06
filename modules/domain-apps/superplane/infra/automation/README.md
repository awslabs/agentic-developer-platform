# Superplane automation ownership

Superplane owns these Terraform definitions. The independent
`platform/automation-infra` root composes them in its existing backend; the domain
installer does not manage automation identities or enable these capabilities.

- `build-dispatch` exports the exact optional paid-worker project/repository
  enrollment and source/dispatch IAM statements. It creates no resources. The
  platform owns the shared trusted build role and its single inline policy, and
  enforces this module's canonical publisher identity check before attaching it.
- `skypilot-deployment` owns the dedicated deployment role, its SSM/cluster
  discovery policy, EKS access entry and namespace-scoped access association.
  It retains the protected `adp-skypilot-deploy-<environment>` OIDC subject and
  cannot deploy the Superplane control plane. Namespace-scoped access administers
  workloads, but does not authorize writes to the cluster-scoped Namespace
  object. Namespace creation and security labels require a separately authorized
  installer/bootstrap path.

The wrapper retains all existing public inputs and default-off behavior. The
platform's explicit selected build inventory is still an authorization input:
it must not automatically enroll every project when the app manifest grows.
Project definitions remain app-owned in `codebuild/projects.json`; selected
shared-dispatcher inventories are composition configuration, not provisioning.

## Existing state compatibility

The automation root has four whole-resource `moved` blocks from
`<resource-type>.skypilot_deployment` to
`module.superplane_skypilot_deployment.<resource-type>.skypilot_deployment` for
`aws_iam_role`, `aws_iam_role_policy`, `aws_eks_access_entry` and
`aws_eks_access_policy_association`. Whole-resource moves include existing `[0]`
instances. Keep these blocks for installations upgrading from earlier releases.

Keep the same automation backend, inputs, provider configuration and enabled
flags. A reviewed plan should show address moves only for these resources, with
unchanged names, trust, actions, resource scopes and namespace. Investigate any
deletion, replacement or permission change before applying. No `state rm`,
import, new backend or cross-state transfer is needed for this source move.
The shared build role/policy addresses do not change.

This source change neither proves a live state upgrade nor transfers the four
standard CodeBuild projects. Their separate ownership procedure is described
in [BUILD-OWNERSHIP-MIGRATION.md](../BUILD-OWNERSHIP-MIGRATION.md).

Automation trust CI includes this directory in its triggers and exercises the
modules through the platform root with mocked AWS providers, including disabled
capabilities, exact grants, namespace scope and protected publisher rejection.
