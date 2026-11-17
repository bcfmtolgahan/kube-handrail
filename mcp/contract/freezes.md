# Change freezes

Appendix to [the contract](../CONTRACT.md).

Status: implemented and tested on a test cluster.

A change freeze is a window in which no change is planned or applied in the namespaces it names, for example over a release or a holiday. Operators set freezes; nothing in the tool surface can. There is no break-glass path: to make a change during a freeze, an operator removes or shortens it ([ADR 0005](../../docs/adr/0005-change-freezes.md)).

## Configuration

One SSM `String` parameter, `FREEZES_PARAM` (Terraform: `/<name_prefix>/freezes`, created as `[]`, later applies ignore its value; output `freezes_parameter`), holds a JSON list of at most 50 entries:

```json
[
  { "namespaces": ["kube-handrail-sandbox"], "start": "2026-12-23T00:00:00Z",
    "until": "2027-01-02T00:00:00Z", "reason": "year-end freeze" },
  { "namespaces": ["*"], "start": "2026-12-01T18:00:00Z", "until": "2026-12-01T20:00:00Z",
    "reason": "database migration", "actions": ["scale", "restart"] }
]
```

- `namespaces`: a non-empty list of distinct DNS-1123 labels, or `"*"` for every namespace. They are not checked against `ALLOWED_NAMESPACES`.
- `start`, `until`: RFC 3339 in UTC to the second (`YYYY-MM-DDTHH:MM:SSZ`, no other offset, no fraction); `until` later than `start`. The window includes `start` and excludes `until`. An entry whose window has passed is valid and simply inactive.
- `reason`: printable text, 1 to 200 characters, returned to agents as is.
- `actions` (optional): a non-empty list of distinct `scale`, `restart`, `rollback`; without it the freeze covers every action.
- No other fields.

`scripts/freeze.py` (`list`, `add`, `remove INDEX...`, `remove --expired`, `remove --all`) reads and writes the parameter and validates every write with the Lambdas' own parser (`handrail_common.freezes`). Without `FREEZES_PARAM` no freeze is checked; Terraform always sets it.

## Reading

The parameter is read on every check, without caching, like the kill switch, so a freeze takes effect on the next call. It fails closed the way the kill switch does: a parameter that cannot be read (missing, denied, throttled) or that does not validate refuses the write with `remediation_disabled`, `details` `{"scope": "freezes", "reason": "unreadable" | "invalid"}`, in every namespace. `scripts/freeze.py remove --all` resets an invalid value to `[]`.

## Where it is checked

- **Plan** (every `plan_*` tool, `k8s_read`): after argument checks 1-3 and the caller's agent and its kill switch, before the Deployment is read. Nothing is read from the cluster and no plan is stored.
- **Apply** (every `apply_*` tool, `k8s_write`): between [apply](../CONTRACT.md#guard-checks) step 2 and step 3, with the time of the apply, before the plan is consumed. A plan approved before a freeze is therefore not applied during it. The refusal leaves the plan unconsumed, with no `result`: it can be applied once the freeze ends, if it has not expired by then (a server approval keeps a plan valid for `APPLY_AFTER_APPROVAL_TTL_SECONDS`), and otherwise it expires. The namespace checked is the request's, which the consume condition (`args_hash`) ties to the plan's.
- **Approval channel:** the approver runs the same apply, so an approval during a freeze is recorded and its apply refused (`data.apply.error.code = "change_frozen"`, HTTP 200). The approval API itself does not read freezes.
- **Not checked:** `request_approval` (its apply is), the read tools, the verification, and a pre-approved revert ([Revert](verification.md#revert)): a revert never enters the apply path, so a freeze does not stop it (ADR 0004 and 0005).

A freeze covers a request when the request's namespace is in `namespaces` (or `namespaces` has `"*"`), its action is in `actions` (or there is none), and `start <= now < until`. The refusal is `change_frozen`:

```json
{ "code": "change_frozen", "message": "changes in namespace sandbox are frozen until 2027-01-02T00:00:00Z",
  "details": { "namespace": "sandbox", "reason": "year-end freeze", "until": "2027-01-02T00:00:00Z" } }
```

`details.until` is the first moment a change is allowed again: windows covering the same namespace and action that overlap or touch the current one extend it. `details.reason` is the reason of the current window that ends last.
