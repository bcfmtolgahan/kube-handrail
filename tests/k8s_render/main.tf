# Renders the namespace policy and the cluster manifests for a fixture, without AWS
# credentials: the same module the root uses (terraform/k8s.tf). render.sh drives it.

terraform {
  required_version = ">= 1.5.0"

  # render.sh points it at a temporary file (-backend-config=path=...).
  backend "local" {}
}

variable "namespaces" {
  type    = any
  default = {}
}

variable "sandbox_namespace" {
  type    = string
  default = null
}

variable "min_replicas" {
  type    = number
  default = null
}

variable "max_replicas" {
  type    = number
  default = null
}

variable "admission_mode" {
  type    = string
  default = "Deny"
}

module "policy" {
  source = "../../terraform/modules/namespace-policy"

  namespaces        = var.namespaces
  sandbox_namespace = var.sandbox_namespace
  min_replicas      = var.min_replicas
  max_replicas      = var.max_replicas
  admission_mode    = var.admission_mode
}

output "files" {
  value = merge(module.policy.manifests, {
    "namespace_policy.json"  = "${module.policy.policy_json}\n"
    "label_command.txt"      = "${module.policy.label_command}\n"
    "allowed_namespaces.txt" = "${module.policy.allowed_namespaces}\n"
  })
}
