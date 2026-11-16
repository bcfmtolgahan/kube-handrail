# The namespace policy and the cluster manifests rendered from it (mcp/CONTRACT.md,
# Namespace policy and Admission policy). No providers and no resources: the root module
# writes the outputs to files (terraform/k8s.tf, the build in main.tf), and the golden test
# (tests/k8s_render) renders fixtures through this module without AWS credentials.

locals {
  legacy_namespace = coalesce(var.sandbox_namespace, "kube-handrail-sandbox")
  legacy_min       = coalesce(var.min_replicas, 0)
  legacy_max       = coalesce(var.max_replicas, 5)

  # Every entry fully set, with sorted lists, so the rendered output is stable.
  namespaces = length(var.namespaces) > 0 ? {
    for name, ns in var.namespaces : name => {
      min_replicas   = ns.min_replicas
      max_replicas   = ns.max_replicas
      actions        = sort(tolist(ns.actions))
      approvers      = distinct(ns.approvers)
      gitops_markers = distinct(ns.gitops_markers)
    }
    } : {
    (local.legacy_namespace) = {
      min_replicas   = local.legacy_min
      max_replicas   = local.legacy_max
      actions        = ["restart", "rollback", "scale"]
      approvers      = []
      gitops_markers = []
    }
  }
  names = sort(keys(local.namespaces))

  # namespace_policy.json in the Lambda packages (handrail_common.config).
  policy_json = jsonencode({ namespaces = local.namespaces })

  # The policy's CEL variables are JSON objects, which are CEL map literals; the read Lambda
  # parses them back (handrail_common.admission) and compares them with bounds_for.
  min_map = jsonencode({ for name, ns in local.namespaces : name => ns.min_replicas })
  max_map = jsonencode({ for name, ns in local.namespaces : name => ns.max_replicas })

  validation_actions = var.admission_mode == "Deny" ? ["Deny"] : ["Warn", "Audit"]

  manifests = {
    "admission-policy.yaml" = templatefile("${path.module}/templates/admission-policy.yaml.tftpl", {
      names              = local.names
      min_map            = local.min_map
      max_map            = local.max_map
      validation_actions = local.validation_actions
    })
    "policy-reader.yaml" = templatefile("${path.module}/templates/policy-reader.yaml.tftpl", {})
    "rbac.yaml" = templatefile("${path.module}/templates/rbac.yaml.tftpl", {
      names      = local.names
      namespaces = local.namespaces
    })
  }
}
