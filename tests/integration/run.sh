#!/usr/bin/env bash
# Run the integration suite (tests/integration) on a throwaway kind cluster, then delete it.
#
# The cluster gets its own kubeconfig file in a temporary directory: the default kubeconfig
# is never read or written. The node image is the one the CI jobs pin. AWS is moto only.
# Needs Docker, kind, kubectl and terraform (the harness renders the namespace policy), and
# network access for the pinned metrics-server manifest (metrics-server.sh).
#
# Environment:
#   PYTHON        interpreter with requirements-dev.txt (default .venv/bin/python)
#   KIND_CLUSTER  cluster name (default kh-int-<random>); must not exist yet, and the cluster
#                 this script creates is deleted at the end either way
# Extra arguments go to pytest, for example: tests/integration/run.sh -k approval
set -euo pipefail

cd "$(dirname "$0")/../.."
PYTHON=${PYTHON:-.venv/bin/python}
NAME=${KIND_CLUSTER:-kh-int-$RANDOM}
NODE_IMAGE=$(sed -n 's/^ *node_image: *//p' .github/workflows/ci.yml | head -n 1)
[ -n "$NODE_IMAGE" ] || { echo "no node_image in .github/workflows/ci.yml" >&2; exit 2; }
for tool in kind kubectl terraform; do
  command -v "$tool" >/dev/null || { echo "$tool not found" >&2; exit 2; }
done

# Never adopt (and later delete) a cluster that another run or session created.
if kind get clusters 2>/dev/null | grep -qx "$NAME"; then
  echo "kind cluster $NAME already exists; set KIND_CLUSTER to an unused name" >&2
  exit 2
fi

dir=$(mktemp -d)
export KUBECONFIG="$dir/kubeconfig"
cleanup() {
  kind delete cluster --name "$NAME" --kubeconfig "$KUBECONFIG" || true
  rm -rf "$dir"
}
trap 'rm -rf "$dir"' EXIT

started=$(date +%s)
# kind removes a cluster it failed to create; the delete trap is set only once the cluster
# is ours, so a name taken meanwhile by someone else is never deleted.
kind create cluster --name "$NAME" --image "$NODE_IMAGE" --kubeconfig "$KUBECONFIG" --wait 120s
trap cleanup EXIT
tests/integration/metrics-server.sh
KH_INTEGRATION=1 "$PYTHON" -m pytest -q tests/integration "$@"
echo "integration suite: $(($(date +%s) - started))s including the cluster"
