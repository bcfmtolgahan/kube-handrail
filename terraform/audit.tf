# --- Audit record (mcp/CONTRACT.md, Audit record) -----------------------------
#
# Every change to a plan record reaches the audit Lambda through the plan table's stream, and
# the Lambda writes it once, as one JSON object, to a bucket with S3 Object Lock. The tool
# Lambdas, the approver and the verifier are denied any write to the bucket, so the executor
# cannot alter the record of what it did. The record detects after the fact; it blocks
# nothing, and the apply path does not depend on it.

locals {
  audit_count = var.audit_enabled ? 1 : 0
  audit_name  = "${var.name_prefix}-audit"
  # Globally unique, like the Cognito domain.
  audit_bucket_name = "${var.name_prefix}-audit-${local.account_id}"
  # Built from the name, which aws_iam_role.audit uses: the approver's DENIED_PRINCIPALS
  # must stay known at plan time.
  audit_role_arn = "arn:${local.partition}:iam::${local.account_id}:role/${local.audit_name}"

  audit_metric_namespace = "${var.name_prefix}/audit"
  # The alarms notify the alert topic, which the drift sentinel creates, so they need both.
  audit_alarm_count = var.audit_enabled && var.sentinel_enabled ? 1 : 0

  # Roles that may never write, delete or unlock audit objects or change the bucket.
  audit_denied_role_arns = concat(
    [for r in aws_iam_role.lambda : r.arn],
    [aws_iam_role.approver.arn],
    aws_iam_role.verifier[*].arn,
    aws_iam_role.sentinel[*].arn,
    aws_iam_role.interceptor[*].arn,
  )
}

# --- Bucket ---------------------------------------------------------------------

resource "aws_s3_bucket" "audit" {
  count = local.audit_count

  bucket              = local.audit_bucket_name
  object_lock_enabled = true
  # Even with true, a GOVERNANCE object can only be deleted by a caller allowed
  # s3:BypassGovernanceRetention, and a COMPLIANCE object not before its retention ends.
  force_destroy = var.audit_force_destroy
}

resource "aws_s3_bucket_versioning" "audit" {
  count = local.audit_count

  bucket = aws_s3_bucket.audit[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  count = local.audit_count

  bucket = aws_s3_bucket.audit[0].id
  rule {
    default_retention {
      mode = var.audit_object_lock_mode
      days = var.audit_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.audit]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  count = local.audit_count

  bucket = aws_s3_bucket.audit[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  count = local.audit_count

  bucket                  = aws_s3_bucket.audit[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "audit" {
  count = local.audit_count

  bucket = aws_s3_bucket.audit[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

data "aws_iam_policy_document" "audit_bucket" {
  count = local.audit_count

  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.audit[0].arn, "${aws_s3_bucket.audit[0].arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # Every put must be conditional (If-None-Match), so no one replaces the current version
  # of an entry by accident; Object Lock keeps the earlier versions in any case.
  statement {
    sid       = "WriteOnce"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit[0].arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Null"
      variable = "s3:if-none-match"
      values   = ["true"]
    }
  }

  # By aws:PrincipalArn rather than as principals, so the policy is valid whatever roles
  # exist and keeps applying to a role recreated under the same name.
  statement {
    sid    = "NeverTheExecutor"
    effect = "Deny"
    actions = [
      "s3:PutObject*",
      "s3:DeleteObject*",
      "s3:BypassGovernanceRetention",
      "s3:RestoreObject",
      "s3:ReplicateObject",
      "s3:ReplicateDelete",
      "s3:PutBucket*",
      "s3:DeleteBucket*",
      "s3:PutLifecycleConfiguration",
      "s3:PutReplicationConfiguration",
      "s3:PutEncryptionConfiguration",
    ]
    resources = [aws_s3_bucket.audit[0].arn, "${aws_s3_bucket.audit[0].arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = local.audit_denied_role_arns
    }
  }
}

resource "aws_s3_bucket_policy" "audit" {
  count = local.audit_count

  bucket = aws_s3_bucket.audit[0].id
  policy = data.aws_iam_policy_document.audit_bucket[0].json

  depends_on = [aws_s3_bucket_public_access_block.audit]
}

# --- Failed records ---------------------------------------------------------------

# Receives the metadata (shard, sequence number range) of records the mapping gave up on;
# the records themselves stay in the stream for 24 hours.
resource "aws_sqs_queue" "audit_dlq" {
  count = local.audit_count

  name                      = "${local.audit_name}-failures"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

# --- Lambda -----------------------------------------------------------------------

resource "aws_iam_role" "audit" {
  count = local.audit_count

  name               = local.audit_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "audit" {
  count = local.audit_count

  name              = "/aws/lambda/${local.audit_name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "audit" {
  count = local.audit_count

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.audit[0].arn}:*"]
  }

  statement {
    sid = "PlanStream"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
      "dynamodb:ListStreams",
    ]
    resources = [aws_dynamodb_table.plans.stream_arn]
  }

  # GetObject only to compare a redelivered record with the object already written.
  statement {
    sid       = "Entries"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.audit[0].arn}/*"]
  }

  # The event source mapping sends failed batches with the function's role.
  statement {
    sid       = "FailedRecords"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.audit_dlq[0].arn]
  }
}

resource "aws_iam_role_policy" "audit" {
  count = local.audit_count

  name   = "runtime"
  role   = aws_iam_role.audit[0].id
  policy = data.aws_iam_policy_document.audit[0].json
}

resource "aws_lambda_function" "audit" {
  count = local.audit_count

  function_name    = local.audit_name
  role             = aws_iam_role.audit[0].arn
  runtime          = local.runtime
  architectures    = [local.architecture]
  handler          = local.handler
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.lambda["audit"].output_path
  source_code_hash = data.archive_file.lambda["audit"].output_base64sha256

  environment {
    variables = {
      AUDIT_BUCKET     = aws_s3_bucket.audit[0].bucket
      METRIC_NAMESPACE = local.audit_metric_namespace
    }
  }

  depends_on = [aws_cloudwatch_log_group.audit, aws_iam_role_policy.audit]

}

resource "aws_lambda_event_source_mapping" "audit" {
  count = local.audit_count

  function_name     = aws_lambda_function.audit[0].arn
  event_source_arn  = aws_dynamodb_table.plans.stream_arn
  starting_position = "TRIM_HORIZON"
  batch_size        = 10
  # One batch per shard at a time, so each plan's records are written in order.
  parallelization_factor         = 1
  function_response_types        = ["ReportBatchItemFailures"]
  bisect_batch_on_function_error = true
  maximum_retry_attempts         = 10
  # Within the stream's 24-hour retention.
  maximum_record_age_in_seconds = 21600

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.audit_dlq[0].arn
    }
  }

  depends_on = [aws_iam_role_policy.audit, aws_s3_bucket_policy.audit, aws_s3_bucket_object_lock_configuration.audit]
}

# --- Alarms -----------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "audit_dlq" {
  count = local.audit_alarm_count

  alarm_name          = "${local.audit_name}-dead-letters"
  alarm_description   = "Plan stream records were not written to the audit bucket and were given up on. The message names the shard and sequence numbers; the records stay in the stream for 24 hours after they were written."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.audit_dlq[0].name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
  ok_actions          = [aws_sns_topic.alerts[0].arn]
}

resource "aws_cloudwatch_metric_alarm" "audit_iterator_age" {
  count = local.audit_alarm_count

  alarm_name          = "${local.audit_name}-iterator-age"
  alarm_description   = "The audit record is more than 5 minutes behind the plan table; a record is failing and being retried."
  namespace           = "AWS/Lambda"
  metric_name         = "IteratorAge"
  dimensions          = { FunctionName = aws_lambda_function.audit[0].function_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 300000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
  ok_actions          = [aws_sns_topic.alerts[0].arn]
}

resource "aws_cloudwatch_metric_alarm" "audit_errors" {
  count = local.audit_alarm_count

  alarm_name          = "${local.audit_name}-errors"
  alarm_description   = "The audit Lambda failed (timeout or start-up error); its batch is retried."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.audit[0].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
}

# A record the Lambda could not write is reported back to the mapping, not raised, so it is
# not a Lambda error. Mismatches (an existing object with other content) count here too.
resource "aws_cloudwatch_metric_alarm" "audit_failures" {
  count = local.audit_alarm_count

  alarm_name          = "${local.audit_name}-failures"
  alarm_description   = "The audit Lambda could not write a plan stream record, or found an existing audit object with different content. Find failed_sequence_numbers in its log."
  namespace           = local.audit_metric_namespace
  metric_name         = "Failures"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
}
