#!/usr/bin/env bash
# Server-side dry-run cases for the admission policy rendered to build/k8s
# (terraform/modules/namespace-policy; mcp/CONTRACT.md, Admission policy). Every request is
# a dry run: nothing in the cluster changes, except the binding's validationActions, which
# the script switches to Deny and back, and the other namespace, which it creates and
# deletes only with OTHER_NAMESPACE set.
#
# Needs kubectl 1.30 or later and a kubeconfig whose user may impersonate users and groups
# (cluster-admin). The rendered manifests (build/k8s) and the Deployments must already exist.
#
# Environment:
#   NAMESPACE        first namespace of the policy (default kube-handrail-sandbox)
#   DEPLOYMENT       Deployment in it, at a count below the maximum (default web)
#   MIN_REPLICAS     the policy's minimum there (default 0)
#   MAX_REPLICAS     the policy's maximum there (default 5)
#   NAMESPACE_2      if set, a second namespace of the policy with other bounds; the bound
#                    cases run there too, and a count allowed in one is checked in the other
#   DEPLOYMENT_2     Deployment in it, below its maximum (default web)
#   MIN_REPLICAS_2   the policy's minimum there (default 0)
#   MAX_REPLICAS_2   the policy's maximum there (default 5)
#   OTHER_NAMESPACE  if set, create this namespace with a Deployment, check that the policy
#                    does not touch it, and delete it again. Leave it unset on a shared cluster.
#   KEEP_DENY        if 1, leave the binding in Deny at the end; otherwise restore the
#                    validationActions it had.
set -euo pipefail

NAMESPACE=${NAMESPACE:-kube-handrail-sandbox}
DEPLOYMENT=${DEPLOYMENT:-web}
MIN_REPLICAS=${MIN_REPLICAS:-0}
MAX_REPLICAS=${MAX_REPLICAS:-5}
DEPLOYMENT_2=${DEPLOYMENT_2:-web}
MIN_REPLICAS_2=${MIN_REPLICAS_2:-0}
MAX_REPLICAS_2=${MAX_REPLICAS_2:-5}
POLICY=kube-handrail-writer-bounds
READER=(--as kube-handrail-admission-reader --as-group kube-handrail-reader)
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

# scale_replicas <namespace> <deployment> <replicas> <identity...>: through the
# deployments/scale subresource.
scale_replicas() {
  local ns=$1 name=$2 replicas=$3
  shift 3
  kubectl scale deployment "$name" -n "$ns" --dry-run=server --replicas="$replicas" "$@" 2>&1
}

# restart <namespace> <deployment> <identity...>
restart() {
  local ns=$1 name=$2
  shift 2
  kubectl patch deployment "$name" -n "$ns" --dry-run=server --type merge \
    -p '{"spec":{"template":{"metadata":{"annotations":{"kube-handrail.io/restarted-at":"admission-test"}}}}}' \
    "$@" 2>&1
}

# can_i <verb> <resource> <identity...>: prints yes or no.
can_i() {
  local verb=$1 resource=$2
  shift 2
  kubectl auth can-i "$verb" "$resource" "$@" 2>/dev/null || true
}

expect_answer() {
  local name=$1 expected=$2 answer
  shift 2
  answer=$(can_i "$@")
  if [[ "$answer" == "$expected" ]]; then
    pass "$name"
  else
    fail "$name" "expected $expected, got ${answer:-nothing}"
  fi
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

# replicas_of <namespace> <deployment> <maximum>: the current count, which must be below
# the maximum so that one more is a change within the bounds.
replicas_of() {
  local count
  count=$(kubectl get deployment "$2" -n "$1" -o jsonpath='{.spec.replicas}')
  if ((count >= $3)); then
    echo "Deployment $1/$2 must be below its maximum ($3); it has $count." >&2
    exit 2
  fi
  echo "$count"
}

current=$(replicas_of "$NAMESPACE" "$DEPLOYMENT" "$MAX_REPLICAS")
over=$((MAX_REPLICAS + 1))
if [[ -n "${NAMESPACE_2:-}" ]]; then
  current_2=$(replicas_of "$NAMESPACE_2" "$DEPLOYMENT_2" "$MAX_REPLICAS_2")
fi
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

# bound_cases <namespace> <deployment> <current> <min> <max>: the Deny cases of one
# namespace's bounds.
bound_cases() {
  local ns=$1 name=$2 count=$3 min=$4 max=$5
  # A changed count within the bounds: one more, or the minimum if that is higher.
  local in_bounds=$((count + 1 > min ? count + 1 : min))
  local above=$((max + 1))
  # Below the minimum, only when that is a change (an unchanged count is always admitted).
  local below=$((min - 1))
  expect_allowed "deny mode: $ns: writer scale within bounds ($count -> $in_bounds)" \
    patch_replicas "$ns" "$name" "$in_bounds" "${WRITER_ADMIN[@]}"
  expect_denied "deny mode: $ns: writer scale over the maximum ($count -> $above)" \
    patch_replicas "$ns" "$name" "$above" "${WRITER_ADMIN[@]}"
  if ((min > 0 && below != count)); then
    expect_denied "deny mode: $ns: writer scale below the minimum ($count -> $below)" \
      patch_replicas "$ns" "$name" "$below" "${WRITER_ADMIN[@]}"
  else
    echo "SKIP  $ns: below the minimum: it is 0 or the count is already one below it"
  fi
  expect_denied "deny mode: $ns: writer over the maximum through deployments/scale, past RBAC" \
    scale_replicas "$ns" "$name" "$above" "${WRITER_ADMIN[@]}"
  expect_allowed "deny mode: $ns: writer restart (template change, count unchanged)" \
    restart "$ns" "$name" "${WRITER_ADMIN[@]}"
  expect_allowed "deny mode: $ns: another user over the maximum" \
    patch_replicas "$ns" "$name" "$above" "${OTHER_ADMIN[@]}"
}

# The writer's own RBAC (no system:masters) in the first namespace.
expect_allowed "deny mode: writer scale within bounds through its own RBAC" \
  patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$((current + 1 > MIN_REPLICAS ? current + 1 : MIN_REPLICAS))" "${WRITER[@]}"
expect_denied "deny mode: writer scale over the maximum through its own RBAC" \
  patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$over" "${WRITER[@]}"
bound_cases "$NAMESPACE" "$DEPLOYMENT" "$current" "$MIN_REPLICAS" "$MAX_REPLICAS"
if [[ -n "${NAMESPACE_2:-}" ]]; then
  bound_cases "$NAMESPACE_2" "$DEPLOYMENT_2" "$current_2" "$MIN_REPLICAS_2" "$MAX_REPLICAS_2"
  # Each namespace is held to its own bounds, not the other's.
  if ((MAX_REPLICAS_2 > MAX_REPLICAS && MAX_REPLICAS + 1 != current_2)); then
    expect_allowed "deny mode: $NAMESPACE_2: a count over $NAMESPACE's maximum but within its own ($current_2 -> $over)" \
      patch_replicas "$NAMESPACE_2" "$DEPLOYMENT_2" "$over" "${WRITER_ADMIN[@]}"
  elif ((MAX_REPLICAS > MAX_REPLICAS_2 && MAX_REPLICAS_2 + 1 != current)); then
    expect_allowed "deny mode: $NAMESPACE: a count over $NAMESPACE_2's maximum but within its own ($current -> $((MAX_REPLICAS_2 + 1)))" \
      patch_replicas "$NAMESPACE" "$DEPLOYMENT" "$((MAX_REPLICAS_2 + 1))" "${WRITER_ADMIN[@]}"
  else
    echo "SKIP  cross-namespace bounds: the maximums are equal"
  fi
else
  echo "SKIP  second namespace: NAMESPACE_2 is not set"
fi

# The read Lambda's ClusterRole: get on the two objects, nothing else.
expect_answer "policy reader: get the policy" yes \
  get "validatingadmissionpolicies.admissionregistration.k8s.io/$POLICY" "${READER[@]}"
expect_answer "policy reader: get the binding" yes \
  get "validatingadmissionpolicybindings.admissionregistration.k8s.io/$POLICY" "${READER[@]}"
expect_answer "policy reader: no other policy" no \
  get "validatingadmissionpolicies.admissionregistration.k8s.io/another-policy" "${READER[@]}"
expect_answer "policy reader: no list" no \
  list "validatingadmissionpolicies.admissionregistration.k8s.io" "${READER[@]}"
expect_answer "policy reader: no patch" no \
  patch "validatingadmissionpolicybindings.admissionregistration.k8s.io/$POLICY" "${READER[@]}"

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
