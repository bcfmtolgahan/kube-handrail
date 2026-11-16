#!/usr/bin/env bash
# Install metrics-server on the current kind cluster (KUBECONFIG), pinned by version and
# checksum, with --kubelet-insecure-tls (kind's kubelets serve self-signed certificates).
# The HPA adjust cases (test_kind_hpa.py) run against it; the HorizontalPodAutoscaler
# controller reads pod CPU from it. Used by tests/integration/run.sh and the CI job.
set -euo pipefail

VERSION=v0.7.2
SHA256=f103539a54ed72efe66616afc74a8bfaed651703cb3918797599046af5617441
URL="https://github.com/kubernetes-sigs/metrics-server/releases/download/$VERSION/components.yaml"

dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
curl -sSfL -o "$dir/components.yaml" "$URL"
if command -v sha256sum >/dev/null; then
  sum=$(sha256sum "$dir/components.yaml" | cut -d' ' -f1)
else
  sum=$(shasum -a 256 "$dir/components.yaml" | cut -d' ' -f1)
fi
if [[ "$sum" != "$SHA256" ]]; then
  echo "metrics-server $VERSION manifest checksum mismatch: $sum" >&2
  exit 1
fi
kubectl apply -f "$dir/components.yaml" >/dev/null
kubectl -n kube-system patch deployment metrics-server --type json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s
