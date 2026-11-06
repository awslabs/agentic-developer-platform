# Shared artifact publication authority

The shared L5 release publishes a wheel, worker archive and immutable manifest,
then reads their exact S3 versions to verify the uploaded bytes. The default
gateway deployment role cannot write those objects. Configure this separately
from worker execution and live qualification.

The automation infrastructure provides `enable_l5_artifact_publication`, default
`false`. With the existing gateway deployment profile enabled, it adds only
`s3:PutObject`, `s3:GetObject` and `s3:GetObjectVersion` on
`adp-<environment>-deployment-evidence-<account>/l5/security/*`. Security and
DevOps share that historical package prefix. The role's permissions boundary
admits the same objects. Other bucket contents, bucket configuration, object
deletion, worker execution and IAM changes remain outside this grant.

To configure an existing environment:

1. Verify the target account and the existing deployment-evidence bucket. The
   bucket must have versioning enabled and all four S3 public-access blocks set.
2. Retain the environment's existing automation inputs and enable
   `enable_l5_artifact_publication` alongside
   `enable_gateway_backend_deployment`. Review the saved Terraform plan. The
   expected changes are the additional inline policy and the bounded object
   exceptions in the gateway role's permissions boundary.
3. Apply the reviewed automation-infrastructure plan using the existing operator
   deployment procedure. This changes publication authority; it requires no
   gateway/frontend rollout and creates no new bucket or deployment role.
4. Set `ADP_L5_RELEASE_BUCKET` in the existing protected
   `adp-gateway-deploy-<environment>` GitHub environment to the
   `l5_artifact_publication_bucket` output. Keep `ADP_DEPLOY_ROLE_ARN` bound to its
   existing gateway trusted deployment role.
5. At a settled delivery-flow boundary, bind the accepted delivery contract's
   `job_mutation_role_arn` to that same role through the native amendment API.
   Preserve budgets, expiry, graph history and required qualification evidence.

Publication uses the existing GitHub OIDC trust for the exact repository and
protected environment. This opt-in does not add another trust principal or give
the workflow authority to create or configure Lambda functions. It also does
not mark runtime deployment, live provider results, recovery or cleanup complete.

The automated Terraform tests verify both the disabled default and the enabled
object scope, including the policy-size ceiling and unchanged protected trust.
