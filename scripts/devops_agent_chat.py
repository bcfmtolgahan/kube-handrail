#!/usr/bin/env python3
"""Drive an AWS DevOps Agent chat from the command line, including directed-action approvals.

Subcommands:
  ask TEXT            start a new chat (or continue --execution-id) and send TEXT;
                      prints the agent's answer and any pending approval request
  approve             approve the pending request in --execution-id (single use)
  reject              reject it, with an optional --reason

The agent space comes from --agent-space-id or AGENT_SPACE_ID. Region and
credentials come from the standard AWS environment (AWS_REGION, AWS_PROFILE).

Start a new chat after changing the association's tools: a chat keeps the tool
list it was created with.
"""

import argparse
import json
import os
import sys
from typing import Any

import boto3


def user_id() -> str:
    # The chat API accepts [a-zA-Z0-9_.-]; use the last segment of the caller ARN.
    return boto3.client("sts").get_caller_identity()["Arn"].rsplit("/", 1)[-1]


def send(client: Any, space: str, execution: str, content: str, context: dict | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"agentSpaceId": space, "executionId": execution, "content": content, "userId": user_id()}
    if context:
        params["context"] = context
    text: list[str] = []
    blocks: dict[int, str] = {}
    failure = None
    for event in client.send_message(**params)["events"]:
        (kind, value), = event.items()
        if kind == "contentBlockDelta":
            delta = value["delta"]
            if "textDelta" in delta:
                text.append(delta["textDelta"]["text"])
            elif "jsonDelta" in delta:
                blocks[value["index"]] = blocks.get(value["index"], "") + delta["jsonDelta"]["partialJson"]
        elif kind == "responseFailed":
            failure = value
    approval = None
    for raw in blocks.values():
        if '"approval_id"' in raw and '"interrupt_id"' in raw:
            approval = json.loads(raw)
    return {"text": "".join(text), "approval": approval, "failure": failure}


def pending_approval(client: Any, space: str, execution: str) -> dict[str, Any]:
    records = client.list_journal_records(agentSpaceId=space, executionId=execution, limit=100, order="DESC")["records"]
    for record in records:
        content = record["content"]
        if '"approval_id"' in content and '"interrupt_id"' in content:
            return json.loads(content)
    sys.exit("no approval request found in this chat")


def print_result(execution: str, result: dict[str, Any]) -> None:
    print(result["text"])
    if result["failure"]:
        print(f"\nresponse failed: {result['failure'].get('errorCode')}: {result['failure'].get('errorMessage')}")
    if result["approval"]:
        data = result["approval"]["data"]
        print("\napproval requested")
        print(f"  tool:      {data['tool']}")
        print(f"  arguments: {json.dumps(data.get('argument_pins'))}")
        print(f"  summary:   {data.get('structured_summary', {}).get('human_readable_summary', '')}")
    print(f"\nexecution id: {execution}")


def ask(client: Any, args: argparse.Namespace) -> None:
    execution = args.execution_id or client.create_chat(
        agentSpaceId=args.agent_space_id, userId=user_id(), userType="IAM"
    )["executionId"]
    print_result(execution, send(client, args.agent_space_id, execution, args.text))


def decide(client: Any, args: argparse.Namespace, action: str) -> None:
    request = pending_approval(client, args.agent_space_id, args.execution_id)
    data = request["data"]
    option = data["options"][0]
    decision: dict[str, Any] = {"agentSpaceId": args.agent_space_id, "approvalId": data["approval_id"], "action": action}
    if action == "APPROVED":
        # Pin exactly the arguments shown to the operator; never widen them.
        decision["finalPattern"] = {"tool": data["tool"], "argumentPins": option["argument_pins"]}
        decision["singleUse"] = True
    elif args.reason:
        decision["reason"] = args.reason
    print(f"decision recorded: {client.update_approval_action(**decision)['status']}")
    context = {
        "userActionResponse": "APPROVAL_ACTION",
        "approvalAction": {
            "toolUseId": data["tool_use_id"],
            "interruptId": request["interrupt_id"],
            "approvalId": data["approval_id"],
            "buttonText": option["display_text"],
            "action": action,
        },
    }
    print_result(args.execution_id, send(client, args.agent_space_id, args.execution_id, option["display_text"], context))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    parser.add_argument("--agent-space-id", default=os.environ.get("AGENT_SPACE_ID"), required=not os.environ.get("AGENT_SPACE_ID"))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ask")
    p.add_argument("text")
    p.add_argument("--execution-id", help="continue an existing chat")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("--execution-id", required=True)
        if name == "reject":
            p.add_argument("--reason")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    client = boto3.client("devops-agent", region_name=args.region)
    if args.command == "ask":
        ask(client, args)
    else:
        decide(client, args, "APPROVED" if args.command == "approve" else "REJECTED")


if __name__ == "__main__":
    main()
