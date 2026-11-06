# Pinned SkyPilot artifact pull access

This optional app-owned root grants one existing consumer node IAM role pull
access to the existing `adp-superplane-skypilot` repository. It owns only the ECR
repository policy. Repository lifecycle, images, tags and encryption remain with
the existing control-plane owner. The consumer operator is assumed only for
identity reads and receives no repository grant.

The native cluster installer queries registry provenance for its application
images and excludes SkyPilot from those queries. Its probe and Deployment use
the locked SkyPilot reference through the node image puller. Consequently only
`BatchCheckLayerAvailability`, `BatchGetImage` and `GetDownloadUrlForLayer` are
granted. The node's existing identity policy must separately permit ECR pulls and
`GetAuthorizationToken`; this root adds no identity policy or token permission.
ECR repository grants cover images in that repository, not individual digests.
The unchanged release lock and runtime checks continue to select the exact image.

The policy omits `Resource`, following the
[pinned provider's repository-policy example](https://github.com/hashicorp/terraform-provider-aws/blob/v6.65.0/website/docs/r/ecr_repository_policy.html.markdown).
ECR scopes it to the resource's exact `repository` argument and selected registry;
the module independently checks that repository ARN. An explicit repository ARN
in the policy's `Resource` member is refused by ECR. No wildcard resource or
additional repository is introduced.

Supply private `source_registry` and `consumer` objects with independently read
account, role ARN and immutable RoleId values. Verify that the consumer cluster
actually uses the selected node role. Before first planning and immediately
before approved apply, the source owner must confirm `GetRepositoryPolicy`
returns `RepositoryPolicyNotFoundException`, inspect the existing repository
state owner, and confirm the new backend is absent. Do not import or overwrite
an existing policy. ECR has no conditional create-policy API; coordinate policy
ownership through this single Terraform state and the source repository owner.

Use the source account's existing encrypted state bucket and canonical
`adp-terraform-locks` DynamoDB table. Assign one stable backend key for this
repository policy, such as
`<environment>/modules/superplane-artifact-pull-access/skypilot/terraform.tfstate`.
Do not create one state per consumer: an ECR repository has one policy document.
Keep backend configuration, variables, saved plans and receipts outside Git.

```bash
terraform init -input=false -backend-config=/private/artifact-backend.hcl
terraform plan -input=false -lock-timeout=60s \
  -var-file=/private/artifact-access.tfvars.json -out=/private/artifact.tfplan
terraform show -json /private/artifact.tfplan > /private/artifact-plan.json
```

First installation must plan exactly one repository-policy creation, with no
repository, IAM, image, delete or replacement changes. Review its exact principal,
three actions and repository ARN. Recheck source/consumer identities and policy
absence, then apply only the approved saved plan with normal state locking.
Afterward verify the live policy and a native digest-pinned cluster pull. The
policy is protected from accidental destroy; revocation requires a separate
reviewed change. No build, application deployment or workload admission is part
of this root.

Offline checks use mocked providers:

```bash
terraform init -backend=false -input=false
terraform validate
terraform test
```
