#!/usr/bin/env bash
# Tear down the Milestone 0 slice in reverse order of setup:
#   0. remove the agent guidance (AGENTS.md and skills)  (AGENT_SPACE_ID; skipped with --delete-space,
#      and a failure only warns)
#   1. disassociate the MCP server from the agent space  (AGENT_SPACE_ID, ASSOCIATION_ID)
#   2. deregister the MCP server                         (SERVICE_ID)
#   3. delete the agent space                            (only with --delete-space)
#   4. empty the audit bucket, bypassing GOVERNANCE retention  (AUDIT_BUCKET, else the
#      audit_bucket_name output in TF_DIR; needs s3:BypassGovernanceRetention; COMPLIANCE
#      objects cannot be deleted before they expire)
#   5. terraform destroy in TF_DIR                       (default: terraform); first, if
#      build/k8s is missing, it is written from the k8s_manifests output
#   6. kubectl delete -f build/k8s, the rendered RBAC, ClusterRole and admission policy
#      (KUBECONFIG must be set), and the kube-handrail.io/managed label from the namespaces;
#      the namespaces themselves stay
#   7. kubectl delete -f k8s/sandbox: the sandbox namespace and its web Deployment
# Steps whose IDs are unset are skipped. Every step asks for confirmation unless --yes.
set -euo pipefail

usage() {
  echo "usage: $0 [--delete-space] [--skip-terraform] [--skip-k8s] [--yes]" >&2
  exit 2
}

delete_space=false
skip_terraform=false
skip_k8s=false
assume_yes=false
for arg in "$@"; do
  case "$arg" in
    --delete-space) delete_space=true ;;
    --skip-terraform) skip_terraform=true ;;
    --skip-k8s) skip_k8s=true ;;
    --yes) assume_yes=true ;;
    *) usage ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON:-python3}"
register=("$python" "$repo_root/scripts/devops_agent_register.py")
tf_dir="${TF_DIR:-$repo_root/terraform}"

confirm() {
  if $assume_yes; then
    return 0
  fi
  local answer
  read -r -p "$1 [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]]
}

if $delete_space; then
  echo "skip: remove agent guidance (deleting the agent space removes it)"
elif [[ -n "${AGENT_SPACE_ID:-}" ]]; then
  if confirm "Remove the kube-handrail AGENTS.md and skills from agent space ${AGENT_SPACE_ID}?"; then
    # Leftover guidance is harmless text, so a failure here must not stop the teardown.
    "${register[@]}" remove-guidance --agent-space-id "$AGENT_SPACE_ID" ||
      echo "warning: remove-guidance failed, continuing" >&2
  fi
else
  echo "skip: remove agent guidance (AGENT_SPACE_ID not set)"
fi

if [[ -n "${AGENT_SPACE_ID:-}" && -n "${ASSOCIATION_ID:-}" ]]; then
  if confirm "Disassociate MCP server association ${ASSOCIATION_ID}?"; then
    "${register[@]}" disassociate --agent-space-id "$AGENT_SPACE_ID" --association-id "$ASSOCIATION_ID"
  fi
else
  echo "skip: disassociate (AGENT_SPACE_ID or ASSOCIATION_ID not set)"
fi

if [[ -n "${SERVICE_ID:-}" ]]; then
  if confirm "Deregister MCP server ${SERVICE_ID}?"; then
    "${register[@]}" deregister --service-id "$SERVICE_ID"
  fi
else
  echo "skip: deregister (SERVICE_ID not set)"
fi

if $delete_space; then
  if [[ -z "${AGENT_SPACE_ID:-}" ]]; then
    echo "--delete-space requires AGENT_SPACE_ID" >&2
    exit 1
  fi
  if confirm "Delete agent space ${AGENT_SPACE_ID} and everything in it?"; then
    "${register[@]}" delete-space --agent-space-id "$AGENT_SPACE_ID" --yes
  fi
else
  echo "skip: delete agent space (pass --delete-space)"
fi

if [[ -z "${AUDIT_BUCKET:-}" && -d "$tf_dir" ]] && ! $skip_terraform; then
  # An empty or failed output (audit disabled, no state) leaves the step skipped.
  AUDIT_BUCKET="$(terraform -chdir="$tf_dir" output -raw audit_bucket_name 2>/dev/null || true)"
  if [[ "$AUDIT_BUCKET" == "null" || "$AUDIT_BUCKET" == *[[:space:]]* ]]; then
    AUDIT_BUCKET=""
  fi
fi

if $skip_terraform; then
  echo "skip: empty the audit bucket (--skip-terraform)"
elif [[ -n "${AUDIT_BUCKET:-}" ]]; then
  if confirm "Delete every object version in audit bucket ${AUDIT_BUCKET}, bypassing GOVERNANCE retention?"; then
    "$python" - "$AUDIT_BUCKET" <<'PY'
import sys

import boto3

bucket = sys.argv[1]
s3 = boto3.client("s3")
deleted = 0
for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
    objects = [
        {"Key": v["Key"], "VersionId": v["VersionId"]}
        for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
    ]
    if not objects:
        continue
    response = s3.delete_objects(
        Bucket=bucket, Delete={"Objects": objects, "Quiet": True}, BypassGovernanceRetention=True
    )
    errors = response.get("Errors", [])
    if errors:
        sys.exit(f"could not delete {len(errors)} versions, first: {errors[0]}")
    deleted += len(objects)
print(f"deleted {deleted} object versions from {bucket}")
PY
  fi
else
  echo "skip: empty the audit bucket (AUDIT_BUCKET not set and no audit_bucket_name output)"
fi

k8s_dir="$repo_root/build/k8s"
if ! $skip_k8s && ! $skip_terraform && [[ -d "$tf_dir" ]] && ! compgen -G "$k8s_dir/*.yaml" >/dev/null; then
  # The destroy removes the output; keep the manifests so step 6 can delete them.
  if manifests="$(terraform -chdir="$tf_dir" output -json k8s_manifests 2>/dev/null)" && [[ "$manifests" == "{"* ]]; then
    mkdir -p "$k8s_dir"
    "$python" -c 'import json, pathlib, sys
for name, text in json.loads(sys.argv[1]).items():
    pathlib.Path(sys.argv[2], name).write_text(text)' "$manifests" "$k8s_dir"
    echo "wrote build/k8s from the k8s_manifests output"
  fi
fi

if $skip_terraform; then
  echo "skip: terraform destroy (--skip-terraform)"
elif [[ ! -d "$tf_dir" ]]; then
  echo "skip: terraform destroy ($tf_dir does not exist)"
elif confirm "Run terraform destroy in ${tf_dir}?"; then
  # Extra arguments (for example -var-file) can be passed via TF_CLI_ARGS_destroy.
  if $assume_yes; then
    terraform -chdir="$tf_dir" destroy -auto-approve
  else
    terraform -chdir="$tf_dir" destroy
  fi
fi

if $skip_k8s; then
  echo "skip: cluster manifests and sandbox (--skip-k8s)"
else
  if [[ -z "${KUBECONFIG:-}" ]]; then
    echo "KUBECONFIG must be set to delete the cluster manifests (or pass --skip-k8s)" >&2
    exit 1
  fi
  context="$(kubectl config current-context)"
  if ! compgen -G "$k8s_dir/*.yaml" >/dev/null; then
    echo "skip: rendered manifests (no build/k8s; delete the kube-handrail Roles, the ClusterRole kube-handrail-policy-reader and the policy kube-handrail-writer-bounds by hand)"
  elif confirm "Delete the rendered manifests in build/k8s (RBAC in every policy namespace, ClusterRole kube-handrail-policy-reader, admission policy) from context ${context}?"; then
    kubectl delete -f "$k8s_dir" --ignore-not-found
    kubectl label namespace -l kube-handrail.io/managed=true kube-handrail.io/managed- || true
    rm -rf "$k8s_dir"
  fi
  if confirm "Delete k8s/sandbox (namespace kube-handrail-sandbox and its web Deployment) from context ${context}?"; then
    kubectl delete -f "$repo_root/k8s/sandbox" --ignore-not-found
  fi
fi
