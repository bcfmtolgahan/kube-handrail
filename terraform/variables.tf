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
