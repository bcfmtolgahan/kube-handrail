"""The agent guidance stays in sync with the code (scripts/check_contract.py,
check_guidance), and the guidance does not grow past its word-count ceilings."""

import importlib.util
import json
import math
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "check_contract", REPO / "scripts" / "check_contract.py"
)
check_contract = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_contract)

AGENTS = "AGENTS.md"
SCALE_SKILL = "agent/skills/kube-handrail-scale/SKILL.md"
ROLLOUT_SKILL = "agent/skills/kube-handrail-rollout/SKILL.md"

# Word counts (whitespace-separated) at the end of Milestone 3 plus 5 % slack. They stop the
# guidance from growing unnoticed; Milestone 7 lowers them to its targets. Raise one only
# with a reason in the commit message.
WORD_CEILINGS = {
    AGENTS: 2028,
    SCALE_SKILL: 1246,
    ROLLOUT_SKILL: 1671,
    "mcp/CONTRACT.md": 22012,
    "README.md": 3163,
    "docs/guards.md": 3270,
    "docs/deploy.md": 5799,
    "docs/roadmap.md": 6956,
    "docs/milestone-0.md": 4245,
    "docs/releasing.md": 677,
}
# Everything an agent reads: AGENTS.md and the skills (4708 words at Milestone 3).
AGENT_CONTEXT = (AGENTS, SCALE_SKILL, ROLLOUT_SKILL)
AGENT_CONTEXT_CEILING = math.ceil(4708 * 1.05)


def words(rel: str) -> int:
    return len((REPO / rel).read_text().split())


def test_repository_guidance_is_in_sync():
    assert check_contract.check_guidance(REPO) == []


@pytest.mark.parametrize(("rel", "ceiling"), sorted(WORD_CEILINGS.items()))
def test_word_ceiling(rel, ceiling):
    assert words(rel) <= ceiling, f"{rel} has {words(rel)} words; the ceiling is {ceiling}"


def test_agent_context_ceiling():
    assert sum(words(rel) for rel in AGENT_CONTEXT) <= AGENT_CONTEXT_CEILING


# --- The checks catch what they are for -------------------------------------


@pytest.fixture
def repo(tmp_path):
    """A copy of the files the guidance checks read, to break one at a time."""
    for rel in ["AGENTS.md", "README.md", "CHANGELOG.md", "mcp/CONTRACT.md", "terraform/main.tf"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, tmp_path / rel)
    for rel in ["docs", "agent/skills", "mcp/k8s_read", "mcp/k8s_write"]:
        shutil.copytree(REPO / rel, tmp_path / rel, ignore=shutil.ignore_patterns("*.py*"))
    common = tmp_path / "mcp/common/handrail_common"
    common.mkdir(parents=True)
    for name in ["envelope.py", "impact.py", "rollout.py"]:
        shutil.copy(REPO / "mcp/common/handrail_common" / name, common / name)
    return tmp_path


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def test_copy_is_in_sync(repo):
    assert check_contract.check_guidance(repo) == []


def test_tool_missing_from_the_table(repo):
    edit(repo / AGENTS, "| `k8sr___get_rollout_history` |", "| `k8sr___get_history` |")
    assert check_contract.check_agents_tables(repo) == [
        "AGENTS.md tool table does not list k8sr___get_rollout_history",
        "AGENTS.md tool table lists unknown k8sr___get_history",
    ]


def test_new_tool_not_in_the_table(repo):
    path = repo / "mcp/k8s_read/tools.json"
    tools = json.loads(path.read_text())
    tools.append({"name": "list_nodes", "inputSchema": {"type": "object", "properties": {}}})
    path.write_text(json.dumps(tools))
    assert check_contract.check_agents_tables(repo) == [
        "AGENTS.md tool table does not list k8sr___list_nodes"
    ]


def test_error_code_missing_from_the_table(repo):
    edit(repo / AGENTS, "| `cooldown` |", "| cooldown |")
    assert check_contract.check_agents_tables(repo) == [
        "AGENTS.md error table does not list cooldown"
    ]


def test_deprecated_error_code_in_the_table(repo):
    edit(
        repo / AGENTS,
        "| `object_budget_exceeded` |",
        "| `object_budget_exceeded`, `plan_rate_limited` |",
    )
    assert check_contract.check_agents_tables(repo) == [
        "AGENTS.md error table lists plan_rate_limited, which is deprecated or unknown"
    ]


def test_skill_names_an_unknown_tool(repo):
    edit(repo / SCALE_SKILL, "Call `get_workload_status(", "Call `get_deployment_status(")
    assert check_contract.check_skills(repo) == [
        f"{SCALE_SKILL}: names unknown tool get_deployment_status"
    ]


def test_skill_passes_an_unknown_argument(repo):
    edit(
        repo / SCALE_SKILL,
        "plan_scale_deployment(namespace, name, replicas)",
        "plan_scale_deployment(namespace, name, count)",
    )
    assert check_contract.check_skills(repo) == [
        f"{SCALE_SKILL}: plan_scale_deployment has no argument count"
    ]


def test_skill_misspells_an_argument(repo):
    edit(repo / SCALE_SKILL, "`revert_on_failure: true`", "`revert_on_failures: true`")
    assert check_contract.check_skills(repo) == [
        f"{SCALE_SKILL}: revert_on_failures looks like the argument revert_on_failure"
    ]


def test_skill_passes_an_unknown_keyword_argument(repo):
    edit(
        repo / SCALE_SKILL,
        "plan_scale_deployment(namespace, name, replicas)",
        "plan_scale_deployment(namespace=ns, name=name, count=3)",
    )
    assert check_contract.check_skills(repo) == [
        f"{SCALE_SKILL}: plan_scale_deployment has no argument count"
    ]


def test_skill_calls_an_unknown_tool(repo):
    edit(
        repo / SCALE_SKILL,
        "plan_scale_deployment(namespace, name, replicas)",
        "cancel_plan(plan_id)",
    )
    assert check_contract.check_skills(repo) == [f"{SCALE_SKILL}: calls unknown tool cancel_plan"]


def test_skill_sets_an_argument_the_tools_dropped(repo):
    for directory in ["k8s_read", "k8s_write"]:
        path = repo / "mcp" / directory / "tools.json"
        tools = json.loads(path.read_text())
        for tool in tools:
            tool.get("inputSchema", {}).get("properties", {}).pop("revert_on_failure", None)
        path.write_text(json.dumps(tools))
    errors = check_contract.check_skills(repo)
    assert f"{SCALE_SKILL}: sets revert_on_failure, which no tool takes" in errors
    assert f"{AGENTS}: sets revert_on_failure, which no tool takes" in errors


def test_a_response_field_set_in_prose_is_not_an_argument(repo, monkeypatch):
    assert "`notified: false`" in (repo / SCALE_SKILL).read_text()
    assert check_contract.check_skills(repo) == []
    monkeypatch.setattr(check_contract, "RESPONSE_FIELDS", frozenset())
    assert f"{SCALE_SKILL}: sets notified, which no tool takes" in check_contract.check_skills(repo)


def test_contract_error_codes_line_matches_the_envelope(repo):
    edit(repo / "mcp/CONTRACT.md", "`plan_used`, ", "")
    assert check_contract.check_contract_codes(repo) == [
        "mcp/CONTRACT.md 'Error codes:' line does not list plan_used"
    ]
    edit(repo / "mcp/CONTRACT.md", "`conflict`, ", "`conflict`, `made_up`, ")
    assert check_contract.check_contract_codes(repo) == [
        "mcp/CONTRACT.md 'Error codes:' line does not list plan_used",
        "mcp/CONTRACT.md 'Error codes:' line lists unknown made_up",
    ]


def test_warning_no_skill_names(repo):
    impact = repo / "mcp/common/handrail_common/impact.py"
    edit(impact, 'NO_PODS = "no_pods"', 'NO_PODS = "no_pods_left"')
    assert check_contract.check_skills(repo) == ["no skill names warning no_pods_left"]


def test_contract_anchor_that_does_not_resolve(repo):
    edit(repo / "README.md", "mcp/CONTRACT.md#drift-sentinel", "mcp/CONTRACT.md#drift-guard")
    assert check_contract.check_links(repo) == [
        "README.md: no heading for mcp/CONTRACT.md#drift-guard"
    ]


def test_link_to_a_missing_file(repo):
    (repo / "docs/deploy.md").unlink()
    errors = check_contract.check_links(repo)
    assert errors and all("link to missing" in e and "deploy.md#" in e for e in errors)


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        (
            "## Milestone 1: Server-verified write path (done)",
            "milestone-1-server-verified-write-path-done",
        ),
        ("### `plan_rate` and `GITOPS_MARKERS`", "plan_rate-and-gitops_markers"),
        ("## History checks (oscillation breaker)", "history-checks-oscillation-breaker"),
    ],
)
def test_github_slugs(heading, anchor):
    assert anchor in check_contract.anchors(heading)


def test_repeated_headings_get_numbered_anchors():
    assert check_contract.anchors("## Notes\n\n## Notes\n") == {"notes", "notes-1"}
