#!/usr/bin/env bash
# Run the integration suite (tests/integration) on a throwaway kind cluster, then delete it.
#
# The cluster gets its own kubeconfig file in a temporary directory: the default kubeconfig
# is never read or written. The node image is the one the CI jobs pin. AWS is moto only.
#
# Environment:
#   PYTHON        interpreter with requirements-dev.txt (default .venv/bin/python)
#   KIND_CLUSTER  cluster name (default kh-int-<random>); deleted at the end either way
# Extra arguments go to pytest, for example: tests/integration/run.sh -k approval
set -euo pipefail

cd "$(dirname "$0")/../.."
PYTHON=${PYTHON:-.venv/bin/python}
NAME=${KIND_CLUSTER:-kh-int-$RANDOM}
NODE_IMAGE=$(sed -n 's/^ *node_image: *//p' .github/workflows/ci.yml | head -n 1)
[ -n "$NODE_IMAGE" ] || { echo "no node_image in .github/workflows/ci.yml" >&2; exit 2; }

dir=$(mktemp -d)
export KUBECONFIG="$dir/kubeconfig"
cleanup() {
  kind delete cluster --name "$NAME" --kubeconfig "$KUBECONFIG" || true
  rm -rf "$dir"
}
trap cleanup EXIT

started=$(date +%s)
kind create cluster --name "$NAME" --image "$NODE_IMAGE" --kubeconfig "$KUBECONFIG" --wait 120s
KH_INTEGRATION=1 "$PYTHON" -m pytest -q tests/integration "$@"
echo "integration suite: $(($(date +%s) - started))s including the cluster"
