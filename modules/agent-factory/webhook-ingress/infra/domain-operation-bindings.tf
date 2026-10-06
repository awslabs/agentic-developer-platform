# Shared authority owner for the bounded native Superplane runtime. App Terraform
# owns the queue and roles; this module owns registry authority and Gateway wiring.
# No default deployment gains a binding or additional permissions.
variable "domain_operation_bindings" {
  description = "Reviewed native Superplane runtime binding; empty by default. IDs/role IDs and secret ARNs come from verified owner preparation, never request bodies."
  type = map(object({
    operator_role_arn   = string
    producer_role_arn   = string
    producer_role_id    = string
    worker_role_arn     = string
    worker_role_id      = string
    secret_kms_key_arns = optional(set(string), [])
    binding = object({
      domain                           = string
      org_id                           = string
      adp_org_id                       = string
      producer_registry_id             = string
      worker_registry_id               = string
      database_secret_id               = string
      database_schema                  = string
      domain_database_secret_id        = string
      domain_database_schema           = string
      queue_url                        = string
      worker_namespace                 = string
      worker_service_account           = string
      worker_container                 = string
      worker_scaled_job                = string
      worker_image_digests             = set(string)
      repo                             = string
      observation_url                  = string
      observation_credential_secret_id = string
    })
  }))
  default = {}
  validation {
    condition = length(var.domain_operation_bindings) <= 1 && alltrue([
      for key, value in var.domain_operation_bindings : key == "superplane" && value.binding.domain == key &&
      can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", value.binding.org_id)) &&
      can(regex("^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,254}$", value.binding.adp_org_id)) &&
      can(regex("^[0-9a-f-]{36}$", value.binding.producer_registry_id)) &&
      can(regex("^[0-9a-f-]{36}$", value.binding.worker_registry_id)) &&
      value.binding.producer_registry_id != value.binding.worker_registry_id &&
      value.binding.database_schema != value.binding.domain_database_schema &&
      alltrue([for schema in [value.binding.database_schema, value.binding.domain_database_schema] : can(regex("^[a-z_][a-z0-9_]{0,62}$", schema)) && schema != "public"]) &&
      value.binding.worker_service_account == "superplane-paid-worker" &&
      value.binding.worker_scaled_job == "superplane-paid-worker" &&
      value.binding.worker_container == "paid-worker" &&
      can(regex("^superplane(-[a-z0-9-]+)?$", value.binding.worker_namespace)) &&
      length(value.binding.worker_image_digests) > 0 && length(value.binding.worker_image_digests) <= 2 &&
      alltrue([for digest in value.binding.worker_image_digests : can(regex("^sha256:[a-f0-9]{64}$", digest)) && digest != "sha256:${join("", [for i in range(64) : "0"])}"]) &&
      can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", value.binding.repo)) &&
      can(regex("^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9/_-]*)?$", value.binding.observation_url)) &&
      can(regex("^AROA[A-Z0-9]{16,32}$", value.producer_role_id)) &&
      can(regex("^AROA[A-Z0-9]{16,32}$", value.worker_role_id))
    ])
    error_message = "Only one exact native Superplane binding with separate schemas, immutable identities and reviewed non-placeholder digests is supported."
  }
}

locals {
  domain_operation_enabled = length(var.domain_operation_bindings) > 0
  domain_operation_documents = {
    for key, value in var.domain_operation_bindings : key => {
      version           = 1
      account_id        = local.account_id
      region            = var.aws_region
      environment       = var.environment
      operator_role_arn = value.operator_role_arn
      registry_table    = data.aws_ssm_parameter.domain_operation_registry[0].value
      domain_org_id     = value.binding.org_id
      adp_org_id        = value.binding.adp_org_id
      producer          = { agent_id = value.binding.producer_registry_id, role_arn = value.producer_role_arn, role_id = value.producer_role_id }
      worker            = { agent_id = value.binding.worker_registry_id, role_arn = value.worker_role_arn, role_id = value.worker_role_id }
    }
  }
  domain_operation_gateway_bindings = [for key in sort(keys(var.domain_operation_bindings)) : merge(var.domain_operation_bindings[key].binding, {
    worker_image_digests = sort(tolist(var.domain_operation_bindings[key].binding.worker_image_digests))
  })]
  domain_operation_queue_arns = [for value in values(var.domain_operation_bindings) : "arn:aws:sqs:${var.aws_region}:${local.account_id}:${element(reverse(split("/", value.binding.queue_url)), 0)}"]
  domain_operation_secrets    = flatten([for value in values(var.domain_operation_bindings) : [value.binding.database_secret_id, value.binding.domain_database_secret_id, value.binding.observation_credential_secret_id]])
  domain_operation_kms_keys   = distinct(flatten([for value in values(var.domain_operation_bindings) : tolist(value.secret_kms_key_arns)]))
  domain_operation_gateway_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Sid = "GovernedDomainQueue", Effect = "Allow", Action = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:ChangeMessageVisibility", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = local.domain_operation_queue_arns },
      { Sid = "ExactDomainServiceSecrets", Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = local.domain_operation_secrets }
      ], length(local.domain_operation_kms_keys) > 0 ? [{
        Sid       = "DecryptExactDomainSecrets", Effect = "Allow", Action = ["kms:Decrypt"], Resource = local.domain_operation_kms_keys,
        Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.aws_region}.amazonaws.com", "kms:EncryptionContext:SecretARN" = local.domain_operation_secrets } }
    }] : [])
  })
}

data "aws_ssm_parameter" "domain_operation_registry" {
  count = local.domain_operation_enabled ? 1 : 0
  name  = "/adp/${var.environment}/gateway/agent-registry-table"
}

data "aws_iam_role" "domain_operation_producer" {
  for_each = var.domain_operation_bindings
  name     = "adp-${var.environment}-superplane-api-producer"
}

data "aws_iam_role" "domain_operation_worker" {
  for_each = var.domain_operation_bindings
  name     = "adp-${var.environment}-superplane-domain-worker"
}

# The registry uses a conditional atomic pair instead of unconditional PutItem:
# no adoption, restoration, overwritten row or lost-response second identity.
resource "terraform_data" "domain_operation_registration" {
  for_each         = var.domain_operation_bindings
  input            = local.domain_operation_documents[each.key]
  triggers_replace = [local.domain_operation_documents[each.key]]
  lifecycle {
    precondition {
      condition = local.agent_authority_provisioned && (
        data.aws_caller_identity.current.arn == each.value.operator_role_arn ||
        startswith(data.aws_caller_identity.current.arn, "arn:aws:sts::${local.account_id}:assumed-role/${trimprefix(each.value.operator_role_arn, "arn:aws:iam::${local.account_id}:role/")}/")
      ) && each.value.producer_role_arn == data.aws_iam_role.domain_operation_producer[each.key].arn && each.value.producer_role_id == data.aws_iam_role.domain_operation_producer[each.key].unique_id && each.value.worker_role_arn == data.aws_iam_role.domain_operation_worker[each.key].arn && each.value.worker_role_id == data.aws_iam_role.domain_operation_worker[each.key].unique_id
      error_message = "Protected authority must be prepared; selected operator or immutable domain role identities changed."
    }
    precondition {
      condition = (
        can(regex("^arn:aws:iam::${local.account_id}:role/[A-Za-z0-9+=,.@_-]+$", each.value.operator_role_arn)) &&
        each.value.binding.queue_url == "https://sqs.${var.aws_region}.amazonaws.com/${local.account_id}/adp-${var.environment}-superplane-domain-operations" &&
        length(distinct([each.value.binding.database_secret_id, each.value.binding.domain_database_secret_id, each.value.binding.observation_credential_secret_id])) == 3 &&
        alltrue([for arn in [each.value.binding.database_secret_id, each.value.binding.domain_database_secret_id, each.value.binding.observation_credential_secret_id] : can(regex("^arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:[A-Za-z0-9/_+=.@-]+$", arn))]) &&
        alltrue([for arn in each.value.secret_kms_key_arns : can(regex("^arn:aws:kms:${var.aws_region}:${local.account_id}:key/[a-f0-9-]{36}$", arn))])
      )
      error_message = "Domain queue, secret purposes, KMS keys and operator must match the exact same-account reviewed recipe."
    }
  }
  provisioner "local-exec" {
    command = "python3 \"${path.module}/../scripts/register-domain-operation.py\""
    environment = {
      ADP_DOMAIN_REGISTRATION_DOCUMENT = jsonencode(self.input)
    }
  }
}

resource "aws_iam_role_policy" "gateway_domain_operations" {
  count      = local.domain_operation_enabled && !var.gateway_authority_managed_policies ? 1 : 0
  name       = "adp-${var.environment}-gateway-domain-operations"
  role       = "adp-${var.environment}-role-gateway-service"
  policy     = local.domain_operation_gateway_policy
  depends_on = [terraform_data.domain_operation_registration]
}
resource "aws_iam_policy" "gateway_domain_operations" {
  count      = local.domain_operation_enabled && var.gateway_authority_managed_policies ? 1 : 0
  name       = "adp-${var.environment}-gateway-domain-operations"
  policy     = local.domain_operation_gateway_policy
  depends_on = [terraform_data.domain_operation_registration]
}
resource "aws_iam_role_policy_attachment" "gateway_domain_operations" {
  count      = local.domain_operation_enabled && var.gateway_authority_managed_policies ? 1 : 0
  role       = "adp-${var.environment}-role-gateway-service"
  policy_arn = aws_iam_policy.gateway_domain_operations[0].arn
}

resource "kubernetes_role" "gateway_domain_operation_read" {
  for_each = var.domain_operation_bindings
  metadata {
    name      = "adp-gateway-domain-operation-read"
    namespace = each.value.binding.worker_namespace
  }
  rule {
    api_groups     = ["keda.sh"]
    resources      = ["scaledjobs"]
    resource_names = [each.value.binding.worker_scaled_job]
    verbs          = ["get"]
  }
  rule {
    api_groups     = [""]
    resources      = ["serviceaccounts"]
    resource_names = [each.value.binding.worker_service_account]
    verbs          = ["get"]
  }
  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = ["${each.value.binding.worker_scaled_job}-config"]
    verbs          = ["get"]
  }
  # Actual protected bootstrap verifies an exact pod UID. Dynamic pod names
  # require namespaced get, not list/watch/update or Secret access.
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get"]
  }
  depends_on = [terraform_data.domain_operation_registration]
}

resource "kubernetes_role_binding" "gateway_domain_operation_read" {
  for_each = var.domain_operation_bindings
  metadata {
    name      = "adp-gateway-domain-operation-read"
    namespace = each.value.binding.worker_namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.gateway_domain_operation_read[each.key].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "gateway-service"
    namespace = var.gateway_namespace
  }
}

output "domain_operation_binding_revisions" {
  description = "Canonical configured binding revision; independent live proof is still required. No readiness is asserted."
  value = { for key, value in var.domain_operation_bindings : key => sha256(jsonencode(merge(value.binding, { worker_image_digests = sort(tolist(value.binding.worker_image_digests)) })))
  }
}
