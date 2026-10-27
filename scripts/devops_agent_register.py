#!/usr/bin/env python3
"""Register the gateway with AWS DevOps Agent, one step per subcommand.

Order: create-space, register-mcp, associate-read, then (after the read path
works) enable-write. status shows the current state; disassociate, deregister
and delete-space undo the steps.

IDs are passed as flags or as AGENT_SPACE_ID, SERVICE_ID and ASSOCIATION_ID.
OAuth client credentials are read from CLIENT_ID and CLIENT_SECRET only and
are never printed. Region and credentials come from the standard AWS
environment (AWS_REGION, AWS_PROFILE) or --region.
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import boto3

REPO_ROOT = Path(__file__).resolve().parent.parent
READ_TARGET = ("k8sr", REPO_ROOT / "mcp/k8s_read/tools.json")
WRITE_TARGET = ("k8sw", REPO_ROOT / "mcp/k8s_write/tools.json")
ELEVATED_PREFERENCE = "elevatedActionsEnabled"


def tool_names(target: tuple[str, Path]) -> list[str]:
    prefix, schema = target
    return [f"{prefix}___{tool['name']}" for tool in json.loads(schema.read_text())]


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} must be set in the environment")
    return value


def create_space(client: Any, args: argparse.Namespace) -> None:
    params: dict[str, Any] = {"name": args.name, "clientToken": str(uuid.uuid4())}
    if args.description:
        params["description"] = args.description
    space = client.create_agent_space(**params)["agentSpace"]
    emit({"agentSpaceId": space["agentSpaceId"], "name": space["name"]})


def register_mcp(client: Any, args: argparse.Namespace) -> None:
    credentials: dict[str, Any] = {
        "clientId": require_env("CLIENT_ID"),
        "clientSecret": require_env("CLIENT_SECRET"),
        "exchangeUrl": args.token_url,
    }
    if args.scope:
        credentials["scopes"] = [scope for value in args.scope for scope in value.split()]
    response = client.register_service(
        service="mcpserver",
        name=args.name,
        serviceDetails={
            "mcpserver": {
                "name": args.name,
                "endpoint": args.endpoint,
                "authorizationConfig": {"oAuthClientCredentials": credentials},
            }
        },
    )
    emit({"serviceId": response["serviceId"]})


def mcp_configuration(classifications: dict[str, str]) -> dict[str, Any]:
    """Build an mcpserver configuration where every tool is classified explicitly."""
    tools = sorted(classifications)
    return {
        "mcpserver": {
            "tools": tools,
            "toolDetails": [{"name": t, "toolClassification": classifications[t]} for t in tools],
        }
    }


def current_classifications(client: Any, space_id: str, association_id: str) -> dict[str, str]:
    association = client.get_association(agentSpaceId=space_id, associationId=association_id)
    config = association["association"]["configuration"].get("mcpserver", {})
    details = {d["name"]: d.get("toolClassification", "READ_ONLY") for d in config.get("toolDetails", [])}
    return {tool: details.get(tool, "READ_ONLY") for tool in config.get("tools", [])}


def associate_read(client: Any, args: argparse.Namespace) -> None:
    configuration = mcp_configuration({tool: "READ_ONLY" for tool in tool_names(READ_TARGET)})
    association = client.associate_service(
        agentSpaceId=args.agent_space_id, serviceId=args.service_id, configuration=configuration
    )["association"]
    emit({"associationId": association["associationId"], "tools": configuration["mcpserver"]["tools"]})


def enable_write(client: Any, args: argparse.Namespace) -> None:
    space_id, association_id = args.agent_space_id, args.association_id
    classifications = current_classifications(client, space_id, association_id)
    for tool in tool_names(READ_TARGET):
        classifications.setdefault(tool, "READ_ONLY")
    for tool in tool_names(WRITE_TARGET):
        classifications[tool] = "MUTATIVE"
    # The write tool is added and classified in one update; configuration is replaced wholesale.
    client.update_association(
        agentSpaceId=space_id,
        associationId=association_id,
        configuration=mcp_configuration(classifications),
    )

    # Preferences are also replaced as a set, so keep whatever else is configured.
    space = client.get_agent_space(agentSpaceId=space_id)["agentSpace"]
    preferences = {**space.get("preferences", {}), ELEVATED_PREFERENCE: True}
    client.update_agent_space(agentSpaceId=space_id, preferences=preferences)

    verify_write(client, space_id, association_id)


def verify_write(client: Any, space_id: str, association_id: str) -> None:
    actual = current_classifications(client, space_id, association_id)
    wrong = [tool for tool in tool_names(WRITE_TARGET) if actual.get(tool) != "MUTATIVE"]
    if wrong:
        sys.exit(f"write tools not classified MUTATIVE after update: {wrong}")
    preferences = client.get_agent_space(agentSpaceId=space_id)["agentSpace"].get("preferences", {})
    if preferences.get(ELEVATED_PREFERENCE) is not True:
        sys.exit(f"{ELEVATED_PREFERENCE} is not enabled on the agent space")
    emit({"tools": actual, "preferences": preferences})


def status(client: Any, args: argparse.Namespace) -> None:
    space = client.get_agent_space(agentSpaceId=args.agent_space_id)["agentSpace"]
    associations = client.list_associations(agentSpaceId=args.agent_space_id)["associations"]
    payload: dict[str, Any] = {
        "agentSpace": {
            "agentSpaceId": space["agentSpaceId"],
            "name": space["name"],
            "preferences": space.get("preferences", {}),
        },
        "associations": [
            {k: a.get(k) for k in ("associationId", "serviceId", "status", "updatedAt")}
            for a in associations
        ],
    }
    if args.association_id:
        payload["tools"] = current_classifications(client, args.agent_space_id, args.association_id)
    emit(payload)


def disassociate(client: Any, args: argparse.Namespace) -> None:
    client.disassociate_service(agentSpaceId=args.agent_space_id, associationId=args.association_id)
    emit({"disassociated": args.association_id})


def deregister(client: Any, args: argparse.Namespace) -> None:
    client.deregister_service(serviceId=args.service_id)
    emit({"deregistered": args.service_id})


def delete_space(client: Any, args: argparse.Namespace) -> None:
    if not args.yes:
        sys.exit("refusing to delete the agent space without --yes")
    client.delete_agent_space(agentSpaceId=args.agent_space_id)
    emit({"deleted": args.agent_space_id})


def id_flag(parser: argparse.ArgumentParser, flag: str, env_name: str, required: bool = True) -> None:
    default = os.environ.get(env_name)
    parser.add_argument(flag, default=default, required=required and default is None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        help="defaults to AWS_REGION or AWS_DEFAULT_REGION",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-space")
    p.add_argument("--name", required=True)
    p.add_argument("--description")
    p.set_defaults(func=create_space)

    p = sub.add_parser("register-mcp", help="register the gateway with OAuth client credentials")
    p.add_argument("--name", required=True, help="display name of the MCP server")
    p.add_argument("--endpoint", default=os.environ.get("GATEWAY_URL"), required="GATEWAY_URL" not in os.environ)
    p.add_argument("--token-url", default=os.environ.get("TOKEN_URL"), required="TOKEN_URL" not in os.environ)
    p.add_argument("--scope", action="append", help="OAuth scope(s), space-separated; may be repeated")
    p.set_defaults(func=register_mcp)

    p = sub.add_parser("associate-read", help="associate the server with the k8sr tools only")
    id_flag(p, "--agent-space-id", "AGENT_SPACE_ID")
    id_flag(p, "--service-id", "SERVICE_ID")
    p.set_defaults(func=associate_read)

    p = sub.add_parser("enable-write", help="add k8sw tools as MUTATIVE and allow elevated actions")
    id_flag(p, "--agent-space-id", "AGENT_SPACE_ID")
    id_flag(p, "--association-id", "ASSOCIATION_ID")
    p.set_defaults(func=enable_write)

    p = sub.add_parser("status")
    id_flag(p, "--agent-space-id", "AGENT_SPACE_ID")
    id_flag(p, "--association-id", "ASSOCIATION_ID", required=False)
    p.set_defaults(func=status)

    p = sub.add_parser("disassociate")
    id_flag(p, "--agent-space-id", "AGENT_SPACE_ID")
    id_flag(p, "--association-id", "ASSOCIATION_ID")
    p.set_defaults(func=disassociate)

    p = sub.add_parser("deregister")
    id_flag(p, "--service-id", "SERVICE_ID")
    p.set_defaults(func=deregister)

    p = sub.add_parser("delete-space")
    id_flag(p, "--agent-space-id", "AGENT_SPACE_ID")
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.set_defaults(func=delete_space)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    client = boto3.client("devops-agent", region_name=args.region)
    args.func(client, args)


if __name__ == "__main__":
    main()
