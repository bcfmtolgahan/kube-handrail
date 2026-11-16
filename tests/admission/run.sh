#!/usr/bin/env bash
# Server-side dry-run cases for k8s/sandbox/admission-policy.yaml (mcp/CONTRACT.md,
# Admission policy). Every request is a dry run: nothing in the cluster changes, except the
# binding's validationActions, which the script switches to Deny and back, and the other
# namespace, which it creates and deletes only with OTHER_NAMESPACE set.
#
# Needs kubectl 1.30 or later and a kubeconfig whose user may impersonate users and groups
# (cluster-admin). The policy, the sandbox RBAC and the Deployment must already exist.
#
# Environment:
#   NAMESPACE        sandbox namespace (default kube-handrail-sandbox)
#   DEPLOYMENT       Deployment in it, at a count below the maximum (default web)
#   MIN_REPLICAS     the policy's minimum (default 0)
#   MAX_REPLICAS     the policy's maximum (default 5)
#   OTHER_NAMESPACE  if set, create this namespace with a Deployment, check that the policy
#                    does not touch it, and delete it again. Leave it unset on a shared cluster.
#   KEEP_DENY        if 1, leave the binding in Deny at the end; otherwise restore the
#                    validationActions it had.
set -euo pipefail

NAMESPACE=${NAMESPACE:-kube-handrail-sandbox}
DEPLOYMENT=${DEPLOYMENT:-web}
MIN_REPLICAS=${MIN_REPLICAS:-0}
MAX_REPLICAS=${MAX_REPLICAS:-5}
POLICY=kube-handrail-writer-bounds
WRITER=(--as kube-handrail-admission-test --as-group kube-handrail-writer)
# system:masters passes RBAC, so the only thing that can stop these requests is the policy.
WRITER_ADMIN=(--as kube-handrail-admission-test --as-group kube-handrail-writer --as-group system:masters)
OTHER_ADMIN=(--as kube-handrail-admission-other --as-group system:masters)
DENIAL="ValidatingAdmissionPolicy '$POLICY' with binding '$POLICY' denied request"
WARNING="Validation failed for ValidatingAdmissionPolicy '$POLICY' with binding '$POLICY'"

failures=0
pass() { echo "PASS  $1"; }
fail() {
  echo "FAIL  $1"
  shift
  printf '      %s\n' "$@"
  failures=$((failures + 1))
}

# patch_replicas <namespace> <deployment> <replicas> <identity...>: prints output, returns status.
patch_replicas() {
  local ns=$1 name=$2 replicas=$3
  shift 3
  kubectl patch deployment "$name" -n "$ns" --dry-run=server --type merge \
    -p "{\"spec\":{\"replicas\":$replicas}}" "$@" 2>&1
}

# scale_replicas <replicas> <identity...>: through the deployments/scale subresource.
scale_replicas() {
  local replicas=$1
  shift
  kubectl scale deployment "$DEPLOYMENT" -n "$NAMESPACE" --dry-run=server \
    --replicas="$replicas" "$@" 2>&1
}

restart() {
  kubectl patch deployment "$DEPLOYMENT" -n "$NAMESPACE" --dry-run=server --type merge \
    -p '{"spec":{"template":{"metadata":{"annotations":{"kube-handrail.io/restarted-at":"admission-test"}}}}}' \
    "$@" 2>&1
}

expect_allowed() {
  local name=$1 out
  shift
  if out=$("$@"); then
    if grep -q "$WARNING" <<<"$out"; then
      fail "$name" "admitted with a policy warning:" "$out"
    else
      pass "$name"
    fi
  else
    fail "$name" "refused:" "$out"
  fi
}

expect_warned() {
  local name=$1 out
  shift
  if out=$("$@") && grep -q "$WARNING" <<<"$out"; then
    pass "$name"
  else
    fail "$name" "expected admission with a warning naming $POLICY:" "$out"
  fi
}

expect_denied() {
  local name=$1 out
  shift
  if out=$("$@"); then
    fail "$name" "admitted:" "$out"
  elif grep -q "$DENIAL" <<<"$out"; then
    pass "$name"
  else
    fail "$name" "refused, but not by $POLICY:" "$out"
  fi
}

current=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
if ((current >= MAX_REPLICAS)); then
  echo "Deployment $NAMESPACE/$DEPLOYMENT must be below MAX_REPLICAS ($MAX_REPLICAS); it has $current." >&2
  exit 2
fi
# A changed count within the bounds: one more, or the minimum if that is higher.
in_bounds=$((current + 1 > MIN_REPLICAS ? current + 1 : MIN_REPLICAS))
over=$((MAX_REPLICAS + 1))
# Below the minimum, only when that is a change (an unchanged count is always admitted).
under=$((MIN_REPLICAS - 1))
actions=$(kubectl get validatingadmissionpolicybinding "$POLICY" -o jsonpath='{.spec.validationActions}')

restore() {
  if [[ "${KEEP_DENY:-0}" != 1 ]]; then
    kubectl patch validatingadmissionpolicybinding "$POLICY" --type merge \
      -p "{\"spec\":{\"validationActions\":$actions}}" >/dev/null
  fi
  if [[ -n "${OTHER_NAMESPACE:-}" ]]; then
    kubectl delete namespace "$OTHER_NAMESPACE" --ignore-not-found --wait=false >/dev/null
  fi
}
trap restore EXIT

warnings=$(kubectl get validatingadmissionpolicy "$POLICY" -o jsonpath='{.status.typeChecking.expressionWarnings}')
if [[ -z "$warnings" ]]; then
  pass "policy type-checks without warnings"
else
  fail "policy type-checks without warnings" "$warnings"
fi

if [[ -n "${OTHER_NAMESPACE:-}" ]]; then
  kubectl create namespace "$OTHER_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl create deployment "$DEPLOYMENT" -n "$OTHER_NAMESPACE" --replicas=0 \
    --image=registry.k8s.io/pause:3.10 --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi

# A fresh policy or binding takes a moment to be enforced: wait until the out-of-bounds
# request carries the warning (or the denial, for a binding already in Deny).
for _ in $(seq 1 30); do
  if patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$over" "${WRITER[@]}" | grep -q -e "$WARNING" -e "$DENIAL"; then
    break
  fi
  sleep 1
done

if [[ "$actions" == *Deny* ]]; then
  echo "SKIP  warn mode: the binding is already in Deny ($actions)"
else
  expect_warned "warn mode: writer over the maximum is admitted with a warning" \
    patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$over" "${WRITER[@]}"
fi

kubectl patch validatingadmissionpolicybinding "$POLICY" --type merge \
  -p '{"spec":{"validationActions":["Deny"]}}' >/dev/null
for _ in $(seq 1 30); do
  if patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$over" "${WRITER[@]}" | grep -q "$DENIAL"; then
    break
  fi
  sleep 1
done

expect_allowed "deny mode: writer scale within bounds ($current -> $in_bounds)" \
  patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$in_bounds" "${WRITER[@]}"
expect_denied "deny mode: writer scale over the maximum ($current -> $over)" \
  patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$over" "${WRITER[@]}"
if ((MIN_REPLICAS > 0 && under != current)); then
  expect_denied "deny mode: writer scale below the minimum ($current -> $under)" \
    patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$under" "${WRITER[@]}"
else
  echo "SKIP  below the minimum: MIN_REPLICAS is 0 or the count is already one below it"
fi
expect_denied "deny mode: writer over the maximum is refused even past RBAC" \
  patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$over" "${WRITER_ADMIN[@]}"
expect_denied "deny mode: writer over the maximum through deployments/scale, past RBAC" \
  scale_replicas "$over" "${WRITER_ADMIN[@]}"
expect_allowed "deny mode: writer restart (template change, count unchanged)" \
  restart "${WRITER[@]}"
expect_allowed "deny mode: another user over the maximum in the sandbox" \
  patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$over" "${OTHER_ADMIN[@]}"
if [[ -n "${OTHER_NAMESPACE:-}" ]]; then
  expect_allowed "deny mode: writer over the maximum in another namespace" \
    patch_replicas "$OTHER_NAMESPACE" "$DEPLOYMENT" "$over" "${WRITER_ADMIN[@]}"
else
  echo "SKIP  other namespace: OTHER_NAMESPACE is not set"
fi

if ((failures > 0)); then
  echo "$failures case(s) failed"
  exit 1
fi
echo "all cases passed"
