# Inputs as the root module receives them (terraform/variables.tf validates them there).

variable "namespaces" {
  description = "Namespace => policy, as terraform/variables.tf namespaces. Empty derives one entry from the deprecated variables."
  type = map(object({
    min_replicas   = optional(number, 0)
    max_replicas   = optional(number, 5)
    actions        = optional(set(string), ["restart", "rollback", "scale"])
    approvers      = optional(list(string), [])
    gitops_markers = optional(list(string), [])
  }))
  default = {}
}

variable "sandbox_namespace" {
  description = "Deprecated: the only namespace, used when namespaces is empty."
  type        = string
  default     = null
}

variable "min_replicas" {
  description = "Deprecated: the minimum of that namespace."
  type        = number
  default     = null
}

variable "max_replicas" {
  description = "Deprecated: the maximum of that namespace."
  type        = number
  default     = null
}

variable "admission_mode" {
  description = "Warn or Deny: the validationActions of the admission policy binding."
  type        = string
  default     = "Deny"

  validation {
    condition     = contains(["Warn", "Deny"], var.admission_mode)
    error_message = "admission_mode must be Warn or Deny."
  }
}
