#!/usr/bin/env python3
"""Export the audit record of a kube-handrail plan (mcp/CONTRACT.md, Audit record).

Reads the objects the audit Lambda wrote for a plan, in event order, with your own AWS
credentials (s3:ListBucket and s3:GetObject on the bucket), and prints one JSON bundle: every
entry, with the old and new images of the plan record, and a summary naming the requesting
agent, the approver, the channel the apply ran under, the result, and the verification and
revert outcome. A plan's revert record, or the plan a revert record undid, is included under
`linked`.

`verify` checks a plan's entries for what no writer of the record produces, and exits 1 on
any finding (information does not count):

  no_entries, malformed, undecoded, not_plan (a prefix under plans/ that is not a plan id)
  missing_creation, gap (an entry that does not start where the previous one ended), deleted
  pre_consume_rewrite   before the consume, a change to an attribute set when the plan was
                        made (args, args_hash, replicas_*, requested_by, ...)
  approval_rewrite      approval or rejection attributes written, changed or removed outside
                        the one decision of an undecided plan, or a decided status undone
  approval_mode_changed
  executor_rewrite      the consume writing more than used, channel, verify_at, verify_queue
                        and revert_to; after it, a change to anything but result, applied_at,
                        apply_correlation_id, applied_generation, verification, revert and
                        verify_queue, or one of them written again
  impossible_consume    a consume the consume's own condition refuses: rejected, expired, or
                        a plan created consumed
  invalid_revert        a revert record that is not created consumed by system:revert with a
                        reverts_plan_id
  wrong_channel         client and both apply through gateway, server through approval-api or
                        gateway, a revert record on verifier
  applied_without_approval  server or both mode, consumed without an approval of the plan's
                        args_hash by someone other than the requester, through api, unexpired
  info: client_unobserved   a client-mode apply: the approval was given in the DevOps Agent
                        chat, which the record cannot see; CloudTrail logs it with the
                        approver but without the plan id, so look in approval_window by time
  info: approval_mode_unknown  a record made before approval_mode; --approval-mode MODE
                        checks it as that mode
  info: revert_retried  a revert's second run recorded applied over the first's error code

The mode checked is the one recorded on the plan: a deployment whose APPROVAL_MODE changed
while a plan was open reports that plan against the mode it was planned under. Stream times
are approximate, so expiries are compared with a margin of a minute.

  audit.py export <plan_id> [--out FILE] [--no-links]
  audit.py verify (<plan_id> | --since SECONDS | --all) [--json] [--approval-mode MODE]
  audit.py list [--limit N]

--bucket defaults to AUDIT_BUCKET, then to `terraform output -raw audit_bucket_name` in TF_DIR
(default: terraform next to this script).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

PLAN_PREFIX = "plans/"
# Summary attributes taken from the latest image of the plan record.
LATEST_ATTRIBUTES = (
    "kind",
    "action",
    "namespace",
    "name",
    "requested_by",
    "agent",
    "requested_client_id",
    "approval_channel",
    "approval_mode",
    "rejected_by",
    "reject_reason",
    "channel",
    "result",
    "applied_at",
    "used",
    "status",
    "reverts_plan_id",
)
# The executing role is not in the record: only the write Lambda's role can consume a plan
# through the guard, so it is attributed by construction, not observed. The export adds query
# templates for the logs that do observe it.
EXECUTOR_ATTRIBUTION = (
    "the k8s_write Lambda role, by construction; the record does not observe it. Run the "
    "queries to see the invocations and the Kubernetes requests"
)
# Seconds added before the first and after the last entry for the query time range.
QUERY_MARGIN_SECONDS = 300
# The bucket is <name_prefix>-audit-<account id> (terraform/audit.tf).
BUCKET_NAME = re.compile(r"^(?P<prefix>.+)-audit-\d{12}$")

# Approval modes (APPROVAL_MODE) and the channels an apply may run under in each
# (mcp/CONTRACT.md, Server-side approval): only in server mode does the approval channel
# apply; a revert runs on the verifier channel whatever the mode.
CLIENT, SERVER, BOTH = "client", "server", "both"
CHANNELS_BY_MODE = {
    CLIENT: ("gateway",),
    SERVER: ("approval-api", "gateway"),
    BOTH: ("gateway",),
}
REVERT_CHANNEL = "verifier"
APPROVAL_CHANNEL = "api"
# What may change in a plan record once it is consumed: the result (record_result), the
# verification and revert (each written once) and the verify-due marker. Anything else is
# the executor-rewrite pattern: after the consume only the write role writes the record.
AFTER_CONSUME = frozenset(
    {
        "result",
        "applied_at",
        "apply_correlation_id",
        "applied_generation",
        "verification",
        "revert",
        "verify_queue",
    }
)
# What the consume itself writes, in the one conditional update (PlanStore.consume).
# An HPA adjust pins revert_min_to and revert_max_to instead of revert_to.
CONSUME = frozenset(
    {"used", "channel", "verify_at", "verify_queue", "revert_to", "revert_min_to", "revert_max_to"}
)
WRITE_ONCE = ("verification", "revert")
# record_result writes these together, once per plan; a revert record's second run may
# record `applied` over the error code of the first.
RESULT_ATTRIBUTES = frozenset(
    {"result", "applied_at", "apply_correlation_id", "applied_generation"}
)
# Set when the plan is made and never written again: no update of the plan store touches
# them (expires_at is not here: the approval request and both decisions set it).
PINNED = frozenset(
    {
        "action",
        "namespace",
        "name",
        "cluster",
        "args",
        "args_hash",
        "replicas_from",
        "replicas_to",
        "resource_version",
        "generation",
        "uid",
        "object_key",
        "created_at",
        "retain_until",
        "requested_by",
        "agent",
        "requested_client_id",
        "override_manual_change",
        "impact",
        "images",
        "fields_changed",
        "diff_truncated",
        "template_from",
        "template_to",
        "revision_from",
        "revision_to",
        "target_rs_name",
        "target_rs_uid",
        "verify_window_seconds",
        "plan_correlation_id",
        "kind",
        "reverts_plan_id",
        "target_kind",
        "hpa_target",
        "min_from",
        "min_to",
        "max_from",
        "max_to",
        "spec_from",
        "spec_to",
    }
)
DECIDED = ("approved", "rejected")
REVERT_REQUESTER = "system:revert"
# Stream times are approximate (whole seconds, another clock than the Lambda's): an expiry
# is only reported when the consume came this much later.
CLOCK_MARGIN_SECONDS = 60
# Written by the approval channel, in the one update that decides.
APPROVAL_ATTRIBUTES = frozenset(
    {"approved_by", "approved_at", "approved_args_hash", "approval_channel", "approval_expires_at"}
)
# ... or by a rejection, in the same way.
DECISION_ATTRIBUTES = APPROVAL_ATTRIBUTES | {"rejected_by", "rejected_at", "reject_reason"}
DECIDABLE = ("planned", "awaiting_approval")
# Severities: a finding fails verify; information does not.
FINDING, INFO = "finding", "info"


def bucket_name(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("AUDIT_BUCKET"):
        return os.environ["AUDIT_BUCKET"]
    tf_dir = os.environ.get("TF_DIR", str(Path(__file__).resolve().parent.parent / "terraform"))
    try:
        out = subprocess.run(
            ["terraform", f"-chdir={tf_dir}", "output", "-raw", "audit_bucket_name"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        out = ""
    if not out or out == "null":
        sys.exit("no audit bucket: pass --bucket or set AUDIT_BUCKET")
    return out


def is_plan_id(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def check_plan_id(plan_id: str) -> str:
    if is_plan_id(plan_id):
        return plan_id
    raise argparse.ArgumentTypeError(f"not a plan id: {plan_id!r}")


def list_keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    # Keys are <time>-<sequence number>, zero-padded, so their order is event order.
    return sorted(keys)


def well_formed(entry: Any) -> bool:
    """An entry in the shape the audit Lambda writes (its stream time is what is relied on)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("stream"), dict):
        return False
    when = entry["stream"].get("approximate_creation_time")
    return isinstance(when, int) and not isinstance(when, bool)


def read_entries(s3: Any, bucket: str, plan_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The plan's entries in order, and the keys of objects under its prefix that are not
    entries the Lambda wrote (reported, not trusted)."""
    entries, malformed = [], []
    for key in list_keys(s3, bucket, f"{PLAN_PREFIX}{plan_id}/"):
        try:
            entry = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except ValueError:
            entry = None
        if not well_formed(entry):
            malformed.append(key)
            continue
        entry["key"] = key
        entries.append(entry)
    return entries, malformed


def decoded(entries: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Entries whose images are plain JSON; undecoded ones (DynamoDB JSON) are kept in the
    bundle but not read for the summary."""
    return [e for e in entries if e.get("image_format", "plain") == "plain"]


def latest_image(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    image: dict[str, Any] = {}
    for entry in entries:
        image = entry.get("new_image") or entry.get("old_image") or image
    return dict(image)


def summarize(
    plan_id: str, entries: list[dict[str, Any]], malformed: list[str] | None = None
) -> dict[str, Any]:
    readable = decoded(entries)
    image = latest_image(readable)
    summary: dict[str, Any] = {"plan_id": plan_id, "entries": len(entries)}
    for name in LATEST_ATTRIBUTES:
        if image.get(name) is not None:
            summary[name] = image[name]
    approvers: list[str] = []
    statuses: list[str] = []
    for entry in readable:
        new = entry.get("new_image") or {}
        if new.get("approved_by") and new["approved_by"] not in approvers:
            approvers.append(new["approved_by"])
        status = new.get("status")
        if status is not None and (not statuses or statuses[-1] != status):
            statuses.append(status)
    summary["approved_by"] = approvers
    summary["status_history"] = statuses
    verification = image.get("verification")
    if isinstance(verification, Mapping):
        summary["verification"] = {
            "outcome": verification.get("outcome"),
            "reason": verification.get("reason"),
        }
    revert = image.get("revert")
    if isinstance(revert, Mapping):
        summary["revert"] = {
            "status": revert.get("status"),
            "reason": revert.get("reason"),
            "revert_plan_id": revert.get("revert_plan_id"),
        }
    summary["undecoded"] = len(entries) - len(readable)
    summary["malformed"] = len(malformed or [])
    events = [entry.get("event") for entry in entries]
    # A plan created before the stream was enabled, or written to the DLQ, lacks its INSERT.
    summary["has_creation"] = "created" in events
    summary["removed"] = next((e for e in events if e in ("expunged", "deleted")), None)
    if entries:
        times = [entry["stream"]["approximate_creation_time"] for entry in entries]
        summary["first_seen"] = min(times)
        summary["last_seen"] = max(times)
    return summary


def correlation_ids(image: Mapping[str, Any]) -> list[str]:
    """The correlation ids the record holds, by step, without repeats."""
    found = [
        image.get(name)
        for name in (
            "plan_correlation_id",
            "approval_request_correlation_id",
            "decision_correlation_id",
            "apply_correlation_id",
        )
    ]
    verification = image.get("verification")
    if isinstance(verification, Mapping):
        found.append(verification.get("correlation_id"))
    out: list[str] = []
    for value in found:
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


def name_prefix(bucket: str) -> str:
    match = BUCKET_NAME.match(bucket)
    return match.group("prefix") if match else "<name_prefix>"


def queries(
    plan_id: str, bucket: str, summary: Mapping[str, Any], image: Mapping[str, Any]
) -> dict[str, Any]:
    """CloudWatch Logs Insights query templates for the logs that observe the executor: the
    Lambdas' structured records (by plan id and the record's correlation ids) and, when the
    cluster's audit log goes to CloudWatch Logs, the Kubernetes requests the Lambdas made for
    the plan (by the User-Agent plan/<plan_id>)."""
    prefix = name_prefix(bucket)
    ids = correlation_ids(image)
    condition = f'@message like "{plan_id}"'
    if ids:
        condition += " or correlation_id in [" + ", ".join(f'"{i}"' for i in ids) + "]"
    time_range: dict[str, Any] = {}
    if "first_seen" in summary:
        time_range = {
            "start": summary["first_seen"] - QUERY_MARGIN_SECONDS,
            "end": summary["last_seen"] + QUERY_MARGIN_SECONDS,
        }
    cluster = image.get("cluster") or "<cluster_name>"
    return {
        "lambda_logs": {
            "log_groups": [
                f"/aws/lambda/{prefix}-{name}"
                for name in ("k8s-read", "k8s-write", "approver", "verifier")
            ],
            "query": (
                "fields @timestamp, @log, tool, event, outcome, caller, approver, "
                f"correlation_id, plan_id\n| filter {condition}\n| sort @timestamp asc"
            ),
            **time_range,
        },
        "eks_audit_log": {
            "log_groups": [f"/aws/eks/{cluster}/cluster"],
            "query": (
                "fields @timestamp, verb, objectRef.namespace, objectRef.name, user.username, "
                "user.groups.0, responseStatus.code, userAgent\n"
                "| filter @logStream like /^kube-apiserver-audit/\n"
                f'| filter userAgent like "plan/{plan_id}"\n| sort @timestamp asc'
            ),
            **time_range,
            "note": "needs the cluster's audit log type enabled (EKS control plane logging)",
        },
    }


def executed_by(
    plan_id: str, bucket: str, summary: Mapping[str, Any], image: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "attribution": EXECUTOR_ATTRIBUTION,
        "queries": queries(plan_id, bucket, summary, image),
    }


def links(summary: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(relation, plan_id) of the linked revert records, one level only: a revert is never
    reverted."""
    found = []
    revert = summary.get("revert") or {}
    if revert.get("revert_plan_id"):
        found.append(("revert", revert["revert_plan_id"]))
    if summary.get("reverts_plan_id"):
        found.append(("reverts", summary["reverts_plan_id"]))
    return found


def export(s3: Any, bucket: str, plan_id: str, follow_links: bool = True) -> dict[str, Any]:
    entries, malformed = read_entries(s3, bucket, plan_id)
    summary = summarize(plan_id, entries, malformed)
    image = latest_image(decoded(entries))
    if image.get("used"):
        summary["executed_by"] = executed_by(plan_id, bucket, summary, image)
    bundle: dict[str, Any] = {
        "plan_id": plan_id,
        "bucket": bucket,
        "exported_at": int(time.time()),
        "summary": summary,
        "entries": entries,
        "malformed": malformed,
        "linked": [],
    }
    if follow_links:
        for relation, linked_id in links(summary):
            if not is_plan_id(linked_id):
                continue
            linked_entries, linked_malformed = read_entries(s3, bucket, linked_id)
            bundle["linked"].append(
                {
                    "relation": relation,
                    "plan_id": linked_id,
                    "summary": summarize(linked_id, linked_entries, linked_malformed),
                    "entries": linked_entries,
                    "malformed": linked_malformed,
                }
            )
    return bundle


def finding(code: str, message: str, severity: str = FINDING, **details: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if details:
        out["details"] = details
    return out


def changed(old: Mapping[str, Any] | None, new: Mapping[str, Any] | None) -> set[str]:
    """Attributes an entry added, removed or changed, from its images."""
    old, new = old or {}, new or {}
    return {k for k in set(old) | set(new) if k not in old or k not in new or old[k] != new[k]}


def consume_index(entries: list[Mapping[str, Any]]) -> int | None:
    """The entry that consumed the plan (`used` from false to true), or the creation of a
    record created consumed (a revert record)."""
    for index, entry in enumerate(entries):
        old, new = entry.get("old_image") or {}, entry.get("new_image") or {}
        if new.get("used") is True and old.get("used") is not True:
            return index
    return None


def is_decision(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """The approval channel's update: an undecided plan, never decided before, becomes
    approved or rejected."""
    return (
        old.get("status") in DECIDABLE
        and new.get("status") in DECIDED
        and old.get("approved_by") is None
        and old.get("rejected_by") is None
    )


def is_revert(image: Mapping[str, Any]) -> bool:
    return image.get("kind") == "revert" or image.get("status") == "revert"


def is_plain(entry: Mapping[str, Any]) -> bool:
    return entry.get("image_format", "plain") == "plain"


def check_sequence(entries: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Missing creation, gaps between consecutive entries, and deletions. An undecoded entry
    (reported on its own) is not compared with its neighbours."""
    found = []
    if not any(e.get("event") == "created" for e in entries):
        found.append(
            finding(
                "missing_creation",
                "no creation entry: created before the stream was enabled, or a record went "
                "to the dead-letter queue",
            )
        )
    previous = None
    for entry in entries:
        if entry.get("event") == "deleted":
            found.append(
                finding("deleted", "the plan record was deleted, not expired", key=entry.get("key"))
            )
        if not is_plain(entry):
            previous = None
            continue
        old = entry.get("old_image")
        if previous is not None and old is not None and old != previous.get("new_image"):
            found.append(
                finding(
                    "gap",
                    "the entry does not start where the previous one ended: a change is "
                    "missing from the record",
                    key=entry.get("key"),
                    after=previous.get("key"),
                )
            )
        previous = entry
    return found


def check_rewrites(entries: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Changes no writer of the record makes. Before the consume: an attribute fixed at
    creation, or a decision written, changed or undone outside the one decision. The consume
    changing more than it writes. After it (the executor-rewrite pattern): a change to
    anything but the result, verification and revert, or one of them written again. At any
    time: a changed approval mode."""
    found = []
    consumed = consume_index(entries)
    for index, entry in enumerate(entries):
        if entry.get("event") != "modified":
            continue
        old, new = entry.get("old_image") or {}, entry.get("new_image") or {}
        names = changed(old, new)
        key = entry.get("key")
        if "approval_mode" in names:
            found.append(
                finding(
                    "approval_mode_changed",
                    "the plan's approval mode changed after it was planned",
                    key=key,
                    before=old.get("approval_mode"),
                    after=new.get("approval_mode"),
                )
            )
            names.discard("approval_mode")
        if index == consumed:
            if names - CONSUME:
                found.append(
                    finding(
                        "executor_rewrite",
                        "the consume changed more than it writes (used, channel, verify_at, "
                        "verify_queue, revert_to or the HPA revert bounds)",
                        key=key,
                        changed=sorted(names - CONSUME),
                        rewritten=[],
                    )
                )
            continue
        if consumed is not None and index > consumed:
            unexpected = sorted(names - AFTER_CONSUME)
            again = [n for n in WRITE_ONCE if n in names and old.get(n) is not None]
            result = names & RESULT_ATTRIBUTES
            if result and (old.get("result") is not None or old.get("applied_at") is not None):
                if revert_retried(old, new, names):
                    found.append(
                        finding(
                            "revert_retried",
                            "a revert's second run recorded applied over the error code "
                            "of the first",
                            INFO,
                            key=key,
                        )
                    )
                else:
                    again += sorted(result)
            if unexpected or again:
                found.append(
                    finding(
                        "executor_rewrite",
                        "the record changed after the consume beyond the result, "
                        "verification and revert; only the write role writes it then",
                        key=key,
                        changed=unexpected,
                        rewritten=again,
                    )
                )
            continue
        pinned = sorted(names & PINNED)
        if pinned:
            found.append(
                finding(
                    "pre_consume_rewrite",
                    "attributes fixed when the plan was made changed before the consume",
                    key=key,
                    changed=pinned,
                )
            )
        decision = sorted(names & DECISION_ATTRIBUTES)
        undone = old.get("status") in DECIDED and new.get("status") != old.get("status")
        if undone or (decision and not is_decision(old, new)):
            found.append(
                finding(
                    "approval_rewrite",
                    "a decision was written, changed or undone outside the approval "
                    "channel's one decision",
                    key=key,
                    changed=decision,
                    status={"from": old.get("status"), "to": new.get("status")},
                )
            )
    return found


def revert_retried(old: Mapping[str, Any], new: Mapping[str, Any], names: set[str]) -> bool:
    """A revert record's second run: the first stopped with an error code, the second
    patched (k8s_write: the plan's revert is written after the revert record's result)."""
    return (
        is_revert(new)
        and names <= RESULT_ATTRIBUTES
        and old.get("result") not in (None, "applied")
        and new.get("result") == "applied"
    )


def check_revert(entry: Mapping[str, Any], record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A revert record is created consumed, by the verifier, for a plan."""
    key = entry.get("key")
    found = []
    if record.get("channel") != REVERT_CHANNEL:
        found.append(
            finding(
                "wrong_channel",
                f"a revert record must run on the {REVERT_CHANNEL} channel",
                key=key,
                channel=record.get("channel"),
            )
        )
    problems = []
    if entry.get("event") != "created":
        problems.append("not created consumed")
    if record.get("requested_by") != REVERT_REQUESTER:
        problems.append(f"requested_by {record.get('requested_by')!r}")
    if not is_plan_id(record.get("reverts_plan_id")):
        problems.append("no reverts_plan_id")
    if problems:
        found.append(
            finding(
                "invalid_revert",
                "not a revert record the verifier creates: " + ", ".join(problems),
                key=key,
            )
        )
    return found


def check_apply(entries: list[Mapping[str, Any]], default_mode: str | None) -> list[dict[str, Any]]:
    """The consume against its own condition, and its channel and approval against the
    plan's approval mode."""
    consumed = consume_index(entries)
    if consumed is None:
        return []
    entry = entries[consumed]
    record = entry.get("new_image") or {}
    key = entry.get("key")
    if is_revert(record):
        return check_revert(entry, record)
    found = []
    at = entry["stream"]["approximate_creation_time"]
    late = at - CLOCK_MARGIN_SECONDS
    impossible = []
    if entry.get("event") == "created":
        impossible.append("created consumed")
    if record.get("rejected_by") is not None or record.get("status") == "rejected":
        impossible.append("rejected")
    expires_at = record.get("expires_at")
    if isinstance(expires_at, int) and expires_at <= late:
        impossible.append(f"expired at {expires_at}")
    if impossible:
        found.append(
            finding(
                "impossible_consume",
                "consumed although the consume's condition fails: " + ", ".join(impossible),
                key=key,
                consumed_at=at,
            )
        )
    channel = record.get("channel")
    mode = record.get("approval_mode") or default_mode
    if mode not in CHANNELS_BY_MODE:
        found.append(
            finding(
                "approval_mode_unknown",
                "the record has no approval mode (planned before it was recorded); pass "
                "--approval-mode to check the channel and the approval",
                INFO,
                key=key,
            )
        )
        return found
    if channel not in CHANNELS_BY_MODE[mode]:
        found.append(
            finding(
                "wrong_channel",
                f"applied on channel {channel!r}; {mode} mode allows "
                + " or ".join(CHANNELS_BY_MODE[mode]),
                key=key,
                channel=channel,
                approval_mode=mode,
            )
        )
    approved_by = record.get("approved_by")
    if mode in (SERVER, BOTH):
        problems = []
        if not approved_by:
            problems.append("no approved_by")
        elif approved_by == record.get("requested_by"):
            problems.append("approved by the requester")
        if record.get("status") != "approved":
            problems.append(f"status {record.get('status')!r}")
        if record.get("approved_args_hash") != record.get("args_hash"):
            problems.append("approval of other arguments")
        if approved_by and record.get("approval_channel") != APPROVAL_CHANNEL:
            problems.append(f"approval channel {record.get('approval_channel')!r}")
        approval_expires_at = record.get("approval_expires_at")
        if approved_by and (
            not isinstance(approval_expires_at, int) or approval_expires_at <= late
        ):
            problems.append(f"approval expired at {approval_expires_at}")
        if problems:
            found.append(
                finding(
                    "applied_without_approval",
                    f"consumed in {mode} mode without a valid server approval: "
                    + ", ".join(problems),
                    key=key,
                    approval_mode=mode,
                )
            )
    elif not approved_by:
        times = [e["stream"]["approximate_creation_time"] for e in entries[: consumed + 1]]
        found.append(
            finding(
                "client_unobserved",
                "client mode: the approval was given in the client (the DevOps Agent chat), "
                "which the record cannot see; CloudTrail logs it (UpdateApprovalAction) with "
                "the approver but without the plan id, so it can only be matched by time",
                INFO,
                key=key,
                approval_window={
                    "from": min(times) - CLOCK_MARGIN_SECONDS,
                    "to": max(times) + CLOCK_MARGIN_SECONDS,
                },
            )
        )
    return found


def verify_plan(
    s3: Any, bucket: str, plan_id: str, default_mode: str | None = None
) -> dict[str, Any]:
    """The findings for one plan, worst first: findings, then information."""
    entries, malformed = read_entries(s3, bucket, plan_id)
    found: list[dict[str, Any]] = []
    if not entries:
        found.append(finding("no_entries", "the bucket holds no entries for this plan"))
    for key in malformed:
        found.append(finding("malformed", "an object that is not an audit entry", key=key))
    readable = decoded(entries)
    for entry in entries:
        if not is_plain(entry):
            found.append(
                finding(
                    "undecoded",
                    "an entry whose images could not be decoded; read it by hand",
                    key=entry.get("key"),
                )
            )
    if entries:
        found += check_sequence(entries)
        found += check_rewrites(readable)
        found += check_apply(readable, default_mode)
    found.sort(key=lambda f: f["severity"] != FINDING)
    return {
        "plan_id": plan_id,
        "entries": len(entries),
        "ok": not any(f["severity"] == FINDING for f in found),
        "findings": found,
    }


def verify(
    s3: Any,
    bucket: str,
    plan_ids: Iterable[str],
    default_mode: str | None = None,
    not_plans: Iterable[str] = (),
) -> dict[str, Any]:
    """`not_plans`: prefixes under plans/ that are not plan ids, which the audit Lambda
    never writes; each counts as a finding."""
    plans = [verify_plan(s3, bucket, plan_id, default_mode) for plan_id in plan_ids]
    not_plans = sorted(not_plans)
    findings = sum(1 for plan in plans for f in plan["findings"] if f["severity"] == FINDING)
    return {
        "bucket": bucket,
        "verified_at": int(time.time()),
        "plans": plans,
        "not_plans": not_plans,
        "findings": findings + len(not_plans),
        "ok": not not_plans and all(plan["ok"] for plan in plans),
    }


def plans_since(s3: Any, bucket: str, since: int | None) -> tuple[list[str], list[str]]:
    """Plan ids with an entry at or after `since` (every plan when None), oldest first, and
    the prefixes in the same window that are not plan ids."""
    plans = [
        plan
        for plan in reversed(list_plans(s3, bucket, limit=None))
        if since is None or plan["last_seen"] >= since
    ]
    return (
        [plan["plan_id"] for plan in plans if is_plan_id(plan["plan_id"])],
        [plan["plan_id"] for plan in plans if not is_plan_id(plan["plan_id"])],
    )


def verify_text(report: Mapping[str, Any]) -> str:
    lines = []
    for plan in report["plans"]:
        lines.append(f"{plan['plan_id']}: {'ok' if plan['ok'] else 'FINDINGS'}")
        for f in plan["findings"]:
            where = f" ({f['details']['key']})" if f.get("details", {}).get("key") else ""
            lines.append(f"  {f['severity']}: {f['code']}: {f['message']}{where}")
    for prefix in report.get("not_plans", []):
        lines.append(f"{PLAN_PREFIX}{prefix}/: FINDINGS")
        lines.append("  finding: not_plan: a prefix that is not a plan id; read it by hand")
    lines.append(f"{len(report['plans'])} plan(s), {report['findings']} finding(s)")
    return "\n".join(lines)


def list_plans(s3: Any, bucket: str, limit: int | None) -> list[dict[str, Any]]:
    """Plans by their last entry, newest first. Lists every key in the bucket."""
    plans: dict[str, dict[str, Any]] = {}
    for key in list_keys(s3, bucket, PLAN_PREFIX):
        parts = key.split("/")
        if len(parts) != 3:
            continue
        plan_id, name = parts[1], parts[2]
        try:
            when = int(name.split("-", 1)[0])
        except ValueError:
            # Not a key the Lambda wrote; export reports it as malformed.
            continue
        plan = plans.setdefault(plan_id, {"plan_id": plan_id, "entries": 0, "last_seen": 0})
        plan["entries"] += 1
        plan["last_seen"] = max(plan["last_seen"], when)
    return sorted(plans.values(), key=lambda p: p["last_seen"], reverse=True)[:limit]


def main(argv: list[str] | None = None, s3: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", help="audit bucket (the audit_bucket_name output)")
    parser.add_argument("--region", help="AWS region of the bucket")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="export one plan's audit record")
    exp.add_argument("plan_id", type=check_plan_id)
    exp.add_argument("--out", help="write the bundle to this file instead of stdout")
    exp.add_argument("--no-links", action="store_true", help="do not include linked reverts")
    doc = __doc__ or ""
    ver = sub.add_parser(
        "verify",
        help="check plans' audit entries for anomalies",
        description=doc[doc.find("`verify`") : doc.find("  audit.py export")],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    which = ver.add_mutually_exclusive_group(required=True)
    which.add_argument("plan_id", nargs="?", type=check_plan_id)
    which.add_argument(
        "--since", type=int, metavar="SECONDS", help="every plan with an entry in the last SECONDS"
    )
    which.add_argument("--all", action="store_true", help="every plan in the bucket")
    ver.add_argument("--json", action="store_true", help="print the report as JSON")
    ver.add_argument(
        "--approval-mode",
        choices=sorted(CHANNELS_BY_MODE),
        help="the mode to assume for records made before approval_mode was recorded",
    )
    lst = sub.add_parser("list", help="list plans with audit entries, newest first")
    lst.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    bucket = bucket_name(args.bucket)
    if s3 is None:
        import boto3

        s3 = boto3.client("s3", region_name=args.region)
    if args.command == "list":
        print(json.dumps(list_plans(s3, bucket, args.limit), indent=2))
        return 0
    if args.command == "verify":
        not_plans: list[str] = []
        if args.plan_id:
            plan_ids = [args.plan_id]
        else:
            since = None if args.all else int(time.time()) - args.since
            plan_ids, not_plans = plans_since(s3, bucket, since)
        report = verify(s3, bucket, plan_ids, args.approval_mode, not_plans)
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else verify_text(report))
        return 0 if report["ok"] else 1
    bundle = export(s3, bucket, args.plan_id, follow_links=not args.no_links)
    text = json.dumps(bundle, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n")
    else:
        print(text)
    if not bundle["entries"]:
        print(f"no audit entries for plan {args.plan_id}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
