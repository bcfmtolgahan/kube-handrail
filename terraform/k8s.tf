# --- Namespace policy and cluster manifests (mcp/CONTRACT.md, Namespace policy) -------
#
# One map, var.namespaces, drives both sides: the namespace policy the Lambda packages
# carry (namespace_policy.json, in the build in main.tf) and the cluster manifests written
# to build/k8s: per namespace a reader Role and, where an action is allowed, a writer Role,
# with their bindings; one ValidatingAdmissionPolicy with the bounds of every namespace;
# and the ClusterRole that lets the read Lambda read that policy. Terraform does not talk
# to the cluster (no kubernetes provider): apply build/k8s with kubectl, or with
# make upgrade UPGRADE_FLAGS=--with-k8s. Namespaces are not created here; they must exist.

module "namespace_policy" {
  source = "./modules/namespace-policy"

  namespaces        = var.namespaces
  sandbox_namespace = var.sandbox_namespace
  min_replicas      = var.min_replicas
  max_replicas      = var.max_replicas
  admission_mode    = var.admission_mode
}

locals {
  namespaces            = module.namespace_policy.namespaces
  namespace_policy_json = module.namespace_policy.policy_json
  k8s_manifests         = module.namespace_policy.manifests

  # The bounds MIN_REPLICAS and MAX_REPLICAS keep: the deprecated variables or their
  # defaults. The Lambdas use them only without a policy file in their package.
  legacy_min_replicas = coalesce(var.min_replicas, 0)
  legacy_max_replicas = coalesce(var.max_replicas, 5)

  # Every principal that may decide in some namespace (approval.tf).
  all_approver_principals = distinct(concat(
    var.approver_principals,
    flatten([for ns in values(local.namespaces) : ns.approvers]),
  ))
}

# Rewritten whenever a manifest changes. A deleted build/k8s comes back with the next
# change, or at once with make upgrade UPGRADE_FLAGS=--with-k8s, which writes it from the
# k8s_manifests output.
resource "terraform_data" "k8s_manifests" {
  triggers_replace = {
    manifests = sha256(jsonencode(local.k8s_manifests))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    working_dir = local.repo_root
    environment = {
      MANIFESTS = jsonencode(local.k8s_manifests)
      PYTHON    = var.python
    }
    command = <<-EOT
      set -euo pipefail
      rm -rf build/k8s
      mkdir -p build/k8s
      "$PYTHON" -c 'import json, os, pathlib
      for name, text in json.loads(os.environ["MANIFESTS"]).items():
          pathlib.Path("build/k8s", name).write_text(text)'
    EOT
  }

  lifecycle {
    precondition {
      condition     = length(var.namespaces) == 0 || (var.sandbox_namespace == null && var.min_replicas == null && var.max_replicas == null)
      error_message = "Set either namespaces or the deprecated sandbox_namespace, min_replicas and max_replicas, not both: with namespaces set they would be ignored. Move their values into namespaces and remove them."
    }
  }
}

check "deprecated_namespace_variables" {
  assert {
    condition     = var.sandbox_namespace == null && var.min_replicas == null && var.max_replicas == null
    error_message = "sandbox_namespace, min_replicas and max_replicas are deprecated; they still work while namespaces is empty. Replace them with namespaces = { \"${module.namespace_policy.allowed_namespaces}\" = { min_replicas = ${local.legacy_min_replicas}, max_replicas = ${local.legacy_max_replicas} } }, which renders the same policy; they will be deleted in a later release."
  }
}

check "namespace_approvers_need_server_approval" {
  assert {
    condition     = var.approval_mode != "client" || alltrue([for ns in values(local.namespaces) : length(ns.approvers) == 0])
    error_message = "A namespace lists approvers while approval_mode is client: they take effect only in server or both mode, where the approver checks them; in client mode the client's own approval decides."
  }
}

check "admission_mode_and_check" {
  assert {
    condition     = var.admission_mode == "Deny" || !var.admission_check
    error_message = "admission_mode is Warn while admission_check is on: the read Lambda refuses every plan (conflict) until the binding is in Deny. Set admission_mode = \"Deny\", or admission_check = false for the trial."
  }
}
