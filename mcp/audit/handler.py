"""Audit Lambda: writes the durable audit record of the plan table (mcp/CONTRACT.md, Audit
record). Not a gateway target.

Invoked by a DynamoDB stream event source mapping on the plan table. Every stream record of
a plan becomes one JSON object in a bucket with S3 Object Lock, written with
If-None-Match: *, so an object is never replaced. The key is derived from the record alone,
so a redelivered record maps to the same key: a 412 for the same entry (the derived summary
aside) is success, and any other difference is a failure that stops the shard and alarms. The tool Lambdas cannot write
to the bucket; the record detects changes after the fact, it does not block them.
"""

import base64
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import Binary, TypeDeserializer
from botocore.exceptions import ClientError

from handrail_common.config import required_env

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_METRIC_NAMESPACE = "kube-handrail/audit"
ENTRY_VERSION = 1
PLAN_PREFIX = "plans/"
# Approximate creation time in epoch seconds and the stream sequence number, zero-padded so
# that keys list in event order. DynamoDB stream sequence numbers have 21 to 40 digits.
TIME_DIGITS = 10
SEQUENCE_DIGITS = 40
# A REMOVE made by the table's TTL carries this identity; any other REMOVE was a delete.
TTL_PRINCIPAL = "dynamodb.amazonaws.com"
EVENTS = {"INSERT": "created", "MODIFY": "modified"}
EXPUNGED = "expunged"
DELETED = "deleted"
# Plan attributes copied into the summary as they are in the image.
SUMMARY_ATTRIBUTES = (
    "cluster",
    "kind",
    "action",
    "target_kind",
    "namespace",
    "name",
    "requested_by",
    "agent",
    "requested_client_id",
    "approved_by",
    "approval_channel",
    "rejected_by",
    "channel",
    "applied_at",
    "reverts_plan_id",
)
# Attributes whose change the summary reports as {"from": ..., "to": ...}.
TRANSITIONS = ("status", "used", "result")

_deserializer = TypeDeserializer()


class AuditMismatch(Exception):
    """An object already exists under the record's key with a different entry."""


def plain(value: Any) -> Any:
    """A deserialized DynamoDB value as plain JSON: integral numbers as int, others as their
    exact decimal string, sets as sorted lists, binary as base64."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, Binary | bytes | bytearray):
        return base64.b64encode(bytes(value)).decode()
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((plain(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


def wire(value: Any) -> Any:
    """A DynamoDB attribute value as the deserializer expects it. Lambda delivers stream
    binary values (B, BS) as base64 strings, which TypeDeserializer refuses."""
    if not isinstance(value, Mapping) or len(value) != 1:
        return value
    ((tag, inner),) = value.items()
    if tag == "B" and isinstance(inner, str):
        return {"B": base64.b64decode(inner)}
    if tag == "BS" and isinstance(inner, list):
        return {"BS": [base64.b64decode(v) if isinstance(v, str) else v for v in inner]}
    if tag == "M" and isinstance(inner, Mapping):
        return {"M": {k: wire(v) for k, v in inner.items()}}
    if tag == "L" and isinstance(inner, list):
        return {"L": [wire(v) for v in inner]}
    return value


def image(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    return {k: plain(_deserializer.deserialize(wire(v))) for k, v in raw.items()}


def canonical(entry: Mapping[str, Any]) -> bytes:
    """The stored bytes: the same entry always encodes the same way."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def is_plan_id(key: str) -> bool:
    try:
        return str(uuid.UUID(key)) == key
    except ValueError:
        return False


def record_plan_id(record: Mapping[str, Any]) -> str:
    keys = record["dynamodb"]["Keys"]
    return str(_deserializer.deserialize(keys["plan_id"]))


def ttl_removal(record: Mapping[str, Any]) -> bool:
    identity = record.get("userIdentity") or {}
    return identity.get("type") == "Service" and identity.get("principalId") == TTL_PRINCIPAL


def object_key(plan_id: str, approximate_time: int, sequence_number: str) -> str:
    if not sequence_number.isdigit() or len(sequence_number) > SEQUENCE_DIGITS:
        raise ValueError(f"unexpected sequence number {sequence_number!r}")
    if not is_plan_id(plan_id):
        raise ValueError("not a plan id")
    return (
        f"{PLAN_PREFIX}{plan_id}/"
        f"{approximate_time:0{TIME_DIGITS}d}-{sequence_number.zfill(SEQUENCE_DIGITS)}.json"
    )


def summarize(
    plan_id: str, old: Mapping[str, Any] | None, new: Mapping[str, Any] | None
) -> dict[str, Any]:
    """A short derived view for humans. The images stay the source of truth."""
    current = new if new is not None else (old or {})
    summary: dict[str, Any] = {"plan_id": plan_id}
    for name in SUMMARY_ATTRIBUTES:
        if current.get(name) is not None:
            summary[name] = current[name]
    for name in TRANSITIONS:
        before = (old or {}).get(name)
        after = (new or {}).get(name)
        if before != after:
            summary[name] = {"from": before, "to": after}
        elif after is not None:
            summary[name] = after
    verification = current.get("verification")
    if isinstance(verification, Mapping):
        summary["verification"] = verification.get("outcome")
    revert = current.get("revert")
    if isinstance(revert, Mapping):
        summary["revert"] = {
            "status": revert.get("status"),
            "revert_plan_id": revert.get("revert_plan_id"),
        }
    old_keys, new_keys = set(old or {}), set(new or {})
    summary["changed"] = sorted(
        k
        for k in old_keys | new_keys
        if (old or {}).get(k) != (new or {}).get(k) or (k in old_keys) != (k in new_keys)
    )
    return summary


def build_entry(record: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The object key and entry for one stream record of a plan. Only fields of the record
    go in, so a redelivered record yields the same bytes."""
    stream = record["dynamodb"]
    name = record["eventName"]
    plan_id = record_plan_id(record)
    approximate_time = int(stream["ApproximateCreationDateTime"])
    sequence_number = str(stream["SequenceNumber"])
    raw_old, raw_new = stream.get("OldImage"), stream.get("NewImage")
    try:
        old, new = image(raw_old), image(raw_new)
        summary = summarize(plan_id, old, new)
        image_format = "plain"
    except Exception as exc:
        # Whatever the tool roles wrote into the plan record, its change is recorded: an
        # image that cannot be decoded is kept as the stream delivered it (DynamoDB JSON).
        # Only S3 errors fail a record.
        logger.warning("record %s: images kept raw (%s)", sequence_number, type(exc).__name__)
        old, new = raw_old, raw_new
        summary = {"plan_id": plan_id, "error": type(exc).__name__}
        image_format = "dynamodb"
    if name == "REMOVE":
        ttl = ttl_removal(record)
        event = EXPUNGED if ttl else DELETED
    elif name in EVENTS:
        ttl = False
        event = EVENTS[name]
    else:
        raise ValueError(f"unexpected event name {name!r}")
    entry = {
        "v": ENTRY_VERSION,
        "plan_id": plan_id,
        "event": event,
        "stream": {
            "event_name": name,
            "event_id": record.get("eventID"),
            "sequence_number": sequence_number,
            "approximate_creation_time": approximate_time,
            "ttl_removal": ttl,
            "user_identity": record.get("userIdentity"),
        },
        "image_format": image_format,
        "old_image": old,
        "new_image": new,
        "summary": summary,
    }
    return object_key(plan_id, approximate_time, sequence_number), entry


@dataclass
class Settings:
    bucket: str
    metric_namespace: str
    region: str | None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(
        bucket=required_env(env, "AUDIT_BUCKET"),
        metric_namespace=env.get("METRIC_NAMESPACE", "").strip() or DEFAULT_METRIC_NAMESPACE,
        region=env.get("AWS_REGION") or None,
    )


@dataclass
class Counts:
    records: int = 0
    written: int = 0
    duplicates: int = 0
    skipped: int = 0
    failures: int = 0
    mismatches: int = 0
    failed: list[str] = field(default_factory=list)


def write_once(s3: Any, bucket: str, key: str, body: bytes) -> bool:
    """Put `body` at `key` unless an object is there. True if written, False if the same
    entry was already there (summary aside); AuditMismatch if a different one is."""
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            # Object Lock requires an integrity header on a put.
            ContentMD5=base64.b64encode(hashlib.md5(body).digest()).decode(),
            IfNoneMatch="*",
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "PreconditionFailed":
            raise
    existing = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if existing != body and recorded(existing) != recorded(body):
        raise AuditMismatch(key)
    return False


def recorded(body: bytes) -> Any:
    """What an entry records: everything but the derived summary, which a newer handler
    version may build differently for a record that is still being retried."""
    try:
        entry = json.loads(body)
    except ValueError:
        return body
    if isinstance(entry, dict):
        entry.pop("summary", None)
    return entry


def process(records: Sequence[Mapping[str, Any]], s3: Any, bucket: str) -> tuple[dict, Counts]:
    """Write each record in order. On the first failure, report that record and stop: the
    event source mapping retries from it, which keeps every shard in order."""
    counts = Counts()
    for record in records:
        counts.records += 1
        sequence_number = str(record.get("dynamodb", {}).get("SequenceNumber", ""))
        try:
            plan_id = record_plan_id(record)
            if not is_plan_id(plan_id):
                # Counters (budget#..., approval#global#...) share the table; not plans.
                counts.skipped += 1
                continue
            key, entry = build_entry(record)
            if write_once(s3, bucket, key, canonical(entry)):
                counts.written += 1
            else:
                counts.duplicates += 1
        except AuditMismatch as exc:
            logger.error("audit object %s exists with different content", exc)
            counts.mismatches += 1
            counts.failures += 1
            counts.failed.append(sequence_number)
            break
        except Exception:
            logger.exception("cannot write the audit entry of record %s", sequence_number)
            counts.failures += 1
            counts.failed.append(sequence_number)
            break
    failures = [{"itemIdentifier": seq} for seq in counts.failed]
    return {"batchItemFailures": failures}, counts


def emit(counts: Counts, namespace: str, correlation_id: str | None) -> None:
    """One log line per invocation that is also a CloudWatch embedded metric format record.
    Only counts and sequence numbers are logged, never plan contents."""
    metrics = {
        "Records": counts.records,
        "Written": counts.written,
        "Duplicates": counts.duplicates,
        "Skipped": counts.skipped,
        "Failures": counts.failures,
        "Mismatches": counts.mismatches,
    }
    line = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [[]],
                    "Metrics": [{"Name": name, "Unit": "Count"} for name in metrics],
                }
            ],
        },
        **metrics,
        "failed_sequence_numbers": counts.failed,
        "correlation_id": correlation_id,
    }
    print(json.dumps(line), flush=True)


_settings: Settings | None = None
_s3: Any = None


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    global _settings, _s3
    if _settings is None:
        _settings = load_settings()
        _s3 = boto3.client("s3", region_name=_settings.region)
    response, counts = process(event.get("Records") or [], _s3, _settings.bucket)
    emit(counts, _settings.metric_namespace, getattr(context, "aws_request_id", None))
    return response
