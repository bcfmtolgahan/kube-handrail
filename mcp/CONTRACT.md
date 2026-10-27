# Tool contract (Milestone 0)

Single source of truth for handlers, infrastructure code and tests. Change this file first.

## Targets and tools

| Gateway target | Lambda | Classification | Tools (schema file) |
|---|---|---|---|
| `k8sr` | `k8s_read` | `READ_ONLY` | `get_workload_status`, `plan_scale_deployment` (`mcp/k8s_read/tools.json`) |
| `k8sw` | `k8s_write` | `MUTATIVE` | `apply_scale_deployment` (`mcp/k8s_write/tools.json`) |

Clients see `<target>___<tool>`, for example `k8sw___apply_scale_deployment`. Full names must be at most 64 characters and match `^[a-z0-9_]+$`.

## Dispatch

The tool name is the part after `___` in `context.client_context.custom["bedrockAgentCoreToolName"]`. The event is the tool arguments object. There is no `tool_name` argument and no fallback. An unknown tool returns error code `unknown_tool`.

## Response envelope

Every handler returns a JSON object (not wrapped in `statusCode`/`body`):

```json
{ "ok": true, "data": {}, "error": null, "truncated": false }
{ "ok": false, "data": null, "error": { "code": "plan_expired", "message": "..." }, "truncated": false }
```

Error codes: `invalid_argument`, `unknown_tool`, `not_found`, `forbidden_namespace`, `out_of_bounds`, `gitops_managed`, `plan_not_found`, `plan_expired`, `plan_used`, `plan_mismatch`, `conflict`, `remediation_disabled`, `kubernetes_error`, `internal`.

## Plan record (DynamoDB)

| Attribute | Type | Notes |
|---|---|---|
| `plan_id` | S | partition key, random UUID |
| `action` | S | `scale_deployment` |
| `namespace`, `name` | S | target |
| `replicas_from`, `replicas_to` | N | |
| `args_hash` | S | SHA-256 of canonical JSON of `{action, namespace, name, replicas}` |
| `resource_version` | S | Deployment `metadata.resourceVersion` at plan time |
| `created_at` | N | epoch seconds |
| `expires_at` | N | epoch seconds, plan TTL (default 300 s); also the table TTL attribute |
| `used` | BOOL | apply sets it with a conditional update (`used = false AND expires_at > now`) |
| `applied_at`, `result` | N, S | written by apply |

## Guard checks

Plan (`k8s_read`):
1. Arguments valid: namespace and name match DNS-1123; `replicas` integer.
2. Namespace in `ALLOWED_NAMESPACES`.
3. `MIN_REPLICAS <= replicas <= MAX_REPLICAS`.
4. Deployment exists and is not GitOps-managed. Fail closed: treat it as managed if it has the Argo CD `argocd.argoproj.io/tracking-id` annotation or the `app.kubernetes.io/instance` label (Argo CD label tracking; may also match Helm releases, which is acceptable).
5. Deployment has no HorizontalPodAutoscaler targeting it.

Apply (`k8s_write`), all re-checked independently of the plan step:
1. Kill switch: SSM parameter `REMEDIATION_ENABLED_PARAM` must equal `true`.
2. Checks 1-3 above.
3. Plan exists, `action` matches, `args_hash` matches, not expired, not used. Mark used with a conditional update before patching.
4. Patch `spec.replicas` with a `metadata.resourceVersion` precondition equal to the planned one; a mismatch returns `conflict`.
5. Server-side dry run (`dryRun=All`) before the real patch.

## Runtime and packaging

- Python 3.12, `x86_64`, pinned in one Terraform local.
- Dependencies installed by Terraform-driven build into `build/<function>/` with `pip install --target build/<function> --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12`, plus `mcp/common/`. `archive_file` zips `build/<function>/`.

## Identity

- Two IAM roles, one per Lambda.
- Two EKS access entries, type `STANDARD`, with `kubernetes_groups` `kube-handrail-reader` and `kube-handrail-writer`. No access policy associations.
- RBAC in the sandbox namespace only:
  - reader: `get`, `list` on `deployments`, `horizontalpodautoscalers`;
  - writer: `get`, `patch` on `deployments`.

## Configuration (environment variables)

`CLUSTER_NAME`, `PLANS_TABLE`, `ALLOWED_NAMESPACES` (comma-separated), `MIN_REPLICAS`, `MAX_REPLICAS`, `PLAN_TTL_SECONDS`, `REMEDIATION_ENABLED_PARAM` (write only). `AWS_REGION` is provided by Lambda.
