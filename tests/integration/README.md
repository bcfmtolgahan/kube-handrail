# Integration suite: kind and moto

The guard scenarios of `tests/e2e/e2e_gateway.py`, run on every pull request without AWS (the `integration` job in `.github/workflows/ci.yml`). Status: implemented; 63 cases run locally on kind in about four minutes on a laptop, of which the five HPA adjust cases take about three (they wait for the HPA controller); the CI job took about 2.5 minutes before the Milestone 6 cases were added.

## Run

```sh
make integration                  # throwaway kh-int-* kind cluster, deleted afterwards
tests/integration/run.sh -k verify  # extra arguments go to pytest
KH_INTEGRATION=1 KUBECONFIG=<kind kubeconfig> .venv/bin/python -m pytest tests/integration
```

`run.sh` needs Docker, kind, kubectl and Terraform (the harness renders the namespace policy; no AWS credentials) and network access for metrics-server (`metrics-server.sh`: v0.7.2, pinned by version and checksum, with `--kubelet-insecure-tls`; the CI job runs the same script), refuses a `KIND_CLUSTER` name that already exists (it deletes only the cluster it created), creates the cluster from the node image `.github/workflows/ci.yml` pins, with its own kubeconfig in a temporary directory, and never reads or writes the default kubeconfig. Without `KH_INTEGRATION=1` the tests are skipped, so `pytest` alone stays the unit suite. The harness refuses a `KUBECONFIG` that is unset, lists several files, or whose current context kind did not create (`kind-*`, with an API server on localhost).

Run the suite sequentially, not with `pytest-xdist` or two runs against one cluster: the admission cases edit the cluster-wide policy or switch its binding to `Warn` while they run (and restore the rendered file), and the verification cases share one BestEffort pod slot in the quota. Separate clusters run in parallel safely. AWS is moto only: every test runs in a fresh `mock_aws` with fake credentials, `AWS_PROFILE` unset and the shared config files pointed at `/dev/null`.

## What runs

- **Namespace policy and cluster.** At the start of a session `tests/k8s_render/render.sh` renders [`namespaces.tfvars`](namespaces.tfvars) through `terraform/modules/namespace-policy`: `kube-handrail-sandbox` (bounds `0` to `5`, every action including `adjust_hpa`, the global approvers) and `kh-int-restart-only` (bounds `1` to `3`, restart only, its own approver). The harness checks the render against its constants, gives the Lambdas its `namespace_policy.json` (`NAMESPACE_POLICY_FILE`, `ALLOWED_NAMESPACES` of both namespaces, `ADMISSION_CHECK=true`), and applies its `rbac.yaml`, `policy-reader.yaml` and `admission-policy.yaml` (binding in `Deny`) after a server-side dry run, then the label command, as [deploy](../../docs/deploy.md#namespaces-rbac-and-admission-policy) applies `build/k8s`. Both namespaces come from `k8s/sandbox/namespace.yaml` (restricted Pod Security), `web` from `k8s/sandbox/web-deployment.yaml`; the superseded `k8s/sandbox/rbac.yaml` and `admission-policy.yaml` are not used. A ResourceQuota admits one BestEffort pod in the sandbox (the verification cases). Fixtures run `registry.k8s.io/pause:3.10`, preloaded on kind nodes, under the namespace's restricted Pod Security; the rollback fixture runs no pods.
- **Identities.** The read Lambda gets a client that impersonates `kube-handrail-reader` through its existing seam, `eks.ClientCache(factory=...)`. The write Lambda's `Runtime` (built by `get_runtime()` from the environment) gets clients (`apps`, `autoscaling`) that impersonate `kube-handrail-writer` and a clock the test can move. `test_kind_harness.py` checks that both get exactly the rendered RBAC: the reader may `get` only the admission policy and its binding cluster-wide, and the writer reads ReplicaSets only where rollback is allowed. Kubeconfig files with `as` and `as-groups` for both are written to pytest's temporary directory for `kubectl` debugging.
- **AWS.** The plan table with the key schema, both indexes, the TTL attribute and the `NEW_AND_OLD_IMAGES` stream of `terraform/main.tf`; the kill switch, both agents' switches and the freezes parameter; the approvals topic; the audit bucket with Object Lock and a default GOVERNANCE retention.
- **Calls.** A tool call is a `tools/call` body run through `mcp/interceptor` with a token-shaped `client_id` and the test `CALLER_KEY`, then the handler with `bedrockAgentCoreToolName` and `bedrockAgentCoreAwsRequestId` in its client context, as the gateway calls it. The approver gets `requestContext.authorizer.iam.userArn`, as its Function URL reports a SigV4-signed caller (example ARNs in account `123456789012`). The approver's and the verifier's `Runtime` are built by the harness with the production classes (so their `get_runtime()` environment wiring is not exercised; unit tests cover it), and they invoke the write Lambda through a fake `lambda` client that runs it in process with their client context. The audit Lambda gets the records of moto's stream, shaped as the event source mapping delivers them.
- **Time.** Nothing sleeps for a window. Verification cases move the plan's `verify_at` one second into the past and run the verifier once; the expiry, cooldown and flapping cases move the write Lambda's clock (the read Lambda has no clock seam and keeps the real time, so such a record can have an `applied_at` earlier than its `created_at`, which production never writes; the breaker orders by `applied_at`); budgets are reached with real applies against small limits.

## Coverage

| Scenario in `e2e_gateway.py` | Integration test | Still live |
|---|---|---|
| Namespace allowlist, replica bounds, names, missing Deployment | `test_kind_guards.py` | Wrong argument types rejected by the gateway's tool schema (the Lambda's own check is covered) |
| GitOps: Argo CD annotation, Flux label added after the plan, Helm instance label and `gitops_markers`; HPA | `test_kind_guards.py` | |
| Unknown, mismatched, single-use and expired plans; label-only change; correlation IDs | `test_kind_guards.py` (expiry by the write clock, no TTL wait) | |
| Conflicts: spec change, manual scale; concurrent applies | `test_kind_guards.py` | Concurrent applies: the conditional write on real DynamoDB |
| Blast radius | `test_kind_guards.py` | |
| Impact preview (Service, PodDisruptionBudget, `pdb_headroom`) | not ported: needs controller-filled EndpointSlices and PDB status; unit tests cover the preview | default live run |
| Untrusted event text | `test_kind_harness.py`, with the RBAC of both impersonated groups | |
| Kill switch, global (including unreadable and not exactly `true`) and per agent | `test_kind_switches.py` | |
| Agent identity, `not_requester`, client-sent caller argument; forged callers (unsigned, wrong key, unknown client) | `test_kind_switches.py`, `test_kind_approval.py` | Through the real gateway and interceptor (`--with-identity`); detaching the interceptor stays a manual case ([Agents](../../docs/deploy.md#agents)) |
| Budgets: global, per agent, per object | `test_kind_switches.py`, real applies against small limits, no wait for a new hour | Agent budget counter on real DynamoDB (`--with-identity`) |
| Oscillation breaker: cooldown, single revert, flapping, manual change and override | `test_kind_breaker.py` | |
| Change freezes, including an unreadable freeze parameter | `test_kind_switches.py` | |
| `admission_denied`: the policy narrowed in the cluster after the plan; the next plan `conflict` (`bounds_mismatch`) | `test_kind_rollout.py` | |
| Namespace policy (`--with-namespaces`): scale and rollback `forbidden_namespace` in a restart-only namespace (`details.action`, on plan and apply), a restart planned and applied there, the binding in `Warn` refusing plans (`binding_not_deny`) and `ADMISSION_CHECK=false` skipping the check, the namespace's own approver deciding and a global approver refused (`not_namespace_approver`), the plan's `cluster` in the record, `get_plan_outcome`, the approval view and the audit entries | `test_kind_namespaces.py`, `test_kind_audit.py` | Against EKS with the access entries (`--with-namespaces` without `--live-only`) |
| Restart; rollback history, refusals, ReplicaSet edited, apply, manual change and override | `test_kind_rollout.py` | |
| HPA adjust (`--with-hpa`): minimum 1 to 2 applied and verified healthy; a minimum the BestEffort quota cannot fit (`hpa_metrics_unavailable`, `replica_failure`) reverted with `revert_on_failure`, bounds restored and pods left as the controller keeps them; the writer refused by `kube-handrail-writer-hpa-bounds` for a `scaleTargetRef` or out-of-bounds change; the scale plan's `details.hpa` hint; refusals | `test_kind_hpa.py`, `test_kind_guards.py` | Against EKS with the access entries and the gateway (`--with-hpa`) |
| Server approval: `approval_required`, request cooldown, show, approve applies, record names the approver, correlation IDs, reject; `both` mode waiting on `get_plan_outcome`'s `approval`; approval identity | `test_kind_approval.py` | The approval URL refusing a Cognito token and unsigned requests, and a real SigV4 signature |
| `change_ref`: pinned between plan and apply (`plan_mismatch`), refused when too long or with control characters, shown wrapped to the approver and in the audit summary, carried by the approval's apply | `test_kind_change_ref.py` | Through the gateway (`--with-hpa`) |
| Verification: pending, healthy, unknown after a change by hand, degraded and reverted (the revert verified, then `flapping`), revert skipped with the kill switch off, degraded without revert | `test_kind_verify.py` | |
| Audit record: entries per stream record, export, redelivery, no overwrite, a different entry under an existing key | `test_kind_audit.py` | The bucket policy's 403 on an overwrite without `If-None-Match`, the real stream and event source mapping (`--with-audit`) |
| Authentication: missing, malformed, tampered token | not portable | live only |
| Drift sentinel | not ported: moto has no Bedrock AgentCore gateway or DevOps Agent APIs; unit tests cover it | live (scheduled) |

`e2e_gateway.py --live-only` runs the live column: authentication against Cognito, the gateway's schema check, in `client` mode one apply and concurrent applies of one plan through the gateway (the interceptor, the Lambdas' EKS access entries and their network path in the VPC, the conditional write on real DynamoDB), in `server` mode all approval cases (for the approval URL), and with `--with-identity` and `--with-audit` the identity and audit cases. The DevOps Agent association is checked by `scripts/devops_agent_register.py` and the sentinel. Without `--live-only` every live case still runs.

## Where moto differs from AWS

- **Conditional writes.** moto runs in the test process; the concurrent-apply case and the counters passing here shows the guards' conditions are right, not that the writes are atomic, which only DynamoDB guarantees. The live run keeps concurrent applies and the agent budget counter (`--live-only`).
- **Indexes.** moto's global secondary indexes are consistent at once; DynamoDB's are eventually consistent. The history merge with the last-plan annotation that covers that gap is unit-tested.
- **TTL.** moto does not expire items, so no TTL `REMOVE` (`expunged` audit entry) is produced; the audit unit tests cover it.
- **Streams.** Records are read synchronously from moto and handed to the Lambda by hand; the event source mapping's batching, retries from a failed record and dead-letter queue are not exercised. moto's record shape is adjusted (creation time in epoch seconds, binary as base64 text, no `OldImage` on an `INSERT`).
- **S3.** moto honours Object Lock retention and `If-None-Match`, but not bucket policies: the policy's refusal of an overwrite without `If-None-Match` stays live.
- **IAM.** No role or policy is evaluated: that the read role cannot consume a plan or the Lambdas cannot write the bucket is not tested here, nor the Function URL's IAM auth.
- **SNS.** `request_approval` publishes to moto; nothing is delivered.
- **Not ported.** The drift sentinel (moto has no Bedrock AgentCore gateway or DevOps Agent APIs) and the impact preview's Service and PodDisruptionBudget case.
- **Clock hour.** The budget cases count applies in the current clock hour; a case that starts in the last 90 seconds of an hour first sleeps past the boundary, so it does not see a fresh counter halfway.
