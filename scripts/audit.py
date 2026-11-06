#!/usr/bin/env python3
"""Export the audit record of a kube-handrail plan (mcp/CONTRACT.md, Audit record).

Reads the objects the audit Lambda wrote for a plan, in event order, with your own AWS
credentials (s3:ListBucket and s3:GetObject on the bucket), and prints one JSON bundle: every
entry, with the old and new images of the plan record, and a summary naming the requesting
agent, the approver, the channel the apply ran under, the result, and the verification and
revert outcome. A plan's revert record, or the plan a revert record undid, is included under
`linked`.

  audit.py export <plan_id> [--out FILE] [--no-links]
  audit.py list [--limit N]

--bucket defaults to AUDIT_BUCKET, then to `terraform output -raw audit_bucket_name` in TF_DIR
(default: terraform next to this script).
"""

import argparse
import json
import os
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
# through the guard, so it is attributed by construction, not observed.
EXECUTOR_NOTE = "the k8s_write Lambda role, by construction; the record does not observe it"


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


def check_plan_id(plan_id: str) -> str:
    try:
        if str(uuid.UUID(plan_id)) == plan_id:
            return plan_id
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"not a plan id: {plan_id!r}")


def list_keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    # Keys are <time>-<sequence number>, zero-padded, so their order is event order.
    return sorted(keys)


def read_entries(s3: Any, bucket: str, plan_id: str) -> list[dict[str, Any]]:
    entries = []
    for key in list_keys(s3, bucket, f"{PLAN_PREFIX}{plan_id}/"):
        entry = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        entry["key"] = key
        entries.append(entry)
    return entries


def latest_image(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    image: dict[str, Any] = {}
    for entry in entries:
        image = entry.get("new_image") or entry.get("old_image") or image
    return dict(image)


def summarize(plan_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    image = latest_image(entries)
    summary: dict[str, Any] = {"plan_id": plan_id, "entries": len(entries)}
    for name in LATEST_ATTRIBUTES:
        if image.get(name) is not None:
            summary[name] = image[name]
    approvers: list[str] = []
    statuses: list[str] = []
    for entry in entries:
        new = entry.get("new_image") or {}
        if new.get("approved_by") and new["approved_by"] not in approvers:
            approvers.append(new["approved_by"])
        status = new.get("status")
        if status is not None and (not statuses or statuses[-1] != status):
            statuses.append(status)
    summary["approved_by"] = approvers
    summary["status_history"] = statuses
    if image.get("used"):
        summary["executed_by"] = EXECUTOR_NOTE
    verification = image.get("verification")
    if isinstance(verification, Mapping):
        summary["verification"] = {
            "status": verification.get("status"),
            "reason": verification.get("reason"),
        }
    revert = image.get("revert")
    if isinstance(revert, Mapping):
        summary["revert"] = {
            "status": revert.get("status"),
            "reason": revert.get("reason"),
            "revert_plan_id": revert.get("revert_plan_id"),
        }
    events = [entry.get("event") for entry in entries]
    # A plan created before the stream was enabled, or written to the DLQ, lacks its INSERT.
    summary["has_creation"] = "created" in events
    summary["removed"] = next((e for e in events if e in ("expunged", "deleted")), None)
    if entries:
        times = [entry["stream"]["approximate_creation_time"] for entry in entries]
        summary["first_seen"] = min(times)
        summary["last_seen"] = max(times)
    return summary


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
    entries = read_entries(s3, bucket, plan_id)
    summary = summarize(plan_id, entries)
    bundle: dict[str, Any] = {
        "plan_id": plan_id,
        "bucket": bucket,
        "exported_at": int(time.time()),
        "summary": summary,
        "entries": entries,
        "linked": [],
    }
    if follow_links:
        for relation, linked_id in links(summary):
            linked_entries = read_entries(s3, bucket, linked_id)
            bundle["linked"].append(
                {
                    "relation": relation,
                    "plan_id": linked_id,
                    "summary": summarize(linked_id, linked_entries),
                    "entries": linked_entries,
                }
            )
    return bundle


def list_plans(s3: Any, bucket: str, limit: int) -> list[dict[str, Any]]:
    """Plans by their last entry, newest first. Lists every key in the bucket."""
    plans: dict[str, dict[str, Any]] = {}
    for key in list_keys(s3, bucket, PLAN_PREFIX):
        parts = key.split("/")
        if len(parts) != 3:
            continue
        plan_id, name = parts[1], parts[2]
        when = int(name.split("-", 1)[0])
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
