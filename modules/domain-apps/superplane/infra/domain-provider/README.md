# Installation-owned workspace provider

Refs #7135. This root prepares one installation/workspace provider identity. It
creates no connection, authority record, workspace or approval. Personal AWS
connections still refuse platform-account roles. The separately protected owner
must enroll the exact provider before Gateway may use it.

## Preparation and ownership

Use a separate reviewed state key from `state_key_convention`. The platform state
bucket, lock table, Gateway role, management cluster and retained foundations are
references, never resources this state may delete.

1. With `execution = null`, prepare only the provider role, ExternalId secret and
   child-role boundary. The provider has **no execution policy**. This supplies a
   real provider RoleId for the separate `lifecycle-foundations` owner.
2. That owner prepares the retained key and three bootstrap actors against the
   real provider ARN/RoleId. Preserve its separate state and ownership.
3. Supply the real key, actor ARNs, backend and validation profile in `execution`. Also supply `node_image_ids`
   (the exact AMIs for the reviewed EKS releases) and `node_instance_types`.
   These describe the workspace nodes, independently of the validation profile.
   Review a fresh plan for the four policies/attachments and final child boundary.
   No inline policy or other managed attachment is supported on the provider.
4. The authority owner pins the provider ARN/RoleId, live trust, exact four policy
   ARNs and each DefaultVersionId/document, separate child-boundary version and
   document, and exact secret VersionId. Its normal Gateway admission checks still
   determine whether any human or paid operation is authorized.

An execution-policy output is configuration evidence, never runtime readiness.
General policy changes require separately reviewed revocation/re-enrollment; do not change
an active child boundary in place. Existing STS sessions can last up to 900 seconds.
The deployment owner must verify service-linked prerequisites before execution;
this role cannot create or change account-wide service-linked roles.
The exact original combined AssumeRole/TagSession trust has a restricted owner
repair in [provider authority](../../installation/PROVIDER-AUTHORITY.md#restricted-session-tag-trust-repair);
it preserves ExternalId on AssumeRole and every session-tag restriction.

## Action/resource/condition matrix

| Surface | Permission and restriction |
| --- | --- |
| Provider assumption | Exact Gateway principal and observed RoleId; dedicated ExternalId; exact beneficiary tag; only maintained operation/validator tags. Gateway proves current identity, lease, fence and sealed request. |
| Provider IAM | Create only four deterministic child-role names with the exact boundary and request ownership tags. Inline/trust edits remain inside that boundary. Attach only the three named EKS service policies to those roles. Pass only cluster/node/CNI roles to EKS. No provider policy/attachment writer. |
| Child maximum policy | Regional enumerated reads, exact cluster/nodegroup reads, reviewed ECR pulls, owned CNI interface operations and retained-key DescribeKey. Explicit deny on IAM, role chaining, secrets, state storage and SSM. No ELB/EBS mutation ceiling. |
| New VPC/network resources | Each create grant names only its new primary resource kind with OrgId/WorkspaceId request tags. Existing dependencies require both resource tags separately; a tagged CreateSubnet request cannot authorize a management VPC. |
| Existing network operations | Enumerated actions on resources already carrying both immutable ownership IDs. Tags cannot be erased or changed to another owner. No permission to first-tag an unrelated existing resource. |
| CNI interfaces | New ENIs require both request tags; existing subnet and SG authorization is independent. Generated EKS node SGs use the reserved `aws:eks:cluster-name` tag. Attach/mutate only owned interfaces/instances. `ADDITIONAL_ENI_TAGS` and launch-template primary-interface tags establish ownership at creation. |
| Default security group | Governed mode leaves AWS's unused, initially untagged default SG untouched. Every actual EKS/endpoint reference is explicit; nodes use the generated EKS managed SG. AWS removes the default with its owned VPC. Legacy configurations keep their existing default-SG address. Enabling this mode on an existing workspace needs a separate migration review. |
| EKS creation | AWS requires `Resource = "*"` and has no cluster-name/VPC/subnet condition for CreateCluster. Enforce exact owner tags, region, API authentication, private endpoint, disabled creator admin and retained encryption key. The sealed request, protected image/module and saved plan enforce actual name and owned network. **IAM alone does not isolate this service-linked network creation.** |
| Later EKS actions | Exact deterministic cluster/default-nodegroup/vpc-cni ARNs. Access-entry creation names only three retained actors; later grants target those entry ARN prefixes and the explicit EKS access-policy set. Existing operation-owned temporary grant journals/restrictions remain mandatory. |
| Foundation | Assume only three reviewed actors; Describe/GetKeyPolicy/CreateGrant only the retained workspace key. CreateGrant can delegate cryptographic operations on that key without an IAM recipient restriction. The protected approved runtime controls grant requests; the existing supplied-key verifier requires context-free simulation and a direct grant dry-run, while EKS encryption setup does not set GrantIsForAWSResource. This is key-scoped authority, not a recipient-only guarantee. Review the real retained key policy and plan separately; the key survives retirement. |
| State | Exact state object and exact DynamoDB lock/checksum keys. Governed backend discovery uses its workspace's deterministic `workspace_key_prefix`, still with only Terraform's default workspace; it does not list the bucket-wide `env:/` prefix. |
| EKS account prerequisite | `iam:GetRole` on the exact existing `eks-nodegroup.amazonaws.com/AWSServiceRoleForAmazonEKSNodegroup` ARN lets EKS validate managed node-group creation. The provider cannot create or mutate this shared service role. Existing enrolled providers use the restricted owner repair and refresh generation-bound validation before resuming. |
| Node launch check | EKS requires the caller to have `ec2:RunInstances` and atomic `ec2:CreateTags` when the node launch template contains tags. The operation grant requires an owned template and subnet, exact reviewed node AMIs/types, IMDSv2, and a same-account regional template reference. Security groups are regional; subnet/VPC compatibility and the owned-template checks apply to the complete request. New resource tags remain mandatory at actual tagging. EKS’s preliminary launch check omits request tags, so the hypothetical instance check relies on the existing template/subnet ownership. |
| Validation | Only the non-delivered `superplane-provider-validation` tagged session can permission-check RunInstances against one AMI/type/subnet/SG profile, without an instance profile. The maintained validator sends DryRun. AWS IAM has no DryRun-only permission; protected Gateway code is part of this boundary. Paid-operation sessions use the separate workspace-template grant above; AWS IAM cannot restrict it to DryRun, so the protected approved runtime remains part of the execution boundary. This proves EC2 profile permission, not all Terraform rights or physical capacity. |

The four exact managed policies avoid IAM's 10,240-byte aggregate inline-role
limit. Each has its own 6,144-byte plan precondition, and the Terraform test checks
that their union includes every intended statement exactly once. Shard membership
is not an authorization boundary; evaluate their union together with the child
boundary, session tags and protected execution path.

## Verification

Tagged bootstrap access-entry creation needs both `eks:CreateAccessEntry` on the
cluster and `eks:TagResource` on the new entry. Tagging is limited to the same
three actor entry ARNs as deletion; node entries and other clusters are excluded.
The create grant still requires the exact organization and workspace request tags.

For an existing enrollment, review and apply the provider Terraform plan, then use
`installation.provider_authority --repair-access-entry-tags-from` with the exact
predecessor document, authenticated policy snapshot, successor document and new
managed-policy version. This conditional repair adds only that tagging action and
advances the enrollment generation without extending its expiry or changing other
permissions. It does not reconcile a failed bootstrap journal or authorize a retry.

`terraform init -backend=false` and `terraform test` use Terraform 1.9.8 and the
pinned AWS provider. Tests use mocked AWS resources; they create no cloud objects.
They verify inert preparation, exact four policy limits/coverage and replaced
Gateway refusal. Workspace tests exercise all role boundaries, CNI/primary-ENI
tagging, unchanged legacy behavior and foreign policy refusal. Python contract
checks bind the explicit policy input to approval digests, reject supplied-network
and missing/foreign-boundary governed policies, and preserve exact saved-plan
backend identity. Live provisioning and workload acceptance are separate gates.

AWS authorization facts are from the public machine-readable references:

- https://servicereference.us-east-1.amazonaws.com/v1/ec2/ec2.json
- https://servicereference.us-east-1.amazonaws.com/v1/eks/eks.json
- https://servicereference.us-east-1.amazonaws.com/v1/iam/iam.json

The pinned CNI release's `pkg/awsutils/awsutils.go` applies additional ENI tags in
CreateNetworkInterface TagSpecifications. Its deployed add-on schema must expose
`ADDITIONAL_ENI_TAGS` as a string. This is prerequisite evidence, not live CNI
readiness.

Completed apply inventory requires region-bound `tag:GetResources`. AWS exposes this read on `*`; ownership filters and physical-resource validation remain enforced by the inventory reader. No tag mutation is granted.
