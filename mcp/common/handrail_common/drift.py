"""Tool classification (mcp/classification.json) and the drift comparisons built on it.

Shared by the drift sentinel and scripts/devops_agent_register.py. Pure functions only:
callers make the API calls and pass the responses in.
"""

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from handrail_common.dispatch import TARGET_DELIMITER

READ_ONLY = "READ_ONLY"
MUTATIVE = "MUTATIVE"
# DevOps Agent never invokes DESTRUCTIVE tools, so no kube-handrail tool uses it.
CLASSIFICATIONS = frozenset({READ_ONLY, MUTATIVE})
ELEVATED_PREFERENCE = "elevatedActionsEnabled"
# Every tool of the write target, and every apply_* tool wherever it is, must be MUTATIVE.
WRITE_TARGET = "k8sw"
WRITE_TOOL_PREFIX = "apply_"
READY = "READY"

Finding = dict[str, Any]


class UnexpectedResponse(Exception):
    """A response that cannot be read. Reported as unknown, never as drift."""


def parse_classification(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("classification must be a non-empty JSON object")
    for tool, value in raw.items():
        if TARGET_DELIMITER not in tool:
            raise ValueError(f"tool name must be <target>___<tool>: {tool!r}")
        if value not in CLASSIFICATIONS:
            raise ValueError(f"{tool}: classification must be one of {sorted(CLASSIFICATIONS)}")
        if is_write_tool(tool) and value != MUTATIVE:
            raise ValueError(f"{tool}: a write tool must be {MUTATIVE}")
    return dict(raw)


def is_write_tool(tool: str) -> bool:
    target, _, name = tool.partition(TARGET_DELIMITER)
    return target == WRITE_TARGET or name.startswith(WRITE_TOOL_PREFIX)


def load_classification(path: Path) -> dict[str, str]:
    return parse_classification(json.loads(path.read_text()))


def target_of(tool: str) -> str:
    return tool.split(TARGET_DELIMITER, 1)[0]


def tools_by_target(classification: Mapping[str, str]) -> dict[str, set[str]]:
    targets: dict[str, set[str]] = {}
    for tool in classification:
        targets.setdefault(target_of(tool), set()).add(tool)
    return targets


def association_classifications(response: Mapping[str, Any]) -> dict[str, str]:
    """Classification per associated tool, from a GetAssociation response.

    A tool without a toolDetails entry counts as READ_ONLY, as it does in DevOps Agent.
    """
    try:
        config = response["association"]["configuration"].get("mcpserver", {})
        details = {
            d["name"]: d.get("toolClassification", READ_ONLY) for d in config.get("toolDetails", [])
        }
        return {tool: details.get(tool, READ_ONLY) for tool in config.get("tools", [])}
    except (KeyError, TypeError, AttributeError) as exc:
        raise UnexpectedResponse("unreadable GetAssociation response") from exc


def elevated_actions(response: Mapping[str, Any]) -> bool:
    """elevatedActionsEnabled from a GetAgentSpace response; absent counts as off."""
    try:
        value = response["agentSpace"].get("preferences", {}).get(ELEVATED_PREFERENCE, False)
    except (KeyError, TypeError, AttributeError) as exc:
        raise UnexpectedResponse("unreadable GetAgentSpace response") from exc
    if not isinstance(value, bool):
        raise UnexpectedResponse(f"{ELEVATED_PREFERENCE} is not a boolean")
    return value


def gateway_target_tools(response: Mapping[str, Any]) -> set[str]:
    """Full tool names served by one target, from a GetGatewayTarget response."""
    try:
        name = response["name"]
        status = response["status"]
        payload = response["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["inlinePayload"]
        tools = {f"{name}{TARGET_DELIMITER}{tool['name']}" for tool in payload}
    except (KeyError, TypeError) as exc:
        raise UnexpectedResponse("unreadable GetGatewayTarget response") from exc
    # A target that is being created or updated may not serve the schema it reports yet.
    if status != READY:
        raise UnexpectedResponse(f"gateway target {name} is {status}")
    return tools


def finding(check: str, issue: str, subject: str, **extra: Any) -> Finding:
    return {"check": check, "issue": issue, "subject": subject, **extra}


def compare_gateway(
    expected: Mapping[str, set[str]],
    actual: Mapping[str, set[str]],
    other_targets: Iterable[str] = (),
) -> list[Finding]:
    """Compare full tool names per target; `other_targets` are gateway targets nobody expects."""
    findings = [finding("gateway", "extra_target", t) for t in sorted(other_targets)]
    for target in sorted(expected):
        if target not in actual:
            findings.append(finding("gateway", "missing_target", target))
            continue
        missing = sorted(expected[target] - actual[target])
        extra = sorted(actual[target] - expected[target])
        findings += [finding("gateway", "missing_tool", t) for t in missing]
        findings += [finding("gateway", "extra_tool", t) for t in extra]
    return findings


def compare_association(expected: Mapping[str, str], actual: Mapping[str, str]) -> list[Finding]:
    """The associated tools must be exactly the expected ones, and every write tool MUTATIVE."""
    findings = [
        finding("association", "extra_tool", t, actual=actual[t])
        for t in sorted(set(actual) - set(expected))
    ]
    for tool in sorted(expected):
        if tool not in actual:
            findings.append(finding("association", "missing_tool", tool))
        elif expected[tool] == MUTATIVE and actual[tool] != MUTATIVE:
            findings.append(finding("association", "not_mutative", tool, actual=actual[tool]))
    return findings


def compare_elevated(expected: bool, actual: bool) -> list[Finding]:
    if expected == actual:
        return []
    return [
        finding(
            "agent_space", "elevated_actions", ELEVATED_PREFERENCE, expected=expected, actual=actual
        )
    ]
