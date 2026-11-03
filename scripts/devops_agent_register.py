#!/usr/bin/env python3
"""Register the gateway with AWS DevOps Agent, one step per subcommand.

Order: create-space, register-mcp, associate-read, then (after the read path
works) enable-write, then install-guidance. status shows the current state;
remove-guidance, disassociate, deregister and delete-space undo the steps.

IDs are passed as flags or as AGENT_SPACE_ID, SERVICE_ID and ASSOCIATION_ID.
OAuth client credentials are read from CLIENT_ID and CLIENT_SECRET only and
are never printed. Region and credentials come from the standard AWS
environment (AWS_REGION, AWS_PROFILE) or --region.

Tool names and classifications come from mcp/classification.json. The agent
guidance comes from AGENTS.md and agent/skills/<name>/SKILL.md.
"""

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "mcp/common"))

from handrail_common.drift import (  # noqa: E402
    ELEVATED_PREFERENCE,
    MUTATIVE,
    READ_ONLY,
    association_classifications,
    load_classification,
)

CLASSIFICATION = REPO_ROOT / "mcp/classification.json"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
SKILLS_DIR = REPO_ROOT / "agent/skills"

AGENTS_MD_TYPE = "agents_md"
SKILL_TYPE = "skill"
AGENTS_MD_PATH = "AGENTS.md"
SKILL_PATH = "SKILL.md"
# The first line of AGENTS.md. An agents_md asset that does not start with it was not
# installed from this repository, and install-guidance refuses to overwrite it.
AGENTS_MD_MARKER = "<!-- kube-handrail:agents-md -->"
# Limits from the DevOps Agent asset documentation.
AGENTS_MD_MAX_BYTES = 25_000
SKILL_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
SKILL_DESCRIPTION_MAX = 1024
GENERIC = "GENERIC"
AGENT_TYPES = (
    GENERIC,
    "CHAT",
    "INCIDENT_TRIAGE",
    "INCIDENT_RCA",
    "INCIDENT_MITIGATION",
    "INCIDENT_UI",
    "PREVENTION",
    "RELEASE_READINESS_REVIEW",
    "RELEASE_TESTING",
)


def classified(classification: str) -> dict[str, str]:
    return {t: c for t, c in load_classification(CLASSIFICATION).items() if c == classification}


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
    return association_classifications(association)


def associate_read(client: Any, args: argparse.Namespace) -> None:
    configuration = mcp_configuration(classified(READ_ONLY))
    association = client.associate_service(
        agentSpaceId=args.agent_space_id, serviceId=args.service_id, configuration=configuration
    )["association"]
    emit({"associationId": association["associationId"], "tools": configuration["mcpserver"]["tools"]})


def enable_write(client: Any, args: argparse.Namespace) -> None:
    space_id, association_id = args.agent_space_id, args.association_id
    # Exactly the tools in mcp/classification.json, so a tool added by hand is removed too.
    classifications = {**classified(READ_ONLY), **classified(MUTATIVE)}
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
    wrong = [tool for tool in classified(MUTATIVE) if actual.get(tool) != MUTATIVE]
    if wrong:
        sys.exit(f"write tools not classified MUTATIVE after update: {wrong}")
    preferences = client.get_agent_space(agentSpaceId=space_id)["agentSpace"].get("preferences", {})
    if preferences.get(ELEVATED_PREFERENCE) is not True:
        sys.exit(f"{ELEVATED_PREFERENCE} is not enabled on the agent space")
    emit({"tools": actual, "preferences": preferences})


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    text: str


def parse_skill(path: Path) -> Skill:
    """Read a SKILL.md whose frontmatter has exactly a one-line name and description."""
    text = path.read_text()
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        sys.exit(f"{path}: must start with frontmatter between --- lines")
    fields: dict[str, str] = {}
    for line in lines[1 : lines.index("---", 1)]:
        key, sep, value = line.partition(": ")
        if not sep or key in fields or key not in ("name", "description") or not value.strip():
            sys.exit(f"{path}: frontmatter must be one-line 'name: ...' and 'description: ...', got {line!r}")
        fields[key] = value.strip()
    if set(fields) != {"name", "description"}:
        sys.exit(f"{path}: frontmatter needs name and description")
    name, description = fields["name"], fields["description"]
    if not SKILL_NAME.match(name) or name != path.parent.name:
        sys.exit(f"{path}: name must match {SKILL_NAME.pattern} and the directory name")
    if len(description) > SKILL_DESCRIPTION_MAX:
        sys.exit(f"{path}: description must be at most {SKILL_DESCRIPTION_MAX} characters")
    return Skill(name, description, text)


def load_skills(directory: Path) -> list[Skill]:
    skills = []
    for skill_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
        # Only SKILL.md is uploaded, so any other file would silently be missing.
        files = sorted(str(p.relative_to(skill_dir)) for p in skill_dir.rglob("*") if p.is_file())
        if files != [SKILL_PATH]:
            sys.exit(f"{skill_dir}: must contain only {SKILL_PATH}, found {files}")
        skills.append(parse_skill(skill_dir / SKILL_PATH))
    if not skills:
        sys.exit(f"no skills in {directory}")
    return skills


def load_agents_md(path: Path) -> str:
    text = path.read_text()
    if not text.startswith(AGENTS_MD_MARKER + "\n"):
        sys.exit(f"{path}: first line must be {AGENTS_MD_MARKER}")
    if len(text.encode()) > AGENTS_MD_MAX_BYTES:
        sys.exit(f"{path}: must be at most {AGENTS_MD_MAX_BYTES} bytes")
    return text


def list_assets(client: Any, space_id: str, asset_type: str) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"agentSpaceId": space_id, "assetType": asset_type}
    items: list[dict[str, Any]] = []
    while True:
        response = client.list_assets(**params)
        items.extend(response["items"])
        if not response.get("nextToken"):
            return items
        params["nextToken"] = response["nextToken"]


def file_content(text: str, path: str) -> dict[str, Any]:
    return {"file": {"path": path, "body": {"text": text}}}


def installed_from_repo(client: Any, space_id: str, asset_id: str) -> bool:
    try:
        file = client.get_asset_file(agentSpaceId=space_id, assetId=asset_id, path=AGENTS_MD_PATH)["file"]
    except client.exceptions.ResourceNotFoundException:
        return False
    return file["content"].get("text", "").startswith(AGENTS_MD_MARKER)


def single(matches: list[dict[str, Any]], what: str) -> dict[str, Any] | None:
    if len(matches) > 1:
        sys.exit(f"more than one asset for {what}: {[a['assetId'] for a in matches]}")
    return matches[0] if matches else None


def put_asset(
    client: Any,
    space_id: str,
    asset_type: str,
    existing: dict[str, Any] | None,
    metadata: dict[str, Any],
    content: dict[str, Any],
) -> dict[str, Any]:
    if existing is None:
        asset = client.create_asset(
            agentSpaceId=space_id,
            assetType=asset_type,
            metadata=metadata,
            content=content,
            clientToken=str(uuid.uuid4()),
        )["asset"]
        action = "created"
    else:
        # Metadata is merged, so a skill's status (ACTIVE or INACTIVE) set by an operator is kept.
        asset = client.update_asset(
            agentSpaceId=space_id, assetId=existing["assetId"], metadata=metadata, content=content
        )["asset"]
        action = "updated"
    return {"assetId": asset["assetId"], "version": asset["version"], "action": action}


def install_guidance(client: Any, args: argparse.Namespace) -> None:
    space_id, agent_type = args.agent_space_id, args.agent_type
    # Everything is read and validated before the first call.
    instructions = load_agents_md(AGENTS_MD)
    skills = load_skills(SKILLS_DIR)

    current = [
        a for a in list_assets(client, space_id, AGENTS_MD_TYPE) if a["metadata"].get("agent_type") == agent_type
    ]
    existing = single(current, f"agent type {agent_type}")
    # One AGENTS.md per agent type: updating someone else's would replace their instructions.
    if existing and not args.replace_instructions and not installed_from_repo(client, space_id, existing["assetId"]):
        sys.exit(
            f"the AGENTS.md for agent type {agent_type} ({existing['assetId']}) was not installed from this "
            "repository; merge AGENTS.md into it by hand, choose another --agent-type, "
            "or pass --replace-instructions to overwrite it"
        )
    payload: dict[str, Any] = {
        "agentsMd": {
            "agentType": agent_type,
            **put_asset(
                client,
                space_id,
                AGENTS_MD_TYPE,
                existing,
                {"agent_type": agent_type},
                file_content(instructions, AGENTS_MD_PATH),
            ),
        },
        "skills": [],
    }

    installed = list_assets(client, space_id, SKILL_TYPE)
    for skill in skills:
        existing = single([a for a in installed if a["metadata"].get("name") == skill.name], f"skill {skill.name}")
        # Skills load only when relevant, so every agent type gets them, whatever --agent-type says.
        metadata = {"name": skill.name, "description": skill.description, "agent_types": [GENERIC]}
        result = put_asset(client, space_id, SKILL_TYPE, existing, metadata, file_content(skill.text, SKILL_PATH))
        payload["skills"].append({"name": skill.name, **result})
    emit(payload)


def remove_guidance(client: Any, args: argparse.Namespace) -> None:
    space_id = args.agent_space_id
    removed = []
    for asset in list_assets(client, space_id, AGENTS_MD_TYPE):
        if installed_from_repo(client, space_id, asset["assetId"]):
            client.delete_asset(agentSpaceId=space_id, assetId=asset["assetId"])
            removed.append({"assetId": asset["assetId"], "agentType": asset["metadata"].get("agent_type")})
    names = skill_names()
    for asset in list_assets(client, space_id, SKILL_TYPE):
        if asset["metadata"].get("name") in names:
            client.delete_asset(agentSpaceId=space_id, assetId=asset["assetId"])
            removed.append({"assetId": asset["assetId"], "name": asset["metadata"]["name"]})
    emit({"removed": removed})


def skill_names() -> set[str]:
    return {p.parent.name for p in SKILLS_DIR.glob(f"*/{SKILL_PATH}")}


def guidance_status(client: Any, space_id: str) -> dict[str, Any]:
    names = skill_names()
    return {
        "agentsMd": [
            {
                "assetId": a["assetId"],
                "agentType": a["metadata"].get("agent_type"),
                "version": a["version"],
                "updatedAt": a["updatedAt"],
            }
            for a in list_assets(client, space_id, AGENTS_MD_TYPE)
        ],
        "skills": [
            {
                "assetId": a["assetId"],
                "name": a["metadata"]["name"],
                "status": a["metadata"].get("status"),
                "agentTypes": a["metadata"].get("agent_types"),
                "version": a["version"],
                "updatedAt": a["updatedAt"],
            }
            for a in list_assets(client, space_id, SKILL_TYPE)
            if a["metadata"].get("name") in names
        ],
    }


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
    payload["guidance"] = guidance_status(client, args.agent_space_id)
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

    p = sub.add_parser("install-guidance", help="create or update the AGENTS.md and skill assets")
    id_flag(p, "--agent-space-id", "AGENT_SPACE_ID")
    p.add_argument(
        "--agent-type", choices=AGENT_TYPES, default=GENERIC, help="agent type of the AGENTS.md; default GENERIC (every agent)"
    )
    p.add_argument(
        "--replace-instructions",
        action="store_true",
        help="overwrite an AGENTS.md for this agent type that was not installed from this repository",
    )
    p.set_defaults(func=install_guidance)

    p = sub.add_parser("remove-guidance", help="delete the assets installed by install-guidance")
    id_flag(p, "--agent-space-id", "AGENT_SPACE_ID")
    p.set_defaults(func=remove_guidance)

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
