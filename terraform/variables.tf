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
