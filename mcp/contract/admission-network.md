# Admission policy and network

Appendix to [the contract](../CONTRACT.md).

## Admission policy


Terraform renders `build/k8s/admission-policy.yaml` (`terraform/modules/namespace-policy`, from `namespaces` and `admission_mode`): a ValidatingAdmissionPolicy and a ValidatingAdmissionPolicyBinding, both named `kube-handrail-writer-bounds` (`admissionregistration.k8s.io/v1`, Kubernetes 1.30 or later). It is a second check of the replica bounds in the cluster, for the case that the guard in the write Lambda is wrong or bypassed; the guard stays authoritative ([ADR 0006](../../docs/adr/0006-admission-policy.md)).

- **Who and where.** The policy's `matchConstraints` and the binding's `matchResources` both select the namespaces of the policy with `kubernetes.io/metadata.name In [...]`, a label the API server sets and nobody can change, so relabelling cannot take a namespace out. A `matchCondition` limits it to requests whose user is in the group `kube-handrail-writer`, the write Lambda's access entry ([Identity](identity.md#iam-roles-and-rbac)). Other users (the read Lambda, humans) and namespaces are not evaluated.
- **Bounds.** Two CEL variables, `minReplicas` and `maxReplicas`, each a map of namespace to bound, JSON objects (CEL map literals) keyed by `request.namespace`; a selected namespace missing from them is refused. No `paramRef` ConfigMap, which a namespace admin could edit.
- **Failure.** `failurePolicy: Fail`: a validation that cannot be evaluated refuses the writer's request in a selected namespace. A `matchCondition` that cannot be evaluated also falls to `failurePolicy`, so in `Deny` it would refuse Deployment updates by every user in those namespaces (the rendered one cannot error).
- **Modes.** Terraform `admission_mode` sets the binding's `validationActions`: `Deny` (the default) or `Warn` (`[Warn, Audit]`: admitted, with a warning and an audit annotation). Because the mode is rendered, applying `build/k8s` again keeps it; a `kubectl patch` is undone by the next apply. The write Lambda does not log the warnings.

A denial is HTTP 422 (the validation's `reason` is left at its default, `Invalid`) whose Status `message`, or one of its `details.causes[].message`, contains `ValidatingAdmissionPolicy 'kube-handrail-writer-bounds' with binding 'kube-handrail-writer-bounds' denied request`. The write Lambda maps exactly that to `admission_denied`, with `details` `{"policy": "kube-handrail-writer-bounds", "binding": "kube-handrail-writer-bounds"}`; the cluster's message is never returned. Any other 422, including field validation and other policies, and the same text with another status, stays `kubernetes_error`.

- **Apply.** The server-side dry run runs admission, so the dry run is refused and nothing is patched. It comes after the consume ([Guard checks](../CONTRACT.md#guard-checks), apply step 3): the plan is used, its `result` is `admission_denied`, and the budget units taken in step 7 are not returned. The agent must not retry; a human looks at the bounds.
- **Revert.** A revert refused by the policy is `revert = {status: refused, reason: admission_denied}` and its revert record's `result` is `admission_denied`, final like any other refusal ([Revert](verification.md#revert)).
- **Tool record.** `admission_denied` counts as `ToolErrors`, not `GuardRejections`, so it raises the `<name_prefix>-tool-errors` alarm: the cluster refused a change the guard allowed, which means the bounds in the policy differ from the Terraform values, or the guard is wrong or was bypassed. A human must look. The record's `outcome` is `admission_denied`.

### Plan-time check

Every plan (scale, restart and rollback; [guard check 3a](../CONTRACT.md#guard-checks); `plan_adjust_hpa` reads the HPA policy the same way, [Adjust HPA](adjust-hpa.md#adjust-hpa)) reads the policy and the binding through the ClusterRole `kube-handrail-policy-reader` and is refused with `conflict` unless all of these hold:

1. the policy exists, and its namespace selector selects the plan's namespace;
2. the binding exists, refers to the policy, selects the namespace, and its `validationActions` include `Deny`;
3. the policy's `minReplicas` and `maxReplicas` hold exactly the namespace's bounds (`bounds_for`);
4. its enforcing parts are as rendered: `failurePolicy: Fail`, the matchCondition, variables and validation expression, resourceRules covering `UPDATE` of `deployments` and `deployments/scale`, no `paramKind`, `objectSelector`, `excludeResourceRules` or non-`Equivalent` `matchPolicy` on policy or binding.

It catches drift and these switch-offs, not every administrator edit.

A selector selects the namespace only if that follows from its name label alone (unset or empty selects all; a requirement on another label counts as not selecting). `details` are `{"check": "admission_policy_match", "reason": "<reason>", "policy": "kube-handrail-writer-bounds", ...}` with reason `policy_missing`, `binding_missing`, `binding_other_policy`, `binding_not_deny` (plus `validation_actions`), `policy_not_selecting_namespace`, `binding_not_selecting_namespace`, `bounds_mismatch` (plus `expected` and `found`, each `{min_replicas, max_replicas}`), `policy_modified` or `binding_modified` (item 4), or `unreadable` (plus `status`, the HTTP status when there is one). The plan is refused before the Deployment is read, and nothing is stored. A passing check is the `preflight` entry `admission_policy_match`.

- **Fail closed.** A policy or binding that cannot be read (no RBAC, an API error, a timeout) is `conflict` with reason `unreadable`, never a pass. The cluster's error text is never returned.
- **Turning it off.** Only Terraform `admission_check = false` (environment `ADMISSION_CHECK=false` on the read Lambda) skips it, for a cluster that does not run the policy (Kubernetes below 1.30). The `preflight` entry is then `{"check": "admission_policy_match", "passed": true, "skipped": true}`. Unset means on.
- **Warn.** With the binding in `Warn` every plan is refused with `binding_not_deny`; Terraform warns (check `admission_mode_and_check`) when `admission_mode = "Warn"` and `admission_check` is on.
- **Apply.** Apply does not read the policy again; the policy itself refuses an out-of-bounds patch.

`tests/admission/run.sh` runs server-side dry runs against the rendered manifests, impersonating the writer group, in `NAMESPACE` and `NAMESPACE_2` (other bounds): within the bounds is admitted; over the maximum is warned in `Warn` and refused in `Deny`, also past RBAC and through `deployments/scale`; below a minimum above 0 is refused; a restart and another user are admitted; a count within one namespace's bounds but not the other's is admitted only there. The writer's RBAC per namespace follows `ACTIONS` and `ACTIONS_2`. The reader group may `get` the two objects and nothing else; with `OTHER_NAMESPACE`, another namespace is untouched. It switches the binding to `Deny` and back. The CI job `admission` runs it on kind for the two-namespace fixture.

## Network

Only the two tool Lambdas talk to the cluster. At runtime they make these calls, all HTTPS on port 443:

| Call | Lambda | Endpoint |
|---|---|---|
| `eks:DescribeCluster` (API server URL and CA, whenever a Kubernetes client is built: on a cold start and every 10 minutes after) | both | `eks.<region>.amazonaws.com` |
| Kubernetes API | both | the cluster endpoint |
| `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `Query` | both (`PutItem` read only) | `dynamodb.<region>.amazonaws.com` |
| `ssm:GetParameter` (the global kill switch on apply; an agent's kill switch on plan, `request_approval` and apply) | write; both | `ssm.<region>.amazonaws.com` |
| `ssm:GetParameter` (the change freezes, on plan and apply) | both | `ssm.<region>.amazonaws.com` |
| `sns:Publish` (`request_approval`) | read | `sns.<region>.amazonaws.com` |

The EKS token is a presigned `sts:GetCallerIdentity` URL built locally; the Lambdas never call STS, the API server does. Lambda delivers function logs itself, outside the function's network. So neither STS nor CloudWatch Logs has to be reachable from the Lambdas' subnets. The approver, sentinel, interceptor and verifier Lambdas never run in a VPC; the verifier reaches the write Lambda through the Lambda API, and the verification's own calls are the write Lambda's.

`lambda_subnet_ids` (default empty) puts both tool Lambdas in those subnets, with `lambda_security_group_ids` or, when that is empty, a created security group that allows outbound TCP 443 only. Terraform refuses a cluster without a public endpoint unless `lambda_subnet_ids` is set. `allow_lambda_to_cluster_endpoint` (default `false`) adds inbound TCP 443 from those security groups to the cluster security group. `create_vpc_endpoints` (default `false`) creates interface endpoints with private DNS for `eks`, `ssm` and `sns` in one Lambda subnet per Availability Zone, with a security group that allows TCP 443 from the VPC's address ranges (private DNS sends every client in the VPC to them), and a DynamoDB gateway endpoint on the route tables of the Lambda subnets (the main route table for a subnet without an explicit association). Both options need private endpoint access on the cluster; Terraform refuses them without it. Deployment steps are in [Deploying to a new account](../../docs/deploy.md).
