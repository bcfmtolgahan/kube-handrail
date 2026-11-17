# The namespace policy of the integration suite (tests/integration/harness.py), rendered
# by tests/k8s_render/render.sh at the start of a session. The sandbox allows every action,
# adjust_hpa included, with the global approvers; the second namespace restarts only and has
# its own approver.
# 123456789012 is the AWS documentation's example account; nothing real.
namespaces = {
  "kube-handrail-sandbox" = {
    min_replicas = 0
    max_replicas = 5
    actions      = ["adjust_hpa", "restart", "rollback", "scale"]
  }
  "kh-int-restart-only" = {
    min_replicas = 1
    max_replicas = 3
    actions      = ["restart"]
    approvers    = ["arn:aws:iam::123456789012:role/kh-int-ns2-approver"]
  }
}
