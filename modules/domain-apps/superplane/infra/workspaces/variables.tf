# =============================================================================
# Inputs for one managed workspace — Issue #5532 (w6-09), EPIC A #4910.
# =============================================================================
# EVERY IDENTITY IS REQUIRED. NOTHING IS DEFAULTED.
#
# The Account Factory adoption (#5530) removed a set of working default targets from the
# reference implementation: a management account that was not ADP's, a cluster name that
# was core ADP's ARC runner cluster, and a fixed account name that collided across two
# independent requests. A run that supplied nothing still acted on a specific real target.
#
# `../account-factory/account_factory/modes.py` states the resulting rule: "There are no defaults here: every
# identity is required". This module is the Terraform half of the same contract, so the
# four identities that decide WHERE this acts — account, workspace, region, cluster
# version — have no `default`. A `terraform plan` missing any of them fails at variable
# resolution, before it can plan against something nobody chose.
#
# That is AC-02's "no default account ... appears in rendered execution inputs", enforced
# by absence of a value rather than by a check that a value is acceptable.
# =============================================================================

variable "environment" {
  type        = string
  description = "Environment (2-10 characters), scoped with immutable org/workspace IDs in the v2 state key."

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,9}$", var.environment))
    error_message = "environment must be 2-10 lowercase alphanumeric/hyphen characters so immutable workspace IAM names fit the 64-character limit."
  }
}

variable "workspace_name" {
  type        = string
  description = "Display label for this workspace; identity, state and AWS names come from immutable org_id/workspace_id."


  validation {
    # Lowercase DNS-label shape. This name becomes the EKS cluster name and part of a VPC
    # name tag, and EKS rejects anything else — better to fail here, with a message naming
    # the input, than in the middle of an apply.
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.workspace_name))
    error_message = "workspace_name must be 3-32 characters, lowercase alphanumeric with hyphens, starting with a letter and not ending with a hyphen."
  }

  validation {
    # A workspace named after an ADP-internal component would produce resource names that
    # read as core platform infrastructure in the console and in cost reports, which is
    # exactly the confusion the tenant/workspace boundary exists to prevent.
    condition = !contains(
      ["adp", "platform", "gateway", "superplane", "agent-factory", "agent-context", "default", "kube-system"],
      var.workspace_name
    )
    error_message = "workspace_name must not be an ADP-internal or Kubernetes-reserved name; a workspace is a tenant boundary and its resources must be identifiable as one."
  }


}

variable "account_id" {
  type        = string
  description = <<-EOT
    The AWS account this workspace's infrastructure is created in. REQUIRED — there is
    deliberately no default, so a deploy must name its target account explicitly rather
    than inherit one.

    For `existing-account-managed` this is the adopted account; for
    `new-account-managed` it is the account the Account Factory recorded opening. Either
    way this module CREATES INTO it and never opens it — account creation is a separate,
    separately-authorized operation (#5531), never a side effect of provisioning a
    workspace.
  EOT

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account id. The literal ACCOUNT_ID placeholder deliberately fails this, so an unsubstituted tfvars file stops the plan instead of planning against the wrong account."
  }

  validation {
    # The same two upstream snapshot accounts ../control-plane/variables.tf blocks, plus
    # the reference Account Factory's management account. #5530 refuses this one BY VALUE
    # ("the four legacy values are additionally refused by value ... Un-defaulting alone
    # would leave them working if pasted back in") and the same reasoning applies to a
    # tfvars file: it is not ADP's account and this module is not its owner.
    condition = !contains(
      ["605440105851", "938500344975"],
      var.account_id
    )
    error_message = <<-EOT
      account_id is an upstream AISuperPlane snapshot account (605440105851 = upstream
      ECR/state bucket and the reference Account Factory's management account,
      938500344975 = upstream test cluster). These are not ADP accounts. Supply the
      ADP-owned or adopted account for this workspace.
    EOT
  }
}

# ---------------------------------------------------------------------------
# REGION AND VERSION SUPPORT POLICY (review finding F6)
#
# These two lists are the Terraform half of scripts/region_version_policy.py, which carries the
# full rationale, the dated review, and the EKS support calendar. They are duplicated here
# because a validation block cannot call Python, and this is the only place that refuses a bad
# target BEFORE any resource is created — which is what F6 asks for. `terraform validate` on a
# workspace pinned to a retired version must fail at the variable, not at the apply that has
# already built the VPC.
#
# The duplication is the obvious hazard, so it is checked rather than trusted:
# tests/test_region_version_policy.py parses these very literals out of this file and asserts
# they equal the Python policy's, and fails if either side gains an entry the other lacks. A
# copy nobody compares is how two policies come to disagree.
# ---------------------------------------------------------------------------
locals {
  # Regions this platform is reviewed to create workspaces in. See region_version_policy.py's
  # SUPPORTED_REGIONS for why each is here and what adding one requires.
  supported_regions = [
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "ap-southeast-2",
  ]

  # Kubernetes versions a workspace may be CREATED at: EKS standard or extended support as of
  # the policy's review date. Retired versions are absent deliberately — AWS auto-upgrades
  # clusters off them, so a cluster created at one does not stay at the reviewed version.
  createable_cluster_versions = [
    "1.31",
    "1.32",
    "1.33",
    "1.34",
    "1.35",
  ]
}

variable "aws_region" {
  type        = string
  description = <<-EOT
    Region this workspace's infrastructure is created in. REQUIRED, no default, and must be
    one of the platform's reviewed regions (local.supported_regions above).

    Regional placement is a data-residency decision and a cost decision, and a default
    would make it silently for whoever did not set it.

    An allowlist rather than a pattern, per finding F6: `xx-fake-1` matches every regex for
    an AWS region identifier, and `us-east-2` is entirely real while not being a region this
    platform has reviewed. Both fail at apply, after the network exists.
  EOT

  validation {
    # The shape check is kept ahead of the membership check so a typo gets a message about
    # its shape rather than a list of five regions to scan.
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.aws_region))
    error_message = "aws_region must be an AWS region identifier such as us-east-1."
  }

  validation {
    condition = contains(
      ["us-east-1", "us-west-2", "eu-west-1", "eu-central-1", "ap-southeast-2"],
      var.aws_region
    )
    error_message = "aws_region must be one of the platform's reviewed regions: us-east-1, us-west-2, eu-west-1, eu-central-1, ap-southeast-2. A region identifier that merely LOOKS valid is not a region this platform supports — opt-in regions (ap-east-1, me-south-1) need account state this module cannot see, other partitions (cn-north-1, us-gov-west-1) change every ARN, and an unreviewed region has no price multiplier so its cost cannot be bounded. See scripts/region_version_policy.py; adding a region is a review, not a typo fix."
  }
}

# ---------------------------------------------------------------------------
# Kubernetes version policy
#
# The reference implementation resolved versions at deploy time:
#
#     RELEASE_VERSION=$(curl -sL ".../releases/latest" | ... 2>/dev/null || echo "")
#
# so what it installed depended on when it ran, and a network error turned into a
# partially-installed control plane reported as success (#5530, "Dependencies are pinned").
# A moving label is not a version. This module takes an exact minor version and refuses
# anything that is a label rather than a pin. The separate node-image-pins.json
# policy pins the worker image release; a Kubernetes minor alone does not pin an AMI.
# ---------------------------------------------------------------------------
variable "cluster_version" {
  type        = string
  description = <<-EOT
    Exact Kubernetes minor version for this workspace's EKS cluster (for example
    "1.33"). REQUIRED, no default, no moving label, and it must be a version EKS can
    actually create today (local.createable_cluster_versions above).

    No moving label, because a version resolved at apply time makes the cluster's contents
    depend on when it was applied rather than on what was reviewed.

    An allowlist rather than a range, per finding F6. The pattern this replaced accepted
    "1.25" — retired in May 2024, so AWS will not create it and auto-upgrades existing
    clusters off it — and "1.39", which does not exist. A retired or nonexistent version is
    a failed apply after the VPC and subnets are already built.

    Versions in EXTENDED support are accepted deliberately: a tenant mid-upgrade has a
    legitimate reason to be there. They cost six times the standard rate, and the plan
    guard's bounded estimate prices them at that rate so the cost of staying is visible.
  EOT

  validation {
    condition     = can(regex("^1\\.[0-9]+$", var.cluster_version))
    error_message = "cluster_version must be an exact Kubernetes minor version such as \"1.33\". Moving labels (\"latest\", \"stable\", \"default\") and patch-level values like \"1.33.2\" are refused: see variables.tf for why a version resolved at apply time is not a pin."
  }

  validation {
    condition = contains(
      ["1.31", "1.32", "1.33", "1.34", "1.35"],
      var.cluster_version
    )
    error_message = "cluster_version must be a version EKS can create today: 1.31, 1.32, 1.33 (extended support, billed at 6x the standard control-plane rate) or 1.34, 1.35 (standard support). Versions up to 1.30 are RETIRED — AWS auto-upgrades clusters off them, so a workspace created at one would not stay at the reviewed version — and anything above 1.35 is not offered as of this policy's review date. See scripts/region_version_policy.py for the support calendar and its review date."
  }
}

# ---------------------------------------------------------------------------
# NETWORKING MODE — owned versus supplied
#
# Design item 3: "Support owned networking versus explicitly supplied networking without
# silently adopting its lifecycle."
#
# The distinction is NOT cosmetic and it is not a convenience. In `owned` mode this module
# creates the VPC and may therefore destroy it. In `supplied` mode the customer's VPC
# already exists, and the destructive mistake — the one this variable exists to make
# impossible — is taking it into this module's state, because then a `terraform destroy`
# of a workspace deletes a network that was merely lent to us.
#
# So `supplied` mode reads the VPC through data sources and declares no network resource at
# all. Nothing is imported. "Without silently adopting its lifecycle" is a property of the
# resource graph, not a promise in a comment: tests/supplied_networking.tftest.hcl asserts
# the planned resource set contains no VPC, subnet, gateway or route table, and
# tests/test_networking_modes.py asserts every network resource declaration in the module
# is gated on `owned`.
#
# It mirrors `ClusterOwnership` in ../account-factory/account_factory/modes.py, which
# records ADP_CREATED vs ADOPTED for the same reason: "a cluster ADP adopted is never
# deleted by ADP, whichever request produced the adoption".
# ---------------------------------------------------------------------------
variable "networking_mode" {
  type        = string
  description = <<-EOT
    Who owns this workspace's network.

      "owned"    — this module creates the VPC, subnets, gateways and routes, and may
                   destroy them. Requires vpc_cidr and availability_zones.
      "supplied" — the VPC already exists and is supplied by its owner. This module
                   reads it, places the cluster in the given subnets, and declares NO
                   network resource, so a destroy cannot reach it. Requires
                   supplied_vpc_id and supplied_private_subnet_ids.

    REQUIRED, no default. A default here would decide ownership of a customer's network
    for whoever did not set it, which is the one decision this input exists to force.
  EOT

  validation {
    condition     = contains(["owned", "supplied"], var.networking_mode)
    error_message = "networking_mode must be exactly \"owned\" or \"supplied\". An unsupported mode is refused rather than interpreted: see variables.tf."
  }
}

variable "vpc_cidr" {
  type        = string
  description = <<-EOT
    IPv4 CIDR for the VPC this module creates. Required in "owned" mode, and must be
    empty in "supplied" mode.
  EOT
  default     = ""

  validation {
    condition     = var.vpc_cidr == "" || can(cidrnetmask(var.vpc_cidr))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block such as 10.64.0.0/16."
  }

  validation {
    # Required when owned. Stated as a cross-variable rule rather than by making the
    # variable required, because in `supplied` mode a value here is not merely redundant.
    condition     = var.networking_mode != "owned" || var.vpc_cidr != ""
    error_message = "vpc_cidr is required when networking_mode is \"owned\" — that mode creates the VPC."
  }

  validation {
    # REFUSED, not ignored, in supplied mode. #5530's reasoning applies exactly: a value
    # that cannot take effect is a value whose author was wrong about what the request
    # does. Someone who supplies a CIDR alongside an existing VPC id believes this module
    # is creating a network; it is not, and one of those two beliefs is wrong.
    condition     = var.networking_mode != "supplied" || var.vpc_cidr == ""
    error_message = "vpc_cidr must be empty when networking_mode is \"supplied\": the VPC already exists and this module creates no network. A CIDR here means the request's author expected a VPC to be created."
  }

  validation {
    # A /24 cannot hold the per-AZ private and public subnets this module carves, and a
    # prefix shorter than /16 is a larger allocation than a single workspace should claim
    # from a shared address plan.
    condition     = var.vpc_cidr == "" || can(regex("^.*/(1[6-9]|2[0-2])$", var.vpc_cidr))
    error_message = "vpc_cidr must have a prefix length between /16 and /22: shorter over-allocates a shared address plan, longer cannot be subdivided across availability zones."
  }
}

variable "availability_zones" {
  type        = list(string)
  description = <<-EOT
    Availability zones to spread this workspace's subnets across. Required in "owned"
    mode, and must be empty in "supplied" mode.
  EOT
  default     = []

  validation {
    condition     = var.networking_mode != "owned" || length(var.availability_zones) >= 2
    error_message = "availability_zones must name at least 2 zones when networking_mode is \"owned\": EKS requires subnets in two zones, and a single-zone cluster has no availability story."
  }

  validation {
    condition     = var.networking_mode != "supplied" || length(var.availability_zones) == 0
    error_message = "availability_zones must be empty when networking_mode is \"supplied\": the supplied subnets already determine zone placement."
  }

  validation {
    condition     = length(var.availability_zones) == length(distinct(var.availability_zones))
    error_message = "availability_zones must not contain duplicates; a repeated zone would produce two subnets in one zone and none in another."
  }
}

variable "supplied_vpc_id" {
  type        = string
  description = <<-EOT
    Id of an existing VPC this workspace's cluster is placed into. Required in "supplied"
    mode, and must be empty in "owned" mode.

    This module READS this VPC. It never imports it, never modifies it and never destroys
    it — see the networking_mode documentation above.
  EOT
  default     = ""

  validation {
    condition     = var.supplied_vpc_id == "" || can(regex("^vpc-[0-9a-f]{8,17}$", var.supplied_vpc_id))
    error_message = "supplied_vpc_id must be an AWS VPC id such as vpc-0a1b2c3d4e5f67890."
  }

  validation {
    condition     = var.networking_mode != "supplied" || var.supplied_vpc_id != ""
    error_message = "supplied_vpc_id is required when networking_mode is \"supplied\"."
  }

  validation {
    condition     = var.networking_mode != "owned" || var.supplied_vpc_id == ""
    error_message = "supplied_vpc_id must be empty when networking_mode is \"owned\": that mode creates the VPC, so an existing id here means the request's author expected adoption. Use networking_mode = \"supplied\" to place the cluster in an existing VPC."
  }
}

variable "supplied_private_subnet_ids" {
  type        = list(string)
  description = <<-EOT
    Private subnets in the supplied VPC that this workspace's cluster and node group use.
    Required in "supplied" mode, and must be empty in "owned" mode.

    Plan-time read-only checks require disabled automatic public IPv4 assignment,
    VPC DNS, no node Internet Gateway route, and a zonal public-NAT egress path.
    Transit and endpoint-only egress are unsupported. #5533 additionally checks
    live node/service reachability, NACLs and available addresses before usability.
  EOT
  default     = []

  validation {
    condition = alltrue([
      for id in var.supplied_private_subnet_ids : can(regex("^subnet-[0-9a-f]{8,17}$", id))
    ])
    error_message = "every supplied_private_subnet_ids entry must be an AWS subnet id such as subnet-0a1b2c3d4e5f67890."
  }

  validation {
    condition     = var.networking_mode != "supplied" || length(var.supplied_private_subnet_ids) >= 2
    error_message = "supplied_private_subnet_ids must name at least 2 subnets when networking_mode is \"supplied\": EKS requires subnets in two availability zones."
  }

  validation {
    condition     = var.networking_mode != "owned" || length(var.supplied_private_subnet_ids) == 0
    error_message = "supplied_private_subnet_ids must be empty when networking_mode is \"owned\": that mode creates the subnets."
  }

  validation {
    condition     = length(var.supplied_private_subnet_ids) == length(distinct(var.supplied_private_subnet_ids))
    error_message = "supplied_private_subnet_ids must not contain duplicates."
  }
}

# ---------------------------------------------------------------------------
# Cluster endpoint exposure
#
# The vendored EKS graph sets BOTH `endpointPublicAccess: true` and
# `endpointPrivateAccess: true` (vendor/kro-account-factory/02-eks-cluster-stack.yaml:97-98)
# with no CIDR restriction, which publishes every workspace's Kubernetes API to the whole
# internet. Authentication still applies, but the attack surface is the entire control
# plane API and the exposure is invisible in the request that created it.
#
# Here private access is always on, public access defaults OFF, and turning it on requires
# naming the address ranges that may reach it. 0.0.0.0/0 is refused outright.
# ---------------------------------------------------------------------------
variable "cluster_endpoint_public_access" {
  type        = bool
  description = <<-EOT
    Whether this workspace's Kubernetes API endpoint is reachable from outside the VPC.
    Defaults to false. When true, cluster_endpoint_public_access_cidrs must name the
    ranges allowed, and 0.0.0.0/0 is refused.
  EOT
  default     = false
}

variable "cluster_endpoint_public_access_cidrs" {
  type        = list(string)
  description = <<-EOT
    Address ranges permitted to reach the public Kubernetes API endpoint. Must be
    non-empty when cluster_endpoint_public_access is true, and must be empty when it is
    false, so the allowlist cannot read as effective while public access is off.
  EOT
  default     = []

  validation {
    condition = alltrue([
      for cidr in var.cluster_endpoint_public_access_cidrs : can(cidrnetmask(cidr))
    ])
    error_message = "every cluster_endpoint_public_access_cidrs entry must be a valid IPv4 CIDR block."
  }

  validation {
    # The rule that matters, declared HERE rather than on the boolean for the reason
    # ../control-plane/variables.tf records about the CORS pairing: Terraform SKIPS a
    # validation whose referenced variable is already invalid, so a cross-variable rule
    # must live on the variable that carries the dangerous value.
    condition = alltrue([
      for cidr in var.cluster_endpoint_public_access_cidrs : try(cidrnetmask(cidr) != "0.0.0.0", false)
    ])
    error_message = "cluster_endpoint_public_access_cidrs must not contain 0.0.0.0/0. A public Kubernetes API open to the entire internet is the exposure this input exists to bound; name the operator and CI ranges that need it."
  }

  validation {
    # ::/0 is the same defect in IPv6 clothing and would otherwise pass the check above
    # while being equally open.
    condition     = !contains(var.cluster_endpoint_public_access_cidrs, "::/0")
    error_message = "cluster_endpoint_public_access_cidrs must not contain ::/0, which is 0.0.0.0/0's IPv6 equivalent."
  }

  validation {
    condition     = !var.cluster_endpoint_public_access || length(var.cluster_endpoint_public_access_cidrs) > 0
    error_message = "cluster_endpoint_public_access_cidrs must name at least one range when cluster_endpoint_public_access is true: enabling public access without an allowlist means every address."
  }

  validation {
    condition     = var.cluster_endpoint_public_access || length(var.cluster_endpoint_public_access_cidrs) == 0
    error_message = "cluster_endpoint_public_access_cidrs must be empty when cluster_endpoint_public_access is false, so a populated allowlist never reads as an effective one."
  }
}

# ---------------------------------------------------------------------------
# Node group sizing. Bounded, and the bounds are the cost story.
#
# Design item 4 requires "bounded cost/resource estimates". An unbounded maximum makes
# that impossible to state: the estimate would have no ceiling. These defaults match the
# vendored graph's (desired=1, min=0, max=2) so a workspace created through either path
# starts the same size.
# ---------------------------------------------------------------------------
variable "node_instance_type" {
  type        = string
  description = "EC2 instance type for this workspace's default node group."
  default     = "m6i.large"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]*[0-9][a-z]*\\.[a-z0-9]+$", var.node_instance_type))
    error_message = "node_instance_type must be an EC2 instance type such as m6i.large."
  }
}

variable "node_group_desired_size" {
  type        = number
  description = "Desired node count for the default node group."
  default     = 1

  validation {
    condition     = var.node_group_desired_size >= 0 && var.node_group_desired_size <= 100 && floor(var.node_group_desired_size) == var.node_group_desired_size
    error_message = "node_group_desired_size must be a whole number between 0 and 100."
  }

  validation {
    condition     = var.node_group_desired_size >= var.node_group_min_size
    error_message = "node_group_desired_size must be greater than or equal to node_group_min_size."
  }
}

variable "node_group_min_size" {
  type        = number
  description = "Minimum node count for the default node group."
  default     = 0

  validation {
    condition     = var.node_group_min_size >= 0 && var.node_group_min_size <= 100 && floor(var.node_group_min_size) == var.node_group_min_size
    error_message = "node_group_min_size must be a whole number between 0 and 100."
  }
}

variable "node_group_max_size" {
  type        = number
  description = <<-EOT
    Maximum node count for the default node group. This is the ceiling a cost estimate is
    computed against, so it is bounded rather than open-ended.
  EOT
  default     = 2

  validation {
    condition     = var.node_group_max_size >= 1 && var.node_group_max_size <= 100 && floor(var.node_group_max_size) == var.node_group_max_size
    error_message = "node_group_max_size must be a whole number between 1 and 100. The upper bound is what makes a bounded cost estimate possible."
  }

  validation {
    condition     = var.node_group_max_size >= var.node_group_min_size
    error_message = "node_group_max_size must be greater than or equal to node_group_min_size."
  }

  validation {
    condition     = var.node_group_max_size >= var.node_group_desired_size
    error_message = "node_group_max_size must be greater than or equal to node_group_desired_size."
  }
}

variable "node_volume_size" {
  type        = number
  description = <<-EOT
    Root EBS volume size in GiB for each node, encrypted with this workspace's KMS key by
    the launch template in eks.tf.

    A reviewed value rather than an inherited default, for two reasons. EKS's default node
    template creates a 20 GiB volume that is either unencrypted or encrypted with the
    ACCOUNT's default key -- not this workspace's -- so leaving it unset would mean the
    encryption guarantee depended on an account-level setting this module does not control.
    And node storage is only a bounded cost if its size is a known number: the plan guard
    prices this volume per node at the node group's ceiling.
  EOT
  default     = 50

  validation {
    # 20 GiB is EKS's documented floor for the node AMI plus image cache. The upper bound
    # exists so this stays a bounded cost: at the node ceiling of 100, 500 GiB volumes would
    # be 50 TiB of gp3 storage, which is an order of magnitude more than the compute.
    condition     = var.node_volume_size >= 20 && var.node_volume_size <= 500 && floor(var.node_volume_size) == var.node_volume_size
    error_message = "node_volume_size must be a whole number of GiB between 20 and 500. The lower bound is EKS's node AMI and image-cache floor; the upper bound keeps node storage a bounded cost at the node ceiling."
  }
}

# ---------------------------------------------------------------------------
# Encryption
#
# Design item 1 requires encryption. Kubernetes Secrets at rest in etcd are encrypted with
# a KMS key, and the node group's EBS volumes are encrypted. The key is an INPUT, not a
# resource: a key created and destroyed with the workspace would be destroyed while
# encrypted backups of that workspace still existed, and key lifecycle is a
# separately-owned decision. Empty means "create a workspace-scoped key here"; supplying
# an ARN uses the operator's existing key.
# ---------------------------------------------------------------------------
variable "kms_key_arn" {
  type        = string
  description = <<-EOT
    KMS key for envelope-encrypting Kubernetes Secrets, control-plane logs and node EBS root
    volumes. Empty means this module creates a workspace-scoped key with rotation enabled and
    the key policy its three consumers require. Supply an ARN to use an existing key whose
    lifecycle you own.

    A SUPPLIED KEY'S POLICY IS YOURS, AND THIS MODULE DOES NOT CHANGE IT

    When you supply a key, this module references it and does not modify its policy -- the
    same non-adoption rule that applies to supplied networking. It cannot: the key is not in
    this module's state, and attaching a policy to a key ADP does not own would take over a
    lifecycle decision that stays with you.

    That means the required permissions must ALREADY be on your key, and if they are not, the
    apply fails part-way -- the log group is rejected with InvalidParameterException, or node
    instances fail to launch with a KMS access error that presents as nodes never joining the
    cluster. The exact statements your key needs are published by
    `output "supplied_kms_key_required_policy"`, rendered with this workspace's real log-group
    ARN, region and account so they can be compared against your key's policy directly:

      1. CloudWatch Logs (logs.<region>.amazonaws.com) may Encrypt/Decrypt/ReEncrypt*/
         GenerateDataKey*/DescribeKey, conditioned on the encryption context
         kms:EncryptionContext:aws:logs:arn matching this workspace's cluster log group.
      2. The EC2 Auto Scaling service-linked role may use the key via ec2.<region>.amazonaws.com
         and CreateGrant for AWS resources, so node root volumes can be encrypted with it.
      3. EKS may use the key for Secrets envelope encryption.

    Verify with:
      aws kms get-key-policy --key-id <this arn> --policy-name default --query Policy --output text
  EOT
  default     = ""

  validation {
    condition     = var.kms_key_arn == "" || can(regex("^arn:aws[a-z-]*:kms:[a-z0-9-]+:[0-9]{12}:key/", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN such as arn:aws:kms:us-east-1:111122223333:key/<id>, or empty to create a workspace-scoped key."
  }
  validation {
    condition = var.kms_key_arn == "" || can(regex(
      "^arn:${data.aws_partition.current.partition}:kms:${var.aws_region}:${var.account_id}:key/[a-zA-Z0-9-]+$",
      var.kms_key_arn
    ))
    error_message = "A supplied KMS key must belong to the selected AWS partition, region and account. Cross-account keys are not supported by this workspace module."
  }

}

# ---------------------------------------------------------------------------
# Who may reach this workspace's Kubernetes API (see the workspace admin role in iam.tf)
#
# Both human and automation trust lists are empty by default. The admin role is created
# only when at least one list names an explicit principal.
#
# Full principal ARNs, never a bare account id. A trust policy whose principal is
# `arn:aws:iam::<account>:root` is assumable by every principal in that account, including
# every role created there in future — which in a workspace account is the tenant's own
# roles. That is the specific mistake this validation refuses.
# ---------------------------------------------------------------------------
variable "workspace_admin_principal_arns" {
  type        = list(string)
  description = <<-EOT
    Exact IAM user ARNs permitted to assume this workspace's admin role with MFA and
    read its cluster endpoint. No admin role is created when both this list and the
    automation-role list are empty. Bare account principals (`:root`) are refused.

    Kubernetes-side authority is NOT granted here — that is an EKS access entry, owned by
    #5533 (w6-10). This is API reachability and a trust anchor only.
  EOT
  default     = []

  validation {
    condition = alltrue([
      for arn in var.workspace_admin_principal_arns :
      can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:user/[^*?]+$", arn))
    ])
    error_message = "Human MFA trust supports exact IAM user ARNs only (arn:aws:iam::<account>:user/<name>); federated human role sessions require a separate reviewed authentication model."
  }

  validation {
    condition = alltrue([
      for arn in var.workspace_admin_principal_arns :
      !can(regex(":root$", arn))
    ])
    error_message = "workspace_admin_principal_arns must not contain an account root principal (arn:aws:iam::<account>:root): that makes the workspace admin role assumable by EVERY principal in the account, including the tenant's own roles. Name the specific human IAM user; automation roles have their own trust list."
  }

  validation {
    condition     = length(var.workspace_admin_principal_arns) == length(distinct(var.workspace_admin_principal_arns))
    error_message = "workspace_admin_principal_arns must not contain duplicates."
  }
}

# ---------------------------------------------------------------------------
# Automation principals — the machine half of workspace admin access (review finding F3)
#
# Separate from var.workspace_admin_principal_arns because the two need DIFFERENT trust
# conditions, and conflating them made the documented automation path impossible.
#
# The human statement requires `aws:MultiFactorAuthPresent = true`. A role session cannot
# satisfy that: when a service assumes a role with its own credentials — an EKS pod via IRSA,
# a CI role, a Lambda execution role — the condition key is false rather than true, so the
# statement denies it. The ADP control-plane role this module's documentation names as the
# consumer of workspace admin access was therefore denied on every attempt, surfacing as an
# opaque AccessDenied that says nothing about MFA.
#
# The safe separation is not "drop the MFA condition" -- that would remove the protection
# making a leaked human access key insufficient. It is to name machine principals explicitly
# and exactly. The validations below are what keep this list from becoming a general escape
# hatch: role ARNs only, no users, no `:root`, no wildcards.
# ---------------------------------------------------------------------------
variable "workspace_admin_automation_role_arns" {
  type        = list(string)
  description = <<-EOT
    IAM ROLE ARNs belonging to automation (the ADP control plane, a CI role, an IRSA role)
    permitted to assume this workspace's admin role WITHOUT an MFA condition, because a role
    session cannot present MFA.

    Empty by default. Each entry is an exact role ARN — IAM users, account root principals and
    wildcards are refused, so widening this is always a reviewable change naming a specific
    role. Human operators belong in workspace_admin_principal_arns instead, where the MFA
    requirement still applies.

    The permissions the assumed role gets are identical either way and remain scoped to this
    workspace's cluster; only the trust condition differs. Kubernetes-side authority is still
    an EKS access entry owned by #5533 (w6-10), not granted here.
  EOT
  default     = []

  validation {
    condition     = length(setintersection(toset(var.workspace_admin_principal_arns), toset(var.workspace_admin_automation_role_arns))) == 0
    error_message = "Human MFA principals and automation principals must be disjoint."
  }

  validation {
    # ROLES ONLY, separate from the human list which accepts only users. An IAM
    # user is a long-lived credential belonging to a person; admitting one here would let a
    # human-shaped credential bypass the MFA requirement, which is precisely the protection
    # this split is designed to preserve.
    condition = alltrue([
      for arn in var.workspace_admin_automation_role_arns :
      can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:role/.+$", arn))
    ])
    error_message = "every workspace_admin_automation_role_arns entry must be a full IAM ROLE ARN (arn:aws:iam::<account>:role/<name>). IAM users are refused here: a user is a human-shaped long-lived credential, and admitting one would let it bypass the MFA condition that workspace_admin_principal_arns enforces. Name human operators there instead."
  }

  validation {
    condition = alltrue([
      for arn in var.workspace_admin_automation_role_arns :
      !can(regex(":root$", arn))
    ])
    error_message = "workspace_admin_automation_role_arns must not contain an account root principal: that would make this workspace's admin role assumable without MFA by EVERY principal in the account, including the tenant's own roles."
  }

  validation {
    # A wildcard in a role path or name would make the allowlist unbounded while still looking
    # like a specific entry. IAM does not expand wildcards in a principal ARN, so such an entry
    # would in fact match nothing and fail silently — but it reads as a grant, and a reviewer
    # cannot tell which meaning was intended. Refuse it either way.
    condition = alltrue([
      for arn in var.workspace_admin_automation_role_arns :
      !can(regex("[*?]", arn))
    ])
    error_message = "workspace_admin_automation_role_arns must not contain wildcards. Name each automation role exactly: a wildcard principal in a trust policy is not expanded by IAM, so it grants nothing while reading as a grant."
  }

  validation {
    condition     = length(var.workspace_admin_automation_role_arns) == length(distinct(var.workspace_admin_automation_role_arns))
    error_message = "workspace_admin_automation_role_arns must not contain duplicates."
  }
}

variable "cluster_log_types" {
  type        = list(string)
  description = <<-EOT
    EKS control-plane log types shipped to CloudWatch. Defaults to all five, matching the
    vendored graph's logging block: an audit trail whose contents depend on a per-workspace
    choice is not an audit trail.
  EOT
  default     = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  validation {
    condition = alltrue([
      for t in var.cluster_log_types :
      contains(["api", "audit", "authenticator", "controllerManager", "scheduler"], t)
    ])
    error_message = "cluster_log_types entries must be EKS control-plane log types: api, audit, authenticator, controllerManager, scheduler."
  }

  validation {
    condition     = contains(var.cluster_log_types, "audit") && contains(var.cluster_log_types, "authenticator")
    error_message = "cluster_log_types must include \"audit\" and \"authenticator\": those two are what make \"who did what in this workspace\" answerable after the fact."
  }

  validation {
    condition     = length(var.cluster_log_types) == length(distinct(var.cluster_log_types))
    error_message = "cluster_log_types must not contain duplicates."
  }
}

variable "log_retention_days" {
  type        = number
  description = "Retention for this workspace's control-plane log group. 0 is refused; logs that never expire and logs that vanish are both operational defects."
  default     = 90

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of CloudWatch's accepted retention values (1, 3, 5, 7, 14, 30, 60, 90, ... 3653). \"Never expire\" is not offered here."
  }
}

variable "cost_center" {
  type        = string
  description = "Cost attribution tag applied to every resource, so a workspace's spend is separable from ADP's and from other workspaces'."
  default     = "engineering"

  validation {
    condition     = can(regex("^[a-zA-Z0-9 _/:.-]{2,64}$", var.cost_center))
    error_message = "cost_center must be 2-64 characters of tag-safe text."
  }
}

variable "org_id" {
  type        = string
  description = "Immutable org_id from the trusted provisioning OperationBinding, never workspace display parameters. Required; no legacy-name fallback."
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", var.org_id))
    error_message = "org_id must be an immutable bound ID (1-128 safe ASCII characters)."
  }
}

variable "workspace_id" {
  type        = string
  description = "Immutable workspace_id from the trusted provisioning OperationBinding, never workspace display parameters. Required; no legacy-name fallback."
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", var.workspace_id))
    error_message = "workspace_id must be an immutable bound ID (1-128 safe ASCII characters)."
  }
}

variable "node_image_repository_arns" {
  description = "Exact ECR repository ARNs that this workspace may pull. AWS EKS system repositories are separately pinned. No account-wide read/list permissions are granted."
  type        = list(string)
  default     = []
  validation {
    condition = alltrue([
      for arn in var.node_image_repository_arns :
      can(regex("^arn:aws:ecr:[a-z0-9-]+:[0-9]{12}:repository/[a-z0-9][a-z0-9._/-]*$", arn))
    ])
    error_message = "Node image repositories must be exact ECR repository ARNs without wildcards."
  }
}
