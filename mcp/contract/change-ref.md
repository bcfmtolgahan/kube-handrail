# Change reference

Appendix to [the contract](../CONTRACT.md#plan-record-dynamodb). Status: implemented, not yet tested on a cluster.

`change_ref` is an optional argument of every `plan_*` and `apply_*` tool: a ticket, incident or change reference for the humans who approve and audit the change. It changes nothing else. Below are the differences from an argument such as `verify_window_seconds`.

- **Arguments.** A string of 1 to 128 characters, not only whitespace, without characters of Unicode category `Cc`, `Cf` or `Cs` (no newline or tab either); anything else is `invalid_argument`, before any other read. It enters `args`, and so `args_hash`, only when given: apply must repeat it exactly (`plan_mismatch` otherwise), and an approval covers it.
- **Untrusted.** The agent chose it. The approval view (`change_ref`), the notification ("Change reference (from the agent): ...") and the audit `summary` show it cleaned and wrapped as [untrusted text](../CONTRACT.md#untrusted-output); the record's `args` and the audit images keep it as given. It is never parsed and never selects anything.
- **Not returned to the agent:** neither the plan response nor `get_plan_outcome` echoes it.
