# Integration suite: kind and moto

The guard scenarios of `tests/e2e/e2e_gateway.py`, run on every pull request without AWS (the `integration` job in `.github/workflows/ci.yml`). Status: implemented; 48 cases run locally on kind in about 75 seconds on a laptop, about two minutes with the cluster; the CI result is not known until the branch is pushed.

## Run

```sh
make integration                  # throwaway kh-int-* kind cluster, deleted afterwards
tests/integration/run.sh -k verify  # extra arguments go to pytest
KH_INTEGRATION=1 KUBECONFIG=<kind kubeconfig> .venv/bin/python -m pytest tests/integration
```

`run.sh` needs Docker and kind, creates the cluster from the node image `.github/workflows/ci.yml` pins, with its own kubeconfig in a temporary directory, and never reads or writes the default kubeconfig. Without `KH_INTEGRATION` the tests are skipped, so `pytest` alone stays the unit suite. The harness refuses a `KUBECONFIG` that is unset, lists several files, or whose current context kind did not create (`kind-*`). AWS is moto only: every test runs in a fresh `mock_aws` with fake credentials, `AWS_PROFILE` unset and the shared config files pointed at `/dev/null`.

## What runs

- **Cluster.** `k8s/sandbox/` (namespace, RBAC, `web`), the admission policy with the bounds `0` and `5` through the `sed` of [Admission policy](../../docs/deploy.md#admission-policy), its binding switched to `Deny`, and a ResourceQuota that admits one BestEffort pod (the verification cases). Fixtures run `registry.k8s.io/pause:3.10`, preloaded on kind nodes, under the namespace's restricted Pod Security; the rollback fixture runs no pods.
- **Identities.** The read Lambda gets a client that impersonates `kube-handrail-reader` through its existing seam, `eks.ClientCache(factory=...)`. The write Lambda's `Runtime` (built by `get_runtime()` from the environment) gets a client that impersonates `kube-handrail-writer` and a clock the test can move. `test_kind_harness.py` checks that both get exactly the sandbox RBAC. Kubeconfig files with `as` and `as-groups` for both are written to pytest's temporary directory for `kubectl` debugging.
- **AWS.** The plan table with the key schema, both indexes, the TTL attribute and the `NEW_AND_OLD_IMAGES` stream of `terraform/main.tf`; the kill switch, both agents' switches and the freezes parameter; the approvals topic; the audit bucket with Object Lock and a default GOVERNANCE retention.
- **Calls.** A tool call is a `tools/call` body run through `mcp/interceptor` with a token-shaped `client_id` and the test `CALLER_KEY`, then the handler with `bedrockAgentCoreToolName` and `bedrockAgentCoreAwsRequestId` in its client context, as the gateway calls it. The approver gets `requestContext.authorizer.iam.userArn`, as its Function URL reports a SigV4-signed caller (example ARNs in account `123456789012`). The approver and the verifier invoke the write Lambda through a fake `lambda` client that runs it in process with their client context. The audit Lambda gets the records of moto's stream, shaped as the event source mapping delivers them.
- **Time.** Nothing sleeps for a window. Verification cases move the plan's `verify_at` one second into the past and run the verifier once; the expiry, cooldown and flapping cases move the write Lambda's clock; budgets are reached with real applies against small limits.

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
| Budgets: global, per agent, per object | `test_kind_switches.py`, real applies against small limits, no clock-hour wait | Agent budget counter on real DynamoDB (`--with-identity`) |
| Oscillation breaker: cooldown, single revert, flapping, manual change and override | `test_kind_breaker.py` | |
| Change freezes, including an unreadable freeze parameter | `test_kind_switches.py` | |
| `admission_denied`: the policy narrower than the Lambdas' bounds | `test_kind_rollout.py` (new: before, unit tests and `tests/admission/run.sh` only) | |
| Restart; rollback history, refusals, ReplicaSet edited, apply, manual change and override | `test_kind_rollout.py` | |
| Server approval: `approval_required`, request cooldown, show, approve applies, record names the approver, correlation IDs, reject; `both` mode; approval identity | `test_kind_approval.py` | The approval URL refusing a Cognito token and unsigned requests, and a real SigV4 signature |
| Verification: pending, healthy, unknown after a change by hand, degraded and reverted (the revert verified, then `flapping`), revert skipped with the kill switch off, degraded without revert | `test_kind_verify.py` | |
| Audit record: entries per stream record, export, redelivery, no overwrite, a different entry under an existing key | `test_kind_audit.py` | The bucket policy's 403 on an overwrite without `If-None-Match`, the real stream and event source mapping (`--with-audit`) |
| Authentication: missing, malformed, tampered token | not portable | live only |
| Drift sentinel | not ported: moto has no Bedrock AgentCore gateway or DevOps Agent APIs; unit tests cover it | live (scheduled) |

`e2e_gateway.py --live-only` runs the live column: authentication against Cognito, the gateway's schema check, in `client` mode one apply and concurrent applies of one plan through the gateway (the interceptor, the Lambdas' EKS access entries and their network path in the VPC, the conditional write on real DynamoDB), in `server` mode the approval URL cases, and with `--with-identity` and `--with-audit` the identity and audit cases. The DevOps Agent association is checked by `scripts/devops_agent_register.py` and the sentinel. Without `--live-only` every live case still runs.

## Where moto differs from AWS

- **Conditional writes.** moto runs in the test process; the concurrent-apply case and the counters passing here shows the guards' conditions are right, not that the writes are atomic, which only DynamoDB guarantees. The live run keeps concurrent applies and the agent budget counter (`--live-only`).
- **Indexes.** moto's global secondary indexes are consistent at once; DynamoDB's are eventually consistent. The history merge with the last-plan annotation that covers that gap is unit-tested.
- **TTL.** moto does not expire items, so no TTL `REMOVE` (`expunged` audit entry) is produced; the audit unit tests cover it.
- **Streams.** Records are read synchronously from moto and handed to the Lambda by hand; the event source mapping's batching, retries from a failed record and dead-letter queue are not exercised. moto's record shape is adjusted (creation time in epoch seconds, binary as base64 text, no `OldImage` on an `INSERT`).
- **S3.** moto honours Object Lock retention and `If-None-Match`, but not bucket policies: the policy's refusal of an overwrite without `If-None-Match` stays live.
- **IAM.** No role or policy is evaluated: that the read role cannot consume a plan or the Lambdas cannot write the bucket is not tested here, nor the Function URL's IAM auth.
- **SNS.** `request_approval` publishes to moto; nothing is delivered.
- **Not ported.** The drift sentinel (moto has no Bedrock AgentCore gateway or DevOps Agent APIs) and the impact preview's Service and PodDisruptionBudget case.
- **Clock hour.** The budget cases count applies in the current clock hour; a case that straddles the hour boundary sees a fresh counter.
