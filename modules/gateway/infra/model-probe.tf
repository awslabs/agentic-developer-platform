# Model invocability probing (#7967). The model-policy plane
# (ADP_CHAT_MODEL_POLICY_ENABLED, required by delegated chat) fails closed unless
# fresh invocability evidence exists for the persona's model, and only the probe
# records that evidence. These controls used to be committed literals in
# modules/gateway/k8s/configmap.yaml, so no environment could ever hold evidence
# and every delegated chat turn was refused with probing_disabled. They are now
# per-environment configuration: gateway tfvars -> these SSM parameters ->
# gateway-deploy.yml / platform/scripts/deploy-all.sh render -> BG_MODEL_PROBE_*
# (src/shared/config.py Settings, BG_ prefix). Defaults keep probing off; the
# probe CronJob itself is unsuspended separately by webhook-ingress
# persona_model_probe_enabled (modules/agent-factory/webhook-ingress/infra/
# persona-model-probe.tf). An environment whose chat model-policy posture is
# "enforced" (chat_model_policy_posture, chief-of-staff-flags.tf, #7997) MUST arm
# both, and deploy-all.sh --chat delegated refuses to roll out when it is not;
# under the default "advisory" posture the same checks only warn.

variable "model_probe_enabled" {
  description = "Admit paid invocability probe slots (BG_MODEL_PROBE_ENABLED). Required whenever chat_model_policy_posture is enforced."
  type        = bool
  default     = false
}

variable "model_probe_max_slots_per_cycle" {
  description = "Probe slots the gateway admits per daily cycle (BG_MODEL_PROBE_MAX_SLOTS_PER_CYCLE); 0 refuses every claim."
  type        = number
  default     = 0
  validation {
    condition     = var.model_probe_max_slots_per_cycle >= 0 && var.model_probe_max_slots_per_cycle <= 100 && floor(var.model_probe_max_slots_per_cycle) == var.model_probe_max_slots_per_cycle
    error_message = "model_probe_max_slots_per_cycle must be a whole number between 0 and 100."
  }
}

variable "model_probe_budget_usd_per_cycle" {
  description = "Spend ceiling in USD per probe cycle (BG_MODEL_PROBE_BUDGET_USD_PER_CYCLE), as a decimal string; \"0\" refuses every claim."
  type        = string
  default     = "0"
  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,6})?$", var.model_probe_budget_usd_per_cycle))
    error_message = "model_probe_budget_usd_per_cycle must be a non-negative decimal string with at most six fractional digits."
  }
}

variable "model_probe_max_budget_usd_per_attempt" {
  description = "Worst-case spend reserved per probe attempt in USD (BG_MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT), as a decimal string; \"0\" refuses every claim."
  type        = string
  default     = "0"
  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,6})?$", var.model_probe_max_budget_usd_per_attempt))
    error_message = "model_probe_max_budget_usd_per_attempt must be a non-negative decimal string with at most six fractional digits."
  }
}

variable "model_probe_model_allowlist" {
  description = "Catalogue model IDs the probe may claim (BG_MODEL_PROBE_MODEL_ALLOWLIST); empty means the whole catalogue, including the Claude models chat resolves to."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for item in var.model_probe_model_allowlist : length(trimspace(item)) > 0])
    error_message = "model_probe_model_allowlist entries must be non-empty model IDs."
  }
}

variable "model_probe_evidence_ttl_hours" {
  description = "Hours a proven result stays fresh (BG_MODEL_PROBE_EVIDENCE_TTL_HOURS); keep it above one missed daily cycle so chat does not fail closed."
  type        = number
  default     = 24
  validation {
    condition     = var.model_probe_evidence_ttl_hours >= 1 && var.model_probe_evidence_ttl_hours <= 720 && floor(var.model_probe_evidence_ttl_hours) == var.model_probe_evidence_ttl_hours
    error_message = "model_probe_evidence_ttl_hours must be a whole number between 1 and 720."
  }
}

resource "aws_ssm_parameter" "model_probe_enabled" {
  name  = "/adp/${var.environment}/gateway/model-probe-enabled"
  type  = "String"
  value = tostring(var.model_probe_enabled)
}

resource "aws_ssm_parameter" "model_probe_max_slots_per_cycle" {
  name  = "/adp/${var.environment}/gateway/model-probe-max-slots-per-cycle"
  type  = "String"
  value = tostring(var.model_probe_max_slots_per_cycle)
}

resource "aws_ssm_parameter" "model_probe_budget_usd_per_cycle" {
  name  = "/adp/${var.environment}/gateway/model-probe-budget-usd-per-cycle"
  type  = "String"
  value = var.model_probe_budget_usd_per_cycle
}

resource "aws_ssm_parameter" "model_probe_max_budget_usd_per_attempt" {
  name  = "/adp/${var.environment}/gateway/model-probe-max-budget-usd-per-attempt"
  type  = "String"
  value = var.model_probe_max_budget_usd_per_attempt
}

resource "aws_ssm_parameter" "model_probe_model_allowlist" {
  name  = "/adp/${var.environment}/gateway/model-probe-model-allowlist"
  type  = "String"
  value = jsonencode(var.model_probe_model_allowlist)
}

resource "aws_ssm_parameter" "model_probe_evidence_ttl_hours" {
  name  = "/adp/${var.environment}/gateway/model-probe-evidence-ttl-hours"
  type  = "String"
  value = tostring(var.model_probe_evidence_ttl_hours)
}
