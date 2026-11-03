#!/usr/bin/env python3
"""Check that tool schemas, mcp/classification.json and Terraform agree.

Fails if a tool has no classification, the classification names a tool that does
not exist, a write tool is not MUTATIVE, a full tool name is invalid,
terraform/main.tf does not load each target's tools.json under that target name,
or AGENTS.md does not name every error code listed in mcp/CONTRACT.md.
Standard library only, so it runs before any dependency is installed.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DELIMITER = "___"
WRITE_TARGET = "k8sw"
# An apply_* tool is a write tool wherever it is placed.
WRITE_TOOL_PREFIX = "apply_"
WRITE_DIR = "k8s_write"
CLASSIFICATIONS = {"READ_ONLY", "MUTATIVE"}
NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
MAX_NAME_LENGTH = 64
ERROR_CODES_LINE = re.compile(r"^Error codes: (.*)$", re.MULTILINE)
CODE = re.compile(r"`([a-z_]+)`")

# An entry of the `functions` map in terraform/main.tf looks like
#   k8s_read = {
#     target = "k8sr"
#     tools  = jsondecode(file("${path.module}/../mcp/k8s_read/tools.json"))
#     ...
#   }
BLOCK_START = re.compile(r"^\s*(\w+)\s*=\s*\{\s*$")
TARGET = re.compile(r"^\s*target\s*=\s*\"([^\"]*)\"\s*$", re.MULTILINE)
TOOLS = re.compile(
    r"^\s*tools\s*=\s*jsondecode\(file\(\"[^\"]*/mcp/(\w+)/tools\.json\"\)\)\s*$", re.MULTILINE
)


def terraform_targets(main_tf: str) -> dict[str, str]:
    """Target name => function directory, for each map entry keyed by its own directory."""
    lines = main_tf.splitlines()
    targets: dict[str, str] = {}
    for i, line in enumerate(lines):
        start = BLOCK_START.match(line)
        if not start:
            continue
        body = []
        for inner in lines[i + 1 :]:
            if inner.strip() == "}" or BLOCK_START.match(inner):
                break
            body.append(inner)
        text = "\n".join(body)
        target, tools = TARGET.search(text), TOOLS.search(text)
        if target and tools and tools.group(1) == start.group(1):
            targets[target.group(1)] = tools.group(1)
    return targets


def schema_tools(root: Path, directory: str) -> list[str]:
    return [
        tool["name"] for tool in json.loads((root / "mcp" / directory / "tools.json").read_text())
    ]


def contract_error_codes(contract: str) -> list[str]:
    line = ERROR_CODES_LINE.search(contract)
    return CODE.findall(line.group(1)) if line else []


def check_agents_md(root: Path) -> list[str]:
    """AGENTS.md tells agents what to do on each error, so it must not miss one."""
    path = root / "AGENTS.md"
    if not path.is_file():
        return ["AGENTS.md is missing"]
    agents_md = path.read_text()
    codes = contract_error_codes((root / "mcp/CONTRACT.md").read_text())
    if not codes:
        return ["mcp/CONTRACT.md has no 'Error codes:' line"]
    named = set(CODE.findall(agents_md))
    return [f"AGENTS.md does not mention {code}" for code in codes if code not in named]


def check(root: Path) -> list[str]:
    errors: list[str] = []
    classification = json.loads((root / "mcp/classification.json").read_text())
    targets = terraform_targets((root / "terraform/main.tf").read_text())

    for directory in sorted(p.parent.name for p in (root / "mcp").glob("*/tools.json")):
        if directory not in targets.values():
            errors.append(f"terraform/main.tf does not load mcp/{directory}/tools.json")

    if targets.get(WRITE_TARGET) != WRITE_DIR:
        errors.append(
            f"terraform/main.tf must load mcp/{WRITE_DIR}/tools.json as target {WRITE_TARGET}"
        )

    tools: set[str] = set()
    for target, directory in sorted(targets.items()):
        for name in schema_tools(root, directory):
            full = f"{target}{DELIMITER}{name}"
            tools.add(full)
            if len(full) > MAX_NAME_LENGTH or not NAME_PATTERN.match(full):
                errors.append(f"{full}: must be at most {MAX_NAME_LENGTH} characters of [a-z0-9_]")
            if full not in classification:
                errors.append(f"{full}: no classification in mcp/classification.json")
            elif (target == WRITE_TARGET or name.startswith(WRITE_TOOL_PREFIX)) and classification[
                full
            ] != "MUTATIVE":
                errors.append(f"{full}: write tool must be MUTATIVE, not {classification[full]}")

    for full, value in sorted(classification.items()):
        if value not in CLASSIFICATIONS:
            errors.append(f"{full}: classification must be one of {sorted(CLASSIFICATIONS)}")
        target = full.split(DELIMITER, 1)[0]
        if target not in targets:
            errors.append(f"{full}: target {target} is not loaded by terraform/main.tf")
        elif full not in tools:
            errors.append(f"{full}: classified but not in mcp/{targets[target]}/tools.json")
    return errors + check_agents_md(root)


def main() -> None:
    errors = check(REPO_ROOT)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        sys.exit(1)
    print("contract check passed")


if __name__ == "__main__":
    main()
