output "namespaces" {
  description = "Namespace => {min_replicas, max_replicas, actions, approvers, gitops_markers}, with the deprecated variables' entry when namespaces is empty."
  value       = local.namespaces
}

output "allowed_namespaces" {
  description = "ALLOWED_NAMESPACES: the namespaces, sorted, comma-separated."
  value       = join(",", local.names)
}

output "policy_json" {
  description = "namespace_policy.json as the Lambda packages carry it."
  value       = local.policy_json
}

output "manifests" {
  description = "File name => content of the cluster manifests (build/k8s)."
  value       = local.manifests
}

output "label_command" {
  description = "The kubectl command that labels the namespaces as kube-handrail's (an inventory label; the manifests select namespaces by name)."
  value       = "kubectl label namespace ${join(" ", local.names)} kube-handrail.io/managed=true --overwrite"
}
