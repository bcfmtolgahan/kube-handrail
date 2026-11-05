#!/usr/bin/env python3
"""Show, approve or reject a kube-handrail plan through the approval API.

Requests are signed with your own AWS credentials (SigV4, service `lambda`), selected the
usual way (AWS_PROFILE, AWS_REGION, ...). Only principals listed in the deployment's
approver_principals can decide; the tool Lambdas' roles never can. The plan is shown as
the server recorded it, followed by a one-line summary of the change, and approve and
reject ask for confirmation unless --yes is given.

  approve.py show <plan_id>
  approve.py approve <plan_id> [--no-apply] [--yes]
  approve.py reject <plan_id> [--reason TEXT] [--yes]

--url defaults to APPROVAL_URL (the approval_url Terraform output). In server and both
mode, approve also runs the apply and prints its result; --no-apply only records the
approval, for a client that applies the plan itself within the apply window.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# https://<url-id>.lambda-url.<region>.on.aws/
URL_REGION = re.compile(r"^https://[^.]+\.lambda-url\.([a-z0-9-]+)\.on\.aws/?$")
TIMEOUT_SECONDS = 90


def region_of(url: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    match = URL_REGION.match(url)
    if not match:
        sys.exit("cannot tell the Region from the URL; pass --region")
    return match.group(1)


def signed_request(
    url: str, region: str, method: str, body: bytes | None = None
) -> urllib.request.Request:
    credentials = botocore.session.Session().get_credentials()
    if credentials is None:
        sys.exit("no AWS credentials found")
    headers = {"content-type": "application/json"} if body is not None else {}
    # Function URLs expect the payload hash as a header, which plain SigV4Auth does not add.
    headers["x-amz-content-sha256"] = hashlib.sha256(body or b"").hexdigest()
    request = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(credentials.get_frozen_credentials(), "lambda", region).add_auth(request)
    return urllib.request.Request(url, data=body, headers=dict(request.headers), method=method)


def send(request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            # A 403 from IAM auth itself has no envelope.
            return error.code, {"ok": False, "error": {"code": f"http_{error.code}"}}


def show(url: str, region: str, plan_id: str) -> tuple[int, dict[str, Any]]:
    query = urllib.parse.urlencode({"plan_id": plan_id})
    return send(signed_request(f"{url.rstrip('/')}/?{query}", region, "GET"))


def decide(
    url: str, region: str, plan_id: str, decision: str, reason: str | None, apply: bool
) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {"plan_id": plan_id, "decision": decision}
    if reason:
        body["reason"] = reason
    if not apply:
        body["apply"] = False
    return send(signed_request(f"{url.rstrip('/')}/", region, "POST", json.dumps(body).encode()))


def emit(envelope: dict[str, Any]) -> None:
    print(json.dumps(envelope, indent=2, sort_keys=True))


def summary(view: dict[str, Any]) -> str:
    """One line naming the action, the Deployment and what changes, from the server's view."""
    change = view.get("change") or {}
    target = f"{view.get('action')} {view.get('namespace')}/{view.get('name')}"
    if "restart" in change:
        return (
            f"{target}: replace {change['restart'].get('pods_replaced')} pod(s), replicas unchanged"
        )
    if "revision" in change:
        revision = change["revision"]
        images = sum(1 for image in change.get("images") or [] if image.get("changed"))
        more = " (list truncated)" if change.get("truncated") else ""
        return (
            f"{target}: revision {revision.get('from')} -> {revision.get('to')} "
            f"(ReplicaSet {change.get('replica_set')}), {images} image(s) changed{more}, "
            "replicas unchanged"
        )
    replicas = change.get("replicas") or view.get("replicas") or {}
    return f"{target}: replicas {replicas.get('from')} -> {replicas.get('to')}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.environ.get("APPROVAL_URL"))
    parser.add_argument("--region", default=None, help="default: taken from the URL")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="show the plan as recorded by the server").add_argument("plan_id")
    p = sub.add_parser("approve", help="approve the plan (and apply it in server mode)")
    p.add_argument("plan_id")
    p.add_argument("--no-apply", action="store_true", help="only record the approval")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p = sub.add_parser("reject", help="reject the plan")
    p.add_argument("plan_id")
    p.add_argument("--reason", default=None)
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.url:
        sys.exit("set --url or APPROVAL_URL")
    region = region_of(args.url, args.region)

    status, envelope = show(args.url, region, args.plan_id)
    emit(envelope)
    if envelope.get("ok"):
        print(summary(envelope.get("data") or {}), file=sys.stderr)
    if args.command == "show" or not envelope.get("ok"):
        sys.exit(0 if envelope.get("ok") else 1)

    if not args.yes:
        answer = input(f"{args.command} plan {args.plan_id} as shown above? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            sys.exit("cancelled")
    apply = args.command == "approve" and not args.no_apply
    status, envelope = decide(
        args.url, region, args.plan_id, args.command, getattr(args, "reason", None), apply
    )
    emit(envelope)
    applied = (envelope.get("data") or {}).get("apply")
    if envelope.get("ok") and args.command == "approve" and applied is None:
        print(
            "Approval recorded; no apply was run. The client must now apply the plan "
            "(in both mode after its own approval) before the approval expires.",
            file=sys.stderr,
        )
    sys.exit(0 if envelope.get("ok") and (applied is None or applied.get("ok")) else 1)


if __name__ == "__main__":
    main()
