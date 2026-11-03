variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "eu-central-1"
}

variable "cluster_name" {
  description = "Name of the existing EKS cluster the tools operate on. Its authentication mode must be API or API_AND_CONFIG_MAP."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
  default     = "kube-handrail"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-32 lowercase letters, digits or hyphens."
  }
}

variable "sandbox_namespace" {
  description = "The only namespace the tools may plan or apply changes in."
  type        = string
  default     = "kube-handrail-sandbox"
}

variable "min_replicas" {
  description = "Lowest replica count a plan may request."
  type        = number
  default     = 0
}

variable "max_replicas" {
  description = "Highest replica count a plan may request."
  type        = number
  default     = 5
}

variable "plan_ttl_seconds" {
  description = "Seconds a plan stays valid."
  type        = number
  default     = 300

  validation {
    condition     = var.plan_ttl_seconds > 0
    error_message = "plan_ttl_seconds must be positive."
  }
}

variable "flap_window_seconds" {
  description = "Window in which a second reversal of scaling direction on one Deployment is rejected as flapping."
  type        = number
  default     = 600

  validation {
    condition     = var.flap_window_seconds > 0 && floor(var.flap_window_seconds) == var.flap_window_seconds
    error_message = "flap_window_seconds must be a positive integer."
  }
}

variable "object_hourly_apply_budget" {
  description = "Applies allowed per Deployment in any 3600-second window."
  type        = number
  default     = 6

  validation {
    condition     = var.object_hourly_apply_budget > 0 && floor(var.object_hourly_apply_budget) == var.object_hourly_apply_budget
    error_message = "object_hourly_apply_budget must be a positive integer."
  }
}

variable "plan_rate_limit" {
  description = "Plans allowed per Deployment in plan_rate_window_seconds."
  type        = number
  default     = 20

  validation {
    condition     = var.plan_rate_limit > 0 && floor(var.plan_rate_limit) == var.plan_rate_limit
    error_message = "plan_rate_limit must be a positive integer."
  }
}

variable "plan_rate_window_seconds" {
  description = "Window for plan_rate_limit."
  type        = number
  default     = 600

  validation {
    condition     = var.plan_rate_window_seconds > 0 && floor(var.plan_rate_window_seconds) == var.plan_rate_window_seconds
    error_message = "plan_rate_window_seconds must be a positive integer."
  }
}

variable "apply_cooldown_seconds" {
  description = "Minimum seconds between two applies to one Deployment; 0 turns the cooldown off."
  type        = number
  default     = 60

  validation {
    condition     = var.apply_cooldown_seconds >= 0 && floor(var.apply_cooldown_seconds) == var.apply_cooldown_seconds
    error_message = "apply_cooldown_seconds must be a non-negative integer."
  }
}

variable "global_hourly_apply_budget" {
  description = "Applies allowed across all objects in one clock hour."
  type        = number
  default     = 30

  validation {
    condition     = var.global_hourly_apply_budget > 0 && floor(var.global_hourly_apply_budget) == var.global_hourly_apply_budget
    error_message = "global_hourly_apply_budget must be a positive integer."
  }
}

variable "max_replica_step_percent" {
  description = "Blast-radius budget: largest change in replicas per plan, as a percentage of the current count (rounded up, at least 1)."
  type        = number
  default     = 100

  validation {
    condition     = var.max_replica_step_percent > 0 && floor(var.max_replica_step_percent) == var.max_replica_step_percent
    error_message = "max_replica_step_percent must be a positive integer."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda functions."
  type        = number
  default     = 30
}

variable "python" {
  description = "Local Python interpreter used to run pip during the build."
  type        = string
  default     = "python3"
}

variable "sentinel_enabled" {
  description = "Deploy the drift sentinel, which turns the kill switch off when the tools or their classification drift."
  type        = bool
  default     = true
}

variable "sentinel_schedule" {
  description = "EventBridge schedule expression for the drift sentinel. The unknown-result alarm assumes a run at least every 5 minutes."
  type        = string
  default     = "rate(5 minutes)"
}

variable "devops_agent_space_id" {
  description = "DevOps Agent space whose association the sentinel checks. Empty skips the DevOps Agent checks. Set it only after enable-write."
  type        = string
  default     = ""
}

variable "devops_agent_association_id" {
  description = "DevOps Agent association of the gateway. Set together with devops_agent_space_id."
  type        = string
  default     = ""
}

variable "expect_elevated_actions" {
  description = "Expected value of elevatedActionsEnabled on the agent space; any other value is drift."
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Email address subscribed to the alert topic. Empty adds no subscription."
  type        = string
  default     = ""
}

variable "approval_mode" {
  description = "client: the MCP client approves (DevOps Agent chat), the server requires no approval record. server: every apply requires a server approval. both: as server on the server side; the client approves as well."
  type        = string
  default     = "server"

  validation {
    condition     = contains(["client", "server", "both"], var.approval_mode)
    error_message = "approval_mode must be client, server or both."
  }
}

variable "approver_principals" {
  description = "IAM user or role ARNs allowed to approve or reject plans. A role ARN covers its assumed-role sessions. Required in server and both mode."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.approver_principals : can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:(user|role)/.+$", arn))])
    error_message = "approver_principals must be IAM user or role ARNs."
  }
}

variable "approval_require_mfa" {
  description = "Grant access to the approval URL only through the approver_invoke policy, which requires MFA. false (test setups only) grants the listed principals in the function's resource policy, without MFA."
  type        = bool
  default     = true
}

variable "approval_wait_ttl_seconds" {
  description = "Seconds from its creation a plan stays valid once an approval was requested. At least plan_ttl_seconds."
  type        = number
  default     = 900

  validation {
    condition     = var.approval_wait_ttl_seconds > 0 && floor(var.approval_wait_ttl_seconds) == var.approval_wait_ttl_seconds
    error_message = "approval_wait_ttl_seconds must be a positive integer."
  }
}

variable "apply_after_approval_ttl_seconds" {
  description = "Seconds after an approval within which the plan must be applied."
  type        = number
  default     = 120

  validation {
    condition     = var.apply_after_approval_ttl_seconds > 0 && floor(var.apply_after_approval_ttl_seconds) == var.apply_after_approval_ttl_seconds
    error_message = "apply_after_approval_ttl_seconds must be a positive integer."
  }
}

variable "approval_request_cooldown_seconds" {
  description = "Minimum seconds between two notifications for one plan; 0 turns the cooldown off."
  type        = number
  default     = 60

  validation {
    condition     = var.approval_request_cooldown_seconds >= 0 && floor(var.approval_request_cooldown_seconds) == var.approval_request_cooldown_seconds
    error_message = "approval_request_cooldown_seconds must be a non-negative integer."
  }
}

variable "approval_request_limit" {
  description = "Approval notifications allowed across all plans in one 600-second clock window."
  type        = number
  default     = 10

  validation {
    condition     = var.approval_request_limit > 0 && floor(var.approval_request_limit) == var.approval_request_limit
    error_message = "approval_request_limit must be a positive integer."
  }
}

variable "approval_email" {
  description = "Email address subscribed to the approval topic. Empty adds no subscription."
  type        = string
  default     = ""
}
