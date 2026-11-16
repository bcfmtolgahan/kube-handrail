# Two namespaces with different bounds and actions: the first restarts only, the second
# allows every action with its own approver and GitOps marker. Used by the golden test
# and by tests/admission/run.sh on kind (CI job admission).
namespaces = {
  "kube-handrail-sandbox" = {
    min_replicas = 1
    max_replicas = 3
    actions      = ["restart"]
  }
  "kube-handrail-sandbox-2" = {
    min_replicas   = 0
    max_replicas   = 6
    approvers      = ["arn:aws:iam::123456789012:role/sandbox-2-approver"]
    gitops_markers = ["example.com/owner"]
  }
}
