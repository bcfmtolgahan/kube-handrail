# Audit record

Appendix to [the contract](../CONTRACT.md).

With `audit_enabled` (Terraform, default `true`), every change to a plan record is written once, as one JSON object, to an S3 bucket with Object Lock, where the tool Lambdas cannot change or delete it. Implemented and tested on a test cluster.

The record detects after the fact and blocks nothing: the apply path does not depend on it, so an audit failure alarms instead of refusing a change. It is a record of plan-record state: a call refused before a record is written or changed is only in the [tool record](observability.md#tool-record) (CloudWatch Logs). A signed hash chain, approval receipts and anomaly detection are not part of it ([ADR 0007](../../docs/adr/0007-durable-audit-record.md)).

## Source

The plan table has a DynamoDB stream with view type `NEW_AND_OLD_IMAGES`. Any write that changes an item yields exactly one stream record, in order per item; a write that changes nothing yields none. A Lambda event source mapping delivers the records to the audit Lambda (`mcp/audit/`), which runs outside any VPC with its own role:

- `starting_position` `TRIM_HORIZON`, `batch_size` 10, `parallelization_factor` 1 (one batch per shard at a time, so each plan's records are handled in order);
- `function_response_types` `["ReportBatchItemFailures"]` and `bisect_batch_on_function_error`;
- `maximum_retry_attempts` 10 and `maximum_record_age_in_seconds` 21600, then the batch's metadata (shard, sequence number range, not the records) goes to the SQS queue `<name_prefix>-audit-failures` (`destination_config.on_failure`, SSE, 14-day retention). The records stay in the stream for 24 hours after they were written.

The Lambda handles a batch's records in order. On the first record it cannot write it returns `{"batchItemFailures": [{"itemIdentifier": "<its sequence number>"}]}` and does not touch the rest, so the mapping retries from that record and no later record of the shard is written before it. Records whose key is not a plan id (the counters `budget#...` and `approval#global#...` that share the table) are skipped. The Lambda's package bundles a pinned `boto3` (`mcp/audit/requirements.txt`, the same pin as the sentinel's), because the runtime's copy may predate S3 conditional writes.

## Objects

One object per stream record of a plan, at

```
plans/<plan_id>/<approximate creation time, epoch seconds, 10 digits>-<sequence number, 40 digits>.json
```

Both numbers are zero-padded, so one `ListObjectsV2` on the prefix returns a plan's entries in event order. The key and the bytes come from the stream record alone (no processing time), so a redelivered record maps to the same key and the same bytes.

The object is `json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True)` of:

| Field | Notes |
|---|---|
| `v` | `1` |
| `plan_id` | the item's key |
| `event` | `created` (INSERT), `modified` (MODIFY), `expunged` (REMOVE by the table's TTL: `userIdentity` `{"type": "Service", "principalId": "dynamodb.amazonaws.com"}`) or `deleted` (any other REMOVE) |
| `stream` | `event_name`, `event_id`, `sequence_number`, `approximate_creation_time`, `ttl_removal` (bool), `user_identity` (as in the record, or `null`) |
| `image_format` | `plain`, or `dynamodb` when an image could not be decoded |
| `old_image`, `new_image` | the item before and after, as plain JSON (`null` when absent): integral numbers as integers, other numbers as their exact decimal string, sets as sorted lists, binary as base64. The source of truth. With `image_format` `dynamodb`, the images are kept exactly as the stream delivered them (DynamoDB JSON) and `summary` is only `{plan_id, error}`, the exception class: a value written into the plan record never keeps its change out of the record |
| `summary` | derived for humans: `plan_id`; `change_ref` as untrusted text; `cluster`, `kind`, `action`, `target_kind`, `hpa_target`, `namespace`, `name`, `requested_by`, `agent`, `requested_client_id`, `approved_by`, `approval_channel`, `approval_mode`, `rejected_by`, `channel`, `applied_at`, `reverts_plan_id` when present; `status`, `used` and `result` as a value, or as `{"from": ..., "to": ...}` when the record changed it; `verification` (its `outcome`) and `revert` (`status`, `revert_plan_id`) when present; `changed`, the sorted names of the attributes the record changed |

Each object is written with `PutObject` and `If-None-Match: *` (and `Content-MD5`; Object Lock requires an integrity header). A `412 PreconditionFailed` means the key exists: the Lambda reads the object, and an entry that is identical except for `summary` counts as written (a redelivery; a newer handler version may derive the summary differently). Any other difference is a failure of that record (`Mismatches`), which stops the shard, raises the failures alarm and, once the retries are spent, the dead-letter alarm. The put never sets a retention: the bucket's default applies.

## Bucket

`<name_prefix>-audit-<account id>`: Object Lock enabled at creation (with versioning, which cannot be suspended), default retention `audit_object_lock_mode` (`GOVERNANCE` by default, or `COMPLIANCE`) for `audit_retention_days` (30), SSE-S3, all public access blocked, `BucketOwnerEnforced`. The bucket policy denies:

- any request without TLS (`aws:SecureTransport` false);
- `s3:PutObject` without the `s3:if-none-match` condition key, so every put is conditional;
- to the read, write, approver, verifier, sentinel, interceptor and gateway roles (by `aws:PrincipalArn`): `s3:PutObject*`, `s3:DeleteObject*`, `s3:BypassGovernanceRetention`, `s3:RestoreObject`, `s3:ReplicateObject`, `s3:ReplicateDelete`, `s3:PutBucket*`, `s3:DeleteBucket*`, `s3:PutLifecycleConfiguration`, `s3:PutReplicationConfiguration` and `s3:PutEncryptionConfiguration`. None of these roles has any S3 grant either.

Under `GOVERNANCE`, a principal allowed `s3:BypassGovernanceRetention` can still delete an entry or shorten its retention; under `COMPLIANCE` nobody can before it ends, and the bucket cannot be deleted until then ([deploy](../../docs/deploy.md#audit-record-retention)). Retention is per object version, so entries outlive the plan table's records, which expire after an hour or two.

## What it shows, and how strongly

- **Requesting agent:** `requested_by` and `agent` in the images, as strong as [Agent identity](identity.md#agent-identity) makes them.
- **Approver:** `approved_by` (the IAM identity of the approval request), `approval_channel` and `decision_correlation_id`. The record shows what the table held; it does not prove that the approval channel wrote it.
- **Executing role:** not observed: stream records carry no principal (except TTL removals). A consume (`used` from `false` to `true`) and the result are attributed to the write Lambda's role by construction (only it can consume through the guard and only it is in `kube-handrail-writer`); `channel` and `apply_correlation_id` join the entry to its tool record and the Deployment's `kube-handrail.io/correlation-id` annotation.
- **Tampering by the executor:** the write role can still change the plan table (see [Identity](identity.md#iam-roles-and-rbac)), but every change becomes another entry, and it cannot replace or delete an earlier one. A rewritten `result` or a flipped `used` therefore shows as a later `modified` entry, which `audit.py verify` reports; nothing flags it as it happens.

## Export

`scripts/audit.py`, with the operator's credentials (`s3:ListBucket` and `s3:GetObject`; `--bucket`, else `AUDIT_BUCKET`, else `terraform output -raw audit_bucket_name` in `TF_DIR`). Its docstring and `--help` give the fields and finding codes.

- `export <plan_id> [--out FILE] [--no-links]` prints one JSON bundle: every entry in order, `malformed` keys (not read further), a `summary` (the latest image, every approver, `status_history`, verification and revert, `has_creation`) and `linked` (the revert record or reverted plan). For a consumed plan, `executed_by` holds the attribution above and CloudWatch Logs Insights templates: the Lambda logs by plan and correlation IDs, and the EKS audit log by the User-Agent. Exit status 1 without entries.
- `verify (<plan_id> | --since SECONDS | --all) [--json] [--approval-mode MODE]` exits 1 when a plan's entries show what no writer of the record produces: rewrites before, at or after the consume, an impossible consume, gaps, or a channel or missing server approval wrong for the plan's `approval_mode`. A `client`-mode apply is information, with a time window for CloudTrail. Tested on a test cluster.
- `list [--limit N]` lists plans by their latest entry, newest first, reading every key in the bucket.

## Alarms and metrics

Each invocation prints one JSON line, also an embedded metric record in `<name_prefix>/audit` (no dimensions): `Records`, `Written`, `Duplicates` (redeliveries), `Skipped` (counters), `Failures` and `Mismatches`, plus `failed_sequence_numbers` and `correlation_id`. Plan contents are not logged. With `sentinel_enabled` and `audit_enabled`, Terraform creates these alarms on the alert topic:

- `<name_prefix>-audit-dead-letters`: `ApproximateNumberOfMessagesVisible` of the failures queue, maximum over 5 minutes greater than 0;
- `<name_prefix>-audit-iterator-age`: `AWS/Lambda` `IteratorAge` of the audit Lambda, maximum over 5 minutes above 300 000 ms;
- `<name_prefix>-audit-errors`: `AWS/Lambda` `Errors` of the audit Lambda, sum over 5 minutes at least 1;
- `<name_prefix>-audit-failures`: `Failures`, sum over 5 minutes greater than 0. A record the Lambda reports back as failed is not a Lambda error, so `Errors` alone would not see it.

Audit role: `dynamodb:DescribeStream`, `GetRecords`, `GetShardIterator` and `ListStreams` on the plan table's stream; `s3:PutObject` and `s3:GetObject` (for the redelivery comparison) on the bucket's objects; `sqs:SendMessage` on the failures queue (the mapping sends with the function's role); its own log group. No table item, EKS, SSM or Lambda access. The approver refuses it as an approver, like the other Lambda roles. Environment: `AUDIT_BUCKET`, `METRIC_NAMESPACE` (`<name_prefix>/audit`). Outputs: `audit_bucket_name`, `audit_dlq_url` (`null` without `audit_enabled`).
