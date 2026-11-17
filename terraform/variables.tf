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

  # It starts the Cognito domain prefix, which may not contain these words.
  validation {
    condition     = !can(regex("aws|amazon|cognito", var.name_prefix))
    error_message = "name_prefix must not contain aws, amazon or cognito (reserved in Cognito domain names)."
  }
}

variable "namespaces" {
  description = "Namespaces the tools may plan and apply changes in, each with its replica bounds, the actions it allows (restart, rollback, scale by default; adjust_hpa only when listed, which lets the writer patch the namespace's HorizontalPodAutoscalers), its own approvers (IAM user or role ARNs; when set, only they decide its plans, otherwise approver_principals) and GitOps marker keys checked in addition to gitops_markers. Empty (the default) derives one namespace from the deprecated sandbox_namespace, min_replicas and max_replicas, which is what earlier releases did."
  type = map(object({
    min_replicas   = optional(number, 0)
    max_replicas   = optional(number, 5)
    actions        = optional(set(string), ["restart", "rollback", "scale"])
    approvers      = optional(list(string), [])
    gitops_markers = optional(list(string), [])
  }))
  default = {}

  validation {
    condition     = alltrue([for name in keys(var.namespaces) : length(name) <= 63 && can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name))])
    error_message = "namespaces keys must be Kubernetes namespace names (DNS-1123 labels)."
  }

  validation {
    condition     = alltrue([for ns in values(var.namespaces) : try(ns.min_replicas >= 0 && ns.min_replicas <= ns.max_replicas && floor(ns.min_replicas) == ns.min_replicas && floor(ns.max_replicas) == ns.max_replicas, false)])
    error_message = "Each namespace needs integers 0 <= min_replicas <= max_replicas."
  }

  validation {
    condition     = alltrue([for ns in values(var.namespaces) : length(setsubtract(ns.actions, ["adjust_hpa", "restart", "rollback", "scale"])) == 0])
    error_message = "A namespace's actions must be adjust_hpa, restart, rollback or scale."
  }

  validation {
    condition     = alltrue(flatten([for ns in values(var.namespaces) : [for arn in ns.approvers : can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:(user|role)/.+$", arn))]]))
    error_message = "A namespace's approvers must be IAM user or role ARNs."
  }

  validation {
    condition     = alltrue(flatten([for ns in values(var.namespaces) : [for key in ns.gitops_markers : can(regex("^([a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", key))]]))
    error_message = "A namespace's gitops_markers must be Kubernetes label or annotation keys."
  }
}

# Deprecated: namespaces replaces them. null means not set; the check
# "deprecated_namespace_variables" in k8s.tf warns while any is set.
variable "sandbox_namespace" {
  description = "Deprecated, use namespaces. The only namespace when namespaces is empty (null: kube-handrail-sandbox)."
  type        = string
  default     = null
}

variable "min_replicas" {
  description = "Deprecated, use namespaces. Lowest replica count a plan may request when namespaces is empty (null: 0)."
  type        = number
  default     = null
}

variable "max_replicas" {
  description = "Deprecated, use namespaces. Highest replica count a plan may request when namespaces is empty (null: 5)."
  type        = number
  default     = null
}

variable "admission_mode" {
  description = "validationActions of the admission policy binding in build/k8s: Deny refuses the writer's out-of-bounds changes, Warn admits them with a warning and an audit annotation (a trial; plans need admission_check = false then, because the read Lambda requires Deny)."
  type        = string
  default     = "Deny"

  validation {
    condition     = contains(["Warn", "Deny"], var.admission_mode)
    error_message = "admission_mode must be Warn or Deny."
  }
}

variable "admission_check" {
  description = "Whether the read Lambda compares the cluster's admission policy with its own bounds on every plan and refuses the plan (conflict) when they differ or the policy cannot be read. Turn it off only for a cluster that does not run the policy (Kubernetes below 1.30)."
  type        = bool
  default     = true
}

variable "plan_ttl_seconds" {
  description = "Seconds a plan stays valid, at most 3600. null (the default) picks by approval_mode: 900 in client mode, where the human approves in the chat after reading the plan, and 300 in server and both mode, where request_approval extends it to approval_wait_ttl_seconds."
  type        = number
  default     = null

  validation {
    condition     = var.plan_ttl_seconds == null || try(var.plan_ttl_seconds > 0 && var.plan_ttl_seconds <= 3600 && floor(var.plan_ttl_seconds) == var.plan_ttl_seconds, false)
    error_message = "plan_ttl_seconds must be null or an integer from 1 to 3600."
  }
}

variable "gitops_markers" {
  description = "Label or annotation keys that mark a Deployment as GitOps-managed, in addition to the defaults (the Argo CD tracking-id annotation and the Flux kustomize and helm controller labels). A Deployment with any of these keys, as a label or an annotation, is refused with gitops_managed. Add \"app.kubernetes.io/instance\" when Argo CD uses label tracking; it is not a default because Helm sets it on every release."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for key in var.gitops_markers : can(regex("^([a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", key))])
    error_message = "gitops_markers must be Kubernetes label or annotation keys, such as app.kubernetes.io/instance."
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

# Deprecated and ignored: the plan rate limit was removed. Kept one release so existing
# tfvars still plan; the check "deprecated_plan_rate" in main.tf warns while either is set.
variable "plan_rate_limit" {
  description = "Deprecated, ignored: the plan rate limit was removed. Remove it from your tfvars."
  type        = number
  default     = null
}

variable "plan_rate_window_seconds" {
  description = "Deprecated, ignored: the plan rate limit was removed. Remove it from your tfvars."
  type        = number
  default     = null
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

variable "agents" {
  description = "Agents by name, one OAuth client each. An entry without client_id gets a Cognito app client created here; with client_id, that existing client of the user pool is allowed instead. hourly_apply_budget overrides agent_hourly_apply_budget for that agent. The default keys are the app clients earlier versions created, so they are kept on upgrade."
  type = map(object({
    client_id           = optional(string)
    hourly_apply_budget = optional(number)
  }))
  default = {
    devops_agent = {}
    # The smoke test and the end-to-end suite apply more than the default budget per hour.
    smoke_test = { hourly_apply_budget = 30 }
  }

  validation {
    condition     = length(var.agents) > 0 && alltrue([for name in keys(var.agents) : can(regex("^[a-z][a-z0-9_]{0,31}$", name))])
    error_message = "agents must name at least one agent, each matching ^[a-z][a-z0-9_]{0,31}$."
  }

  validation {
    condition     = alltrue([for a in values(var.agents) : a.client_id == null || try(length(a.client_id) > 0, false)])
    error_message = "An agent's client_id must not be empty; leave it out to create a client."
  }

  validation {
    condition     = alltrue([for a in values(var.agents) : a.hourly_apply_budget == null || try(a.hourly_apply_budget > 0 && floor(a.hourly_apply_budget) == a.hourly_apply_budget, false)])
    error_message = "An agent's hourly_apply_budget must be a positive integer."
  }
}

variable "agent_hourly_apply_budget" {
  description = "Applies allowed per agent in one clock hour, for agents without their own hourly_apply_budget. The global budget applies as well."
  type        = number
  default     = 10

  validation {
    condition     = var.agent_hourly_apply_budget > 0 && floor(var.agent_hourly_apply_budget) == var.agent_hourly_apply_budget
    error_message = "agent_hourly_apply_budget must be a positive integer."
  }
}

variable "verify_window_default_seconds" {
  description = "Verification window of a plan that gives no verify_window_seconds: seconds after the apply at which the result is checked."
  type        = number
  default     = 120

  validation {
    condition     = var.verify_window_default_seconds > 0 && floor(var.verify_window_default_seconds) == var.verify_window_default_seconds
    error_message = "verify_window_default_seconds must be a positive integer."
  }
}

variable "verify_window_min_seconds" {
  description = "Shortest verify_window_seconds a plan may give; at least 60, because the verifier runs once a minute."
  type        = number
  default     = 60

  validation {
    condition     = var.verify_window_min_seconds >= 60 && floor(var.verify_window_min_seconds) == var.verify_window_min_seconds
    error_message = "verify_window_min_seconds must be an integer of at least 60."
  }
}

variable "verify_window_max_seconds" {
  description = "Longest verify_window_seconds a plan may give."
  type        = number
  default     = 1800

  validation {
    condition     = var.verify_window_max_seconds > 0 && floor(var.verify_window_max_seconds) == var.verify_window_max_seconds
    error_message = "verify_window_max_seconds must be a positive integer."
  }
}

variable "verify_grace_seconds" {
  description = "How long after its window a verification may still run; a check that could not run by then is reported as unknown (late) and never reverts."
  type        = number
  default     = 300

  validation {
    condition     = var.verify_grace_seconds > 0 && floor(var.verify_grace_seconds) == var.verify_grace_seconds
    error_message = "verify_grace_seconds must be a positive integer."
  }
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

variable "lambda_subnet_ids" {
  description = "Subnets for the two tool Lambdas, which call the EKS API. Required for a cluster with a private endpoint only. Empty runs them outside any VPC. See docs/deploy.md, Network."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.lambda_subnet_ids : can(regex("^subnet-[0-9a-f]+$", id))])
    error_message = "lambda_subnet_ids must be subnet IDs."
  }
}

variable "lambda_security_group_ids" {
  description = "Security groups for the tool Lambdas in lambda_subnet_ids. Empty creates one that allows outbound HTTPS only."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.lambda_security_group_ids : can(regex("^sg-[0-9a-f]+$", id))])
    error_message = "lambda_security_group_ids must be security group IDs."
  }
}

variable "allow_lambda_to_cluster_endpoint" {
  description = "Add an inbound HTTPS rule from the tool Lambdas' security groups to the cluster security group. Off by default because it changes a security group of the cluster. Needs private endpoint access on the cluster."
  type        = bool
  default     = false
}

variable "create_vpc_endpoints" {
  description = "Create the VPC endpoints the tool Lambdas need without a NAT gateway: interface endpoints for EKS, SSM and SNS, and a DynamoDB gateway endpoint on the route tables of lambda_subnet_ids. Needs private endpoint access on the cluster. Leave off if the VPC already has endpoints for these services."
  type        = bool
  default     = false
}

variable "audit_enabled" {
  description = "Write every change to a plan record, from the plan table's stream, once to an S3 bucket with Object Lock (mcp/CONTRACT.md, Audit record). Enabling it turns the table's stream on in place."
  type        = bool
  default     = true
}

variable "audit_object_lock_mode" {
  description = "Default Object Lock mode of audit objects. GOVERNANCE: a principal allowed s3:BypassGovernanceRetention can still delete them (test deployments). COMPLIANCE: nobody can delete them, or the bucket, before the retention ends (recommended for production; see docs/deploy.md)."
  type        = string
  default     = "GOVERNANCE"

  validation {
    condition     = contains(["GOVERNANCE", "COMPLIANCE"], var.audit_object_lock_mode)
    error_message = "audit_object_lock_mode must be GOVERNANCE or COMPLIANCE."
  }
}

variable "audit_retention_days" {
  description = "Default Object Lock retention of each audit object, in days."
  type        = number
  default     = 30

  validation {
    condition     = var.audit_retention_days >= 1 && floor(var.audit_retention_days) == var.audit_retention_days
    error_message = "audit_retention_days must be a positive integer."
  }
}

variable "audit_force_destroy" {
  description = "Let terraform destroy empty the audit bucket. Objects under GOVERNANCE retention are deleted only if the caller is allowed s3:BypassGovernanceRetention; COMPLIANCE objects cannot be deleted before their retention ends. Test deployments only."
  type        = bool
  default     = false
}

# Deprecated and ignored: the interceptor and the verifier are always deployed. Kept one
# release so existing tfvars still plan; the check "deprecated_switches" in main.tf warns
# while either is set.
variable "caller_interceptor_enabled" {
  description = "Deprecated, ignored: the gateway request interceptor is always deployed (legacy mode was removed). Remove it from your tfvars."
  type        = bool
  default     = null
}

variable "verify_enabled" {
  description = "Deprecated, ignored: every applied plan is verified. Remove it from your tfvars."
  type        = bool
  default     = null
}
