#!/usr/bin/env bash
# Tear down the Milestone 0 slice in reverse order of setup:
#   1. disassociate the MCP server from the agent space  (AGENT_SPACE_ID, ASSOCIATION_ID)
#   2. deregister the MCP server                         (SERVICE_ID)
#   3. delete the agent space                            (only with --delete-space)
#   4. terraform destroy in TF_DIR                       (default: terraform)
#   5. kubectl delete -f k8s/sandbox                     (KUBECONFIG must be set)
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
  echo "skip: sandbox manifests (--skip-k8s)"
else
  if [[ -z "${KUBECONFIG:-}" ]]; then
    echo "KUBECONFIG must be set to delete the sandbox manifests (or pass --skip-k8s)" >&2
    exit 1
  fi
  context="$(kubectl config current-context)"
  if confirm "Delete k8s/sandbox (namespace kube-handrail-sandbox) from context ${context}?"; then
    kubectl delete -f "$repo_root/k8s/sandbox" --ignore-not-found
  fi
fi
