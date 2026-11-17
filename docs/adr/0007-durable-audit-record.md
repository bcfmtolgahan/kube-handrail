# ADR 0007: Durable audit record

- Status: Accepted
- Date: 2026-11-06

Implemented in [Milestone 2](../history/milestones.md#milestone-2-verified-changes-more-actions-accountable-agents) (item 6); see [Audit record](../../mcp/contract/audit.md#audit-record) for the exact rules.

## Context

The plan record was the only trail of a change: who asked for it, who approved it, whether and how it ran, and its verification and revert. Records expire through the table's TTL an hour or two after the plan, so nothing could be shown afterwards. And the write role, the executor, can update any attribute of a plan record: IAM does not keep it from rewriting `result` or `approved_by`, because its consume condition reads the approval attributes.

## Decision

- **The stream is the source.** The plan table's DynamoDB stream (`NEW_AND_OLD_IMAGES`) yields one record per change, in order per item, whoever made it. A separate audit Lambda, with its own role and outside the VPC, receives them through an event source mapping. The executor's code writes no audit record, so there is none of its own for it to rewrite; what it writes to the table becomes another entry.
- **One write-once object per stream record**, in an S3 bucket with Object Lock and a default retention. The key is derived from the record (plan id, approximate time, sequence number), and each put carries `If-None-Match: *`, which the bucket policy requires. A redelivery maps to the same key with the same entry; a different entry under an existing key (the derived summary aside) stops the shard and alarms.
- **The executor is denied the bucket.** The bucket policy denies writes, deletes, the governance bypass and bucket changes to every role Terraform creates for the tool Lambdas, the approver, the verifier, the sentinel, the interceptor and the gateway.
- **The raw images are the source of truth**, with a small derived summary for humans. `scripts/audit.py export` gives one plan's entries and a summary, and follows the link between a plan and its revert record.
- **Detect, do not block.** The apply path does not wait for the record. A failure alarms (dead-letter queue, iterator age, errors, failed records) but never refuses a change.
- **GOVERNANCE by default**, so a test deployment can be torn down by an administrator; COMPLIANCE is recommended for production.

## Consequences

- A change can be shown with its requesting agent, approver, channel, result, verification and revert for the retention period, long after the plan record has expired.
- A plan record rewritten by the executor keeps its earlier versions in the record, but nothing flags the rewrite: a human reading the export sees two `modified` entries.
- The executing role is attributed by construction (only the write role can consume a plan through the guard), not observed: stream records carry no principal except for TTL removals.
- The record shows what the table held. It does not prove that an approval came through the approval channel; anyone able to write the table can still write approval attributes, and the record then shows that write.
- Records the mapping gives up on are lost from the record unless replayed from the stream within 24 hours; the plan's export then lacks entries, and `has_creation` shows a missing creation.
- Calls refused before a record is written are only in the tool records in CloudWatch Logs.
- Under COMPLIANCE, neither the objects nor the bucket can be deleted before the retention ends.

## Alternatives considered

### A. A signed, hash-chained ledger with approval receipts and anomaly detection

Each entry signed with a KMS key and chained to the previous one, a write-once receipt per approval decision, and automatic classification of unexpected transitions that turns remediation off. It would detect a forged approval automatically and make the record verifiable offline, but it needs a sequence allocator, a signing key, receipts in the approval path and a classification table kept in step with every action. Deferred: the write-once bucket gives the durable record now, and the chain, receipts and anomaly detection can be added on top of the same stream later.

### B. Write the record from the tool Lambdas

Simple, but the executor would write its own record, and a compromised executor could skip or falsify it.

### C. CloudTrail data events on the plan table

Would show the principal of each table write, so the executor would be observed rather than attributed. It costs per event and is not plan-shaped. Kept as an open question in the roadmap.
