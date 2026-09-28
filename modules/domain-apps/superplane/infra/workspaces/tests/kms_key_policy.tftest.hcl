# =============================================================================
# The workspace KMS key actually permits its three consumers, and node root volumes are
# really encrypted with it — Issue #5532 (w6-09), review findings W9-01 and W9-02.
# =============================================================================
# WHY THIS FILE EXISTS SEPARATELY FROM cluster_security.tftest.hcl
#
# Because of what it can assert that the other suites structurally cannot.
#
# Every other suite here mocks `data.aws_iam_policy_document` — it must, since a provider-free
# run has no provider to render one. Its `defaults` replace the `json` attribute with
# `{"Version":"2012-10-17","Statement":[]}`: an EMPTY statement list. So any assertion about a
# policy built from that data source passes identically whether the real policy grants exactly
# the right access or grants nothing whatsoever. The review named this directly: "Validate the
# real rendered policy; the current mock replaces every policy document with an empty statement
# list."
#
# The workspace key's policy is therefore built with `jsonencode` in eks.tf rather than through
# that data source. `jsonencode` is evaluated by Terraform, not the provider, so the rendered
# document is a KNOWN value in the plan and the assertions below read its actual statements.
# That is also why this file declares no `mock_data` for aws_iam_policy_document: nothing here
# depends on it.
#
# WHAT WOULD HAPPEN WITHOUT THE CODE THESE TESTS COVER
#
#   W9-01: a KMS key with no policy gets AWS's default, which grants no SERVICE access.
#          CloudWatch Logs cannot use it, so `aws_cloudwatch_log_group.cluster` — which sets
#          kms_key_id to this key — is rejected with InvalidParameterException. A DEFAULT
#          INSTALL cannot be created, and encrypted audit logging never starts.
#   W9-02: without a launch template, EKS's default node template creates a 20 GiB root volume
#          that is either unencrypted or encrypted with the ACCOUNT's default key. The module
#          claimed workspace-key encryption in three places and implemented it in none.
#
# Each run below states which of those it would catch.
# =============================================================================

mock_provider "aws" {
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/workspace-provisioner" }
  }

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }

  # This mock is here ONLY so the IAM roles in iam.tf have a syntactically valid
  # assume_role_policy — without it the provider rejects an empty string with "not a JSON
  # object" and every run in this file errors before reaching an assertion.
  #
  # It does NOT weaken anything below. The assertions in this file read
  # `aws_kms_key.workspace[0].policy`, which eks.tf builds with `jsonencode` precisely so it is
  # rendered by Terraform rather than by the provider. Nothing asserted here passes through this
  # empty-statement default — which is exactly the substitution that made the module's other
  # suites unable to see whether the key policy granted anything at all.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  # The two AWS-assigned identifiers the W9-02 run asserts on. See the note below on why these
  # are mock_resource defaults rather than `override_resource { override_during = plan }`.
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-east-1:111122223333:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
      key_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    }
  }

  mock_resource "aws_launch_template" {
    defaults = {
      id             = "lt-0abcdef1234567890"
      latest_version = 1
    }
  }

  # Not an assertion target — a well-formedness requirement of the `apply` run below. Terraform's
  # mock provider invents a random string for every computed attribute it is not given, and
  # `aws_eks_cluster.role_arn` is validated for ARN SHAPE by the provider before any API call, so
  # an invented value like "23ccyzai" aborts the run with `invalid ARN: arn: invalid prefix`.
  # Nothing in this file asserts on a role ARN; the role-scoping claims live in
  # test_least_privilege.py and admin_trust.tftest.hcl, which are unaffected by this default.
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::111122223333:role/mock-workspace-role"
    }
  }

  # Also well-formedness rather than assertion targets. The mock provider returns an EMPTY list
  # for a computed nested block it is not given, and eks.tf/outputs.tf index into both of these
  # (`identity[0].oidc[0].issuer` for the OIDC trust anchor, `certificate_authority[0].data` for
  # the cluster output), which fails with "Invalid index ... collection has no elements". The
  # OIDC issuer and CA are asserted in admin_trust.tftest.hcl and test_least_privilege.py.
  mock_resource "aws_eks_cluster" {
    defaults = {
      identity = [{
        oidc = [{
          issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/MOCKED0000000000000000000000000"
        }]
      }]
      certificate_authority = [{
        data = "TU9DS0VEQ0VSVElGSUNBVEU="
      }]
    }
  }
}

mock_provider "tls" {
  mock_data "tls_certificate" {
    defaults = {
      certificates = [
        {
          sha1_fingerprint = "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"
        }
      ]
    }
  }
}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
    arn        = "arn:aws:sts::111122223333:assumed-role/workspace-provisioner/test"
  }
}

override_data {
  target = data.aws_partition.current
  values = {
    partition = "aws"
  }
}

# ---------------------------------------------------------------------------
# Two AWS-assigned identifiers, made KNOWN so the W9-02 claims can be asserted.
#
# `aws_kms_key.workspace[0].arn` and `aws_launch_template.node.id` are assigned by AWS at
# create time. An assertion referring to them from a `command = plan` run fails with "Unknown
# condition value" rather than passing or failing on its merits.
#
# The alternative — asserting only on values that happen to be literals — would drop exactly
# the two claims the review demands proof of: that the node volume uses THE REVIEWED KEY, and
# that the node group REFERENCES the template.
#
# WHY THIS IS NOT `override_resource { override_during = plan }`
#
# That argument does not exist in Terraform 1.9.8, which is what `superplane-infra-plan.yml`
# pins and therefore what gates merge. On 1.9.8 it is not a failing assertion but an
# `Unsupported argument` error during `terraform init`, so the WHOLE FILE fails to load and
# every run in it is reported as not executed — the shape the CI test-count guard exists to
# catch. Run 35508993241 failed exactly there.
#
# So the identifiers come from `mock_resource` defaults on the mocked provider instead, and the
# one run needing them uses `command = apply`. Verified by direct experiment on both 1.9.8 and
# 1.15.3: `mock_resource` defaults do NOT resolve under `command = plan` on either version, and
# DO resolve under `command = apply`, which for a fully mocked provider performs no API call and
# needs no credential — confirmed under this workflow's own credential-stripped environment.
#
# This supplies only the two opaque identifiers. It does not stub the resources or their other
# attributes: encryption, key wiring, device name, size, IMDSv2 and tag_specifications are all
# still the module's own values, and the key policy is still rendered by `jsonencode` in eks.tf.
# ---------------------------------------------------------------------------

variables {
  org_id             = "test-org"
  environment        = "dev"
  workspace_name     = "tenant-alpha"
  workspace_id       = "tenant-alpha"
  account_id         = "111122223333"
  aws_region         = "us-east-1"
  cluster_version    = "1.31"
  networking_mode    = "owned"
  vpc_cidr           = "10.42.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
  cost_center        = "superplane-dev"
}

# ---------------------------------------------------------------------------
# W9-01: the key permits CloudWatch Logs, scoped to THIS log group
# ---------------------------------------------------------------------------
run "the_key_policy_grants_cloudwatch_logs_access_scoped_to_this_clusters_log_group" {
  command = plan

  # The statement must exist at all. This is the assertion that fails on the reviewed head,
  # where `policy` was absent from aws_kms_key.workspace entirely and a default install could
  # not create its log group.
  assert {
    condition = length([
      for statement in jsondecode(aws_kms_key.workspace[0].policy).Statement :
      statement if try(statement.Principal.Service, "") == "logs.us-east-1.amazonaws.com"
    ]) == 1
    error_message = "The workspace KMS key's policy has no statement for logs.us-east-1.amazonaws.com. KMS's default key policy grants no AWS service access, so CloudWatch Logs cannot encrypt with this key and aws_cloudwatch_log_group.cluster is rejected with InvalidParameterException. A DEFAULT INSTALL OF THIS MODULE CANNOT BE CREATED without this statement, and the encrypted audit trail never begins. Exactly one statement is expected — two would mean a broader duplicate was added alongside the scoped one."
  }

  # REGIONAL principal, not the global `logs.amazonaws.com`. CloudWatch Logs calls KMS with the
  # regional service principal; a global one silently matches nothing.
  assert {
    condition = length([
      for statement in jsondecode(aws_kms_key.workspace[0].policy).Statement :
      statement if try(statement.Principal.Service, "") == "logs.amazonaws.com"
    ]) == 0
    error_message = "The key policy names the GLOBAL logs.amazonaws.com principal. CloudWatch Logs uses the REGIONAL principal (logs.<region>.amazonaws.com) when calling KMS, so a global principal grants nothing while appearing to grant access — the log group still fails to create."
  }

  # The scoping condition. Without it the grant reads "CloudWatch Logs may use this key", which
  # would let the regional Logs principal — shared by every log group in the account, including
  # a tenant's own — use this workspace's key for any group. The review requires the grant be
  # narrowed to "the exact log-group encryption context".
  assert {
    condition = length([
      for statement in jsondecode(aws_kms_key.workspace[0].policy).Statement :
      statement if try(statement.Principal.Service, "") == "logs.us-east-1.amazonaws.com"
      && try(statement.Condition.ArnEquals["kms:EncryptionContext:aws:logs:arn"], "") == "arn:aws:logs:us-east-1:111122223333:log-group:/aws/eks/adp-dev-spw-970b320a868d197402321c9d69957998/cluster"
    ]) == 1
    error_message = "The CloudWatch Logs statement is not conditioned on this workspace's exact log-group encryption context. Unconditioned, it permits the regional Logs service principal — which every log group in the account shares, including the tenant's own — to use this workspace's key for ANY log group. The condition must pin kms:EncryptionContext:aws:logs:arn to arn:aws:logs:us-east-1:111122223333:log-group:/aws/eks/adp-dev-spw-970b320a868d197402321c9d69957998/cluster."
  }

  # Decrypt as well as Encrypt: without it the group accepts writes and errors on every read,
  # which is an audit trail that exists and cannot be consulted.
  #
  # `flatten([statement.Action])` because an IAM Action may be a single string or a list, and
  # `contains` rejects a string argument. Terraform 1.15 short-circuits `&&` and never evaluates
  # `contains` for the statements whose Principal does not match; 1.9.8 — the version CI pins —
  # evaluates both operands, so on the account-root statement (`Action = "kms:*"`) the bare form
  # aborts the whole run with "argument must be list, tuple, or set". Normalising to a list is
  # version-independent and asserts the same thing.
  assert {
    condition = length([
      for statement in jsondecode(aws_kms_key.workspace[0].policy).Statement :
      statement if try(statement.Principal.Service, "") == "logs.us-east-1.amazonaws.com"
      && contains(flatten([statement.Action]), "kms:Decrypt")
      && contains(flatten([statement.Action]), "kms:Encrypt")
      && contains(flatten([statement.Action]), "kms:DescribeKey")
      && contains(flatten([statement.Action]), "kms:GenerateDataKey*")
    ]) == 1
    error_message = "The CloudWatch Logs statement is missing one of kms:Encrypt, kms:Decrypt, kms:DescribeKey or kms:GenerateDataKey*. Omitting Decrypt in particular produces a log group that accepts writes and fails every read — an audit trail that exists but cannot be consulted."
  }
}

# ---------------------------------------------------------------------------
# W9-01: the key stays administrable
# ---------------------------------------------------------------------------
run "the_key_retains_an_administration_and_recovery_path" {
  command = plan

  # Attaching a policy REPLACES AWS's default entirely. A policy containing only service grants
  # produces a key nobody can schedule for deletion, re-enable or inspect — including during the
  # incident where that is the required action. The review asks to "preserve a usable key
  # administration/recovery policy".
  assert {
    condition = length([
      for statement in jsondecode(aws_kms_key.workspace[0].policy).Statement :
      statement if try(statement.Principal.AWS, "") == "arn:aws:iam::111122223333:root"
      && try(statement.Action, "") == "kms:*"
    ]) == 1
    error_message = "The key policy has no administration statement delegating to the workspace account's IAM. Attaching a policy replaces KMS's default policy completely, so without this the key is UNMANAGEABLE: no principal can schedule deletion, re-enable it, or rotate it — including during an incident. In a KEY policy the account root principal means 'defer to this account's IAM', which is the documented idiom and is not the bare-account trust-policy grant iam.tf refuses."
  }
}

# ---------------------------------------------------------------------------
# W9-02: node root volumes, and the key path they need
# ---------------------------------------------------------------------------
run "node_root_volumes_are_encrypted_with_the_reviewed_key_and_bounded" {
  # `apply`, not `plan`, and against the mocked provider declared above — this is the one run
  # whose assertions need `aws_kms_key.workspace[0].arn` and `aws_launch_template.node.id`, which
  # are AWS-assigned and therefore unknown during plan. No API call and no credential: every
  # provider in this file is mocked. See the note above the `variables` block.
  command = apply

  # The launch template must exist. Fails on the reviewed head, where no launch template was
  # declared at all and the encryption claim rested on nothing.
  assert {
    condition     = aws_launch_template.node.block_device_mappings[0].ebs[0].encrypted == "true"
    error_message = "The node launch template does not set encrypted = true on its root volume. EKS's default node template leaves node disks unencrypted, or encrypted with the ACCOUNT's default key if encryption-by-default happens to be on — neither of which is the workspace-scoped key this module's variables, outputs and PR description all claim. Node root volumes hold pulled images, the kubelet's cached Secret material and tenant scratch data."
  }

  # The REVIEWED key specifically — not merely "some encryption". An account-default key is
  # shared with everything else in the account, so it does not separate this tenant's data.
  assert {
    condition     = aws_launch_template.node.block_device_mappings[0].ebs[0].kms_key_id == aws_kms_key.workspace[0].arn
    error_message = "The node root volume is not encrypted with this workspace's KMS key. Encryption with the account's default key is not the promised property: that key is shared with every other resource in the account, so it does not separate this workspace's cached data from anything else."
  }

  # The root device name. A mapping under any other name is accepted by EC2 and attaches an
  # ADDITIONAL volume, leaving the real root volume on the unencrypted default — a plan that
  # looks correct and an instance that is not.
  assert {
    condition     = aws_launch_template.node.block_device_mappings[0].device_name == "/dev/xvda"
    error_message = "The encrypted block device mapping is not on /dev/xvda, the root device for EKS's Amazon Linux node AMIs. EC2 accepts a mapping under another device name and attaches it as an EXTRA volume, leaving the actual root volume on the unencrypted default — so the plan reads as encrypted while the instance is not."
  }

  # Bounded, which is also what lets the cost guard price node storage instead of listing it as
  # unboundable.
  assert {
    condition     = aws_launch_template.node.block_device_mappings[0].ebs[0].volume_size == 50
    error_message = "The node root volume size is not the reviewed var.node_volume_size default of 50 GiB. A reviewed size is what makes node storage a priced component in check_workspace_plan.py rather than the 'cannot be bounded' item it was previously listed as."
  }

  # THE REFERENCE. A launch template the node group does not use is decoration that reads as
  # evidence — the review says to "prove the node group references the template".
  assert {
    condition     = aws_eks_node_group.default.launch_template[0].id == aws_launch_template.node.id
    error_message = "aws_eks_node_group.default does not reference aws_launch_template.node. Without the reference the template is inert and the node group falls back to EKS's default — so the encryption claim is false while an aws_launch_template resource sits in the plan looking like proof of it."
  }

  # IMDSv2: until #5533 attaches per-workload identities, a pod reaching the metadata service
  # gets the NODE's credentials, so token-based access is what stops a request-forgery bug in a
  # tenant workload from harvesting them.
  assert {
    condition     = aws_launch_template.node.metadata_options[0].http_tokens == "required"
    error_message = "The node launch template does not require IMDSv2. Until #5533 (w6-10) attaches per-workload identities, any pod that reaches the instance metadata service obtains the NODE role's credentials; requiring a token is what prevents a request-forgery bug in a tenant workload from reading them."
  }

  # The Auto Scaling key grants. Without them the template is valid and every instance launch
  # fails with a KMS error, which presents as nodes that never join the cluster — commonly
  # misdiagnosed as a networking fault.
  assert {
    condition = length([
      for statement in jsondecode(aws_kms_key.workspace[0].policy).Statement :
      statement if try(statement.Principal.AWS, "") == "arn:aws:iam::111122223333:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
    ]) == 2
    error_message = "The key policy does not grant the EC2 Auto Scaling service-linked role use of this key (expected exactly two statements: key use via ec2.<region>, and CreateGrant for AWS resources). Auto Scaling — not the node, not the operator — is the principal that creates node root volumes at launch, so without these grants every instance launch fails with a KMS access error that surfaces as nodes never joining the cluster."
  }

  # ViaService confines the service-linked role to EC2 calls in this region, so it cannot use
  # this key through any other service.
  assert {
    condition = length([
      for statement in jsondecode(aws_kms_key.workspace[0].policy).Statement :
      statement if try(statement.Condition.StringEquals["kms:ViaService"], "") == "ec2.us-east-1.amazonaws.com"
    ]) == 1
    error_message = "The Auto Scaling key-use statement is not restricted with kms:ViaService = ec2.us-east-1.amazonaws.com. Without it the service-linked role could use this workspace's key through any service that accepts it, not only for the EC2 volumes it exists to encrypt."
  }

  # The node volumes carry the workspace tag. default_tags does not reach them: EC2 creates
  # them from this template rather than Terraform declaring them, so without tag_specifications
  # they drop out of the per-workspace cost attribution design item 4 depends on.
  assert {
    condition = length([
      for spec in aws_launch_template.node.tag_specifications :
      spec if spec.resource_type == "volume" && spec.tags.Workspace == "tenant-alpha"
    ]) == 1
    error_message = "The launch template does not tag the volumes it creates with Workspace = tenant-alpha. Provider default_tags do not reach them — EC2 creates these volumes from the template, they are not Terraform-declared resources — so without a volume tag_specification the workspace's node disks are unattributable in cost reports, which is the basis of design item 4's per-workspace estimate."
  }
}

# ---------------------------------------------------------------------------
# A SUPPLIED key: state the requirement, take no ownership
# ---------------------------------------------------------------------------
run "a_supplied_key_is_referenced_not_adopted_and_its_requirement_is_published" {
  command = plan

  variables {
    kms_key_arn = "arn:aws:kms:us-east-1:111122223333:key/11111111-2222-3333-4444-555555555555"
  }

  # No key is created, so no key policy is written. This is the non-adoption property: the
  # supplied key's lifecycle and policy stay the operator's, exactly as a supplied VPC's do.
  assert {
    condition     = length(aws_kms_key.workspace) == 0
    error_message = "A supplied KMS key must not cause this module to create one of its own. Creating a second key would leave the workspace with a key it owns and destroys alongside a key the operator owns, and it is the supplied key that the cluster and log group reference."
  }

  assert {
    condition     = length(aws_kms_alias.workspace) == 0
    error_message = "A supplied KMS key must not get a workspace-managed alias: the alias would be an ADP-owned resource pointing at an operator-owned key, which is a partial adoption of something this module does not own."
  }

  # The supplied key is what the consumers actually use.
  assert {
    condition     = aws_cloudwatch_log_group.cluster.kms_key_id == "arn:aws:kms:us-east-1:111122223333:key/11111111-2222-3333-4444-555555555555"
    error_message = "The control-plane log group does not use the supplied KMS key."
  }

  assert {
    condition     = aws_launch_template.node.block_device_mappings[0].ebs[0].kms_key_id == "arn:aws:kms:us-east-1:111122223333:key/11111111-2222-3333-4444-555555555555"
    error_message = "Node root volumes do not use the supplied KMS key, so a workspace with a supplied key would encrypt its node disks with something else."
  }

  # The requirement is PUBLISHED rather than left as prose. This module cannot verify a
  # supplied key's policy and must not modify it, so the operator and the Wave 6 operations
  # evaluator need the exact statements to compare against `aws kms get-key-policy`.
  assert {
    condition = length([
      for statement in jsondecode(output.supplied_kms_key_required_policy).required_statements :
      statement if try(statement.Condition.ArnEquals["kms:EncryptionContext:aws:logs:arn"], "") == "arn:aws:logs:us-east-1:111122223333:log-group:/aws/eks/adp-dev-spw-970b320a868d197402321c9d69957998/cluster"
    ]) == 1
    error_message = "output.supplied_kms_key_required_policy does not publish the CloudWatch Logs statement with this workspace's real log-group ARN. Because this module neither verifies nor modifies a supplied key's policy, that requirement has to be stated in checkable form — otherwise a key missing it fails late, at apply, with InvalidParameterException on the log group."
  }

  assert {
    condition     = output.kms_key_is_workspace_managed == false
    error_message = "output.kms_key_is_workspace_managed must be false for a supplied key, so a consumer can tell whether this module wrote the key's policy or the operator owns it."
  }
}

run "a_module_created_key_publishes_no_supplied_key_requirement" {
  command = plan

  # Null rather than the statement list, so a reader cannot mistake "this module already did
  # this" for "you must do this".
  assert {
    condition     = output.supplied_kms_key_required_policy == null
    error_message = "When this module creates the key it writes the required policy itself, so supplied_kms_key_required_policy must be null. Publishing the statements anyway would read as an outstanding action item for a key that already has them."
  }

  assert {
    condition     = output.kms_key_is_workspace_managed == true
    error_message = "output.kms_key_is_workspace_managed must be true when this module created the key."
  }
}

run "owned_key_authorizes_the_actual_createcluster_caller" {
  command = plan
  assert {
    condition     = output.provisioning_principal_arn == "arn:aws:iam::111122223333:role/workspace-provisioner"
    error_message = "The apply verifier needs a known scalar principal even while the new key ARN is unknown."
  }
  assert {
    condition = length([
      for statement in jsondecode(aws_kms_key.workspace[0].policy).Statement : statement
      if statement.Sid == "AllowProvisioningCallerToConfigureEKSEncryption" &&
      try(statement.Principal.AWS == "arn:aws:iam::111122223333:role/workspace-provisioner", false) &&
      try(toset(statement.Action) == toset(["kms:DescribeKey", "kms:CreateGrant"]), false) &&
      !can(statement.Condition)
    ]) == 1
    error_message = "Owned key must grant the actual provisioning caller DescribeKey/CreateGrant without an AWS-resource grant condition."
  }
  assert {
    condition     = output.provisioning_caller_kms_requirements.principal_arn == "arn:aws:iam::111122223333:role/workspace-provisioner"
    error_message = "Identity permissions must be attributed to the actual provisioning principal."
  }
}

run "supplied_key_requires_caller_identity_and_key_authorization" {
  command = plan
  variables {
    kms_key_arn = "arn:aws:kms:us-east-1:111122223333:key/11111111-2222-3333-4444-555555555555"
  }
  assert {
    condition     = jsondecode(output.supplied_kms_key_required_policy) == jsondecode(file("${path.module}/tests/fixtures/supplied-kms-required-policy.json"))
    error_message = "Python preflight contract fixture must match the complete Terraform-rendered supplied-key output."
  }
  assert {
    condition = length([
      for statement in jsondecode(output.supplied_kms_key_required_policy).required_statements : statement
      if statement.Sid == "AllowProvisioningCallerToConfigureEKSEncryption" &&
      try(statement.Principal.AWS == "arn:aws:iam::111122223333:role/workspace-provisioner", false) &&
      try(toset(statement.Action) == toset(["kms:DescribeKey", "kms:CreateGrant"]), false) &&
      !can(statement.Condition)
    ]) == 1
    error_message = "Supplied key requirements must name the real CreateCluster caller, not the EKS service principal."
  }
  assert {
    condition = (
      jsondecode(output.provisioning_caller_kms_requirements.identity_policy).Statement[0].Resource == var.kms_key_arn &&
      toset(jsondecode(output.provisioning_caller_kms_requirements.identity_policy).Statement[0].Action) == toset(["kms:DescribeKey", "kms:CreateGrant"]) &&
      !can(jsondecode(output.provisioning_caller_kms_requirements.identity_policy).Statement[0].Condition)
    )
    error_message = "Publish the exact caller identity policy separately from the supplied key policy."
  }
}
