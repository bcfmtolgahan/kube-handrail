#!/usr/bin/env python3
"""Check that tool schemas, mcp/classification.json and Terraform agree.

Fails if a tool has no classification, the classification names a tool that does
not exist, a write tool is not MUTATIVE, a full tool name is invalid, a plan_<x>
tool has no apply_<x> tool or the reverse, terraform/main.tf does not load each
target's tools.json under that target name, a target has the reserved name of the
internal handlers or a tools.json names one of them, or AGENTS.md does not name
every error code listed in mcp/CONTRACT.md except the deprecated ones, which it must
not name.

The guidance checks (check_guidance) keep the agent guidance in sync with the code: the
AGENTS.md tool table lists exactly the served tools, its error table exactly the error
codes minus the deprecated ones, every tool the skills name exists and every argument
they pass in a call exists for that tool, every impact warning code appears in a skill,
and every relative Markdown link with an anchor resolves to a heading.
Standard library only, so it runs before any dependency is installed.
"""

import difflib
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DELIMITER = "___"
WRITE_TARGET = "k8sw"
# An apply_* tool is a write tool wherever it is placed.
WRITE_TOOL_PREFIX = "apply_"
# Every action is planned by plan_<action> and applied by apply_<action>.
PLAN_TOOL_PREFIX = "plan_"
WRITE_DIR = "k8s_write"
# Internal handlers run only as kh_internal___<name> on the verifier channel (mcp/CONTRACT.md,
# Dispatch). Must match INTERNAL_TARGET in handrail_common/dispatch.py and the keys of
# INTERNAL_HANDLERS in mcp/k8s_write/handler.py.
INTERNAL_TARGET = "kh_internal"
INTERNAL_TOOLS = frozenset({"verify_applied_plan"})
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


def envelope_module(root: Path):
    """handrail_common/envelope.py, loaded by path: it imports only the standard library."""
    path = root / "mcp/common/handrail_common/envelope.py"
    spec = importlib.util.spec_from_file_location("_kh_envelope", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deprecated_error_codes(root: Path) -> frozenset[str]:
    return envelope_module(root).DEPRECATED_ERROR_CODES


def check_agents_md(root: Path) -> list[str]:
    """AGENTS.md tells agents what to do on each error, so it must not miss one. A
    deprecated code is never returned, so it is left out to keep the agent's context small."""
    path = root / "AGENTS.md"
    if not path.is_file():
        return ["AGENTS.md is missing"]
    agents_md = path.read_text()
    codes = contract_error_codes((root / "mcp/CONTRACT.md").read_text())
    if not codes:
        return ["mcp/CONTRACT.md has no 'Error codes:' line"]
    deprecated = deprecated_error_codes(root)
    named = set(CODE.findall(agents_md))
    return [
        f"AGENTS.md does not mention {code}"
        for code in codes
        if code not in named and code not in deprecated
    ] + [f"AGENTS.md mentions deprecated {code}" for code in sorted(deprecated & named)]


def check_pairs(names: set[str]) -> list[str]:
    """A plan nothing can apply, or an apply nothing can plan, is a contract error."""
    plans = {n.removeprefix(PLAN_TOOL_PREFIX) for n in names if n.startswith(PLAN_TOOL_PREFIX)}
    applies = {n.removeprefix(WRITE_TOOL_PREFIX) for n in names if n.startswith(WRITE_TOOL_PREFIX)}
    return [
        f"{PLAN_TOOL_PREFIX}{a}: no {WRITE_TOOL_PREFIX}{a} tool" for a in sorted(plans - applies)
    ] + [f"{WRITE_TOOL_PREFIX}{a}: no {PLAN_TOOL_PREFIX}{a} tool" for a in sorted(applies - plans)]


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

    if INTERNAL_TARGET in targets:
        errors.append(f"terraform/main.tf: {INTERNAL_TARGET} is reserved for internal handlers")

    tools: set[str] = set()
    names: set[str] = set()
    for directory in sorted(p.parent.name for p in (root / "mcp").glob("*/tools.json")):
        for name in sorted(INTERNAL_TOOLS & set(schema_tools(root, directory))):
            errors.append(f"mcp/{directory}/tools.json: {name} is an internal handler")
    for target, directory in sorted(targets.items()):
        for name in schema_tools(root, directory):
            full = f"{target}{DELIMITER}{name}"
            tools.add(full)
            names.add(name)
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
    return errors + check_pairs(names) + check_agents_md(root)


# --- Guidance ----------------------------------------------------------------

TABLE_ROW = re.compile(r"^\|\s*([^|]*?)\s*\|", re.MULTILINE)
SECTION = re.compile(r"^## (.+)$", re.MULTILINE)
# A tool named in prose, with or without its target prefix: plan_/apply_ actions on a
# Deployment, get_ reads and request_ calls.
TOOL_MENTION = re.compile(
    r"\b(?:k8s[rw]___)?((?:plan|apply)_[a-z]+(?:_[a-z]+)*_deployment|get_[a-z]+(?:_[a-z]+)*"
    r"|request_[a-z]+(?:_[a-z]+)*)\b"
)
# A call written as tool(arg, ...) or tool(arg=value, ...).
TOOL_CALL = re.compile(r"\b(?:k8s[rw]___)?([a-z]+(?:_[a-z]+)+)\(([^()\n`]*)\)")
IDENTIFIER = re.compile(r"[a-z_]+")
# `name: true` or `name: false` sets an argument, unless name is one of these response fields.
SETTING = re.compile(r"`([a-z]+(?:_[a-z]+)*): (?:true|false)`")
RESPONSE_FIELDS = frozenset({"notified"})
# Python modules whose "code": CONSTANT entries are the impact warnings.
WARNING_MODULES = ("impact.py", "rollout.py")
WARNING_CODE = re.compile(r"[\"']code[\"']:\s*([A-Z_]+)")
LINK = re.compile(r"\]\(([^)\s]*?)#([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
GUIDANCE_DOCS = ("AGENTS.md", "README.md", "CHANGELOG.md")
CODE_OR_TOOL = re.compile(r"`([a-z0-9_]+)`")


def section(text: str, title: str) -> str:
    """The body of the `## title` section, up to the next `## ` heading."""
    matches = list(SECTION.finditer(text))
    for i, match in enumerate(matches):
        if match.group(1).strip() == title:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            return text[match.end() : end]
    return ""


def table_codes(body: str) -> list[str]:
    """Backticked names in the first cell of each table row, header and rule excluded."""
    names: list[str] = []
    for cell in TABLE_ROW.findall(body):
        names.extend(CODE_OR_TOOL.findall(cell))
    return names


def served_tools(root: Path) -> dict[str, dict]:
    """Full tool name => tool schema, for every target terraform/main.tf loads."""
    targets = terraform_targets((root / "terraform/main.tf").read_text())
    tools: dict[str, dict] = {}
    for target, directory in targets.items():
        for tool in json.loads((root / "mcp" / directory / "tools.json").read_text()):
            tools[f"{target}{DELIMITER}{tool['name']}"] = tool
    return tools


def check_agents_tables(root: Path) -> list[str]:
    agents_md = (root / "AGENTS.md").read_text()
    errors: list[str] = []
    listed = table_codes(section(agents_md, "Tools"))
    served = set(served_tools(root))
    errors += [f"AGENTS.md tool table does not list {n}" for n in sorted(served - set(listed))]
    errors += [f"AGENTS.md tool table lists unknown {n}" for n in sorted(set(listed) - served)]
    errors += [f"AGENTS.md tool table lists {n} twice" for n in sorted(_twice(listed))]
    module = envelope_module(root)
    expected = set(module.ERROR_CODES) - set(module.DEPRECATED_ERROR_CODES)
    codes = table_codes(section(agents_md, "Errors"))
    errors += [f"AGENTS.md error table does not list {c}" for c in sorted(expected - set(codes))]
    errors += [
        f"AGENTS.md error table lists {c}, which is deprecated or unknown"
        for c in sorted(set(codes) - expected)
    ]
    errors += [f"AGENTS.md error table lists {c} twice" for c in sorted(_twice(codes))]
    return errors


def _twice(names: list[str]) -> set[str]:
    return {n for n in names if names.count(n) > 1}


def warning_codes(root: Path) -> set[str]:
    codes: set[str] = set()
    for name in WARNING_MODULES:
        source = (root / "mcp/common/handrail_common" / name).read_text()
        constants = dict(re.findall(r"^([A-Z_]+) = \"([a-z_]+)\"$", source, re.MULTILINE))
        codes |= {constants[c] for c in WARNING_CODE.findall(source) if c in constants}
    return codes


def check_skills(root: Path) -> list[str]:
    tools = {full.split(DELIMITER, 1)[1]: schema for full, schema in served_tools(root).items()}
    errors: list[str] = []
    skills = sorted((root / "agent/skills").glob("*/SKILL.md"))
    texts = {path.relative_to(root).as_posix(): path.read_text() for path in skills}
    for rel, text in texts.items():
        unknown = set(TOOL_MENTION.findall(text)) - set(tools)
        errors += [f"{rel}: names unknown tool {name}" for name in sorted(unknown)]
        calls = TOOL_CALL.findall(text)
        # A call to a name outside the TOOL_MENTION patterns (cancel_plan(plan_id)).
        for name in sorted({name for name, _ in calls} - set(tools) - unknown):
            errors.append(f"{rel}: calls unknown tool {name}")
        for name, raw in calls:
            if name not in tools:
                continue
            properties = tools[name].get("inputSchema", {}).get("properties", {})
            for arg in (a.split("=", 1)[0].strip() for a in raw.split(",")):
                if IDENTIFIER.fullmatch(arg) and arg not in properties:
                    errors.append(f"{rel}: {name} has no argument {arg}")
    named = set(re.findall(r"`([a-z_]+)`", "\n".join(texts.values())))
    errors += [f"no skill names warning {c}" for c in sorted(warning_codes(root) - named)]
    # An argument named outside a call (`revert_on_failure: true`) cannot be told apart from
    # a response field, so only near misses of an argument name are reported, as typos.
    arguments = {a for s in tools.values() for a in s.get("inputSchema", {}).get("properties", {})}
    texts["AGENTS.md"] = (root / "AGENTS.md").read_text()
    for rel, text in texts.items():
        typos = set()
        for token in sorted(set(re.findall(r"`([a-z]+(?:_[a-z]+)+)\b", text)) - arguments):
            close = difflib.get_close_matches(token, arguments, n=1, cutoff=0.85)
            if close:
                typos.add(token)
                errors.append(f"{rel}: {token} looks like the argument {close[0]}")
        for token in sorted(set(SETTING.findall(text)) - arguments - RESPONSE_FIELDS - typos):
            errors.append(f"{rel}: sets {token}, which no tool takes")
    return errors


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation removed, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(text: str) -> set[str]:
    seen: dict[str, int] = {}
    result: set[str] = set()
    for heading in HEADING.findall(FENCE.sub("", text)):
        base = slug(heading)
        count = seen.get(base, 0)
        seen[base] = count + 1
        result.add(base if count == 0 else f"{base}-{count}")
    return result


def guidance_files(root: Path) -> list[Path]:
    files = [root / name for name in GUIDANCE_DOCS if (root / name).is_file()]
    files += sorted((root / "docs").rglob("*.md")) + sorted((root / "agent/skills").glob("*/*.md"))
    return files + [root / "mcp/CONTRACT.md"]


def check_links(root: Path) -> list[str]:
    """Every relative Markdown link with an anchor, including same-file ones, resolves."""
    errors: list[str] = []
    cache: dict[Path, set[str]] = {}
    for path in guidance_files(root):
        text = FENCE.sub("", path.read_text())
        for target, anchor in LINK.findall(text):
            if "://" in target or target.startswith("mailto:"):
                continue
            file = (path.parent / target).resolve() if target else path
            rel = path.relative_to(root).as_posix()
            if not file.is_file():
                errors.append(f"{rel}: link to missing {target}#{anchor}")
                continue
            if file not in cache:
                cache[file] = anchors(file.read_text())
            if anchor not in cache[file]:
                errors.append(f"{rel}: no heading for {target}#{anchor}")
    return errors


def check_contract_codes(root: Path) -> list[str]:
    """The CONTRACT.md 'Error codes:' line lists exactly ERROR_CODES, deprecated ones included."""
    codes = contract_error_codes((root / "mcp/CONTRACT.md").read_text())
    expected = set(envelope_module(root).ERROR_CODES)
    prefix = "mcp/CONTRACT.md 'Error codes:' line"
    return [f"{prefix} does not list {c}" for c in sorted(expected - set(codes))] + [
        f"{prefix} lists unknown {c}" for c in sorted(set(codes) - expected)
    ]


def check_guidance(root: Path) -> list[str]:
    return (
        check_agents_tables(root)
        + check_contract_codes(root)
        + check_skills(root)
        + check_links(root)
    )


def main() -> None:
    errors = check(REPO_ROOT) + check_guidance(REPO_ROOT)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        sys.exit(1)
    print("contract check passed")


if __name__ == "__main__":
    main()
