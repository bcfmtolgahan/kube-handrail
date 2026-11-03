import importlib.util
import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "check_contract", REPO / "scripts" / "check_contract.py"
)
check_contract = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_contract)


@pytest.fixture
def repo(tmp_path):
    """A copy of the files the check reads, to break one at a time."""
    for rel in [
        "mcp/classification.json",
        "mcp/k8s_read/tools.json",
        "mcp/k8s_write/tools.json",
        "terraform/main.tf",
        "mcp/CONTRACT.md",
        "AGENTS.md",
    ]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, tmp_path / rel)
    return tmp_path


def edit_json(path: Path, change) -> None:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def add_tool(name: str):
    return lambda tools: tools.append(
        {"name": name, "description": "", "inputSchema": {"type": "object", "properties": {}}}
    )


def test_repository_passes():
    assert check_contract.check(REPO) == []


def test_terraform_targets_parsed():
    main_tf = (REPO / "terraform" / "main.tf").read_text()
    assert check_contract.terraform_targets(main_tf) == {"k8sr": "k8s_read", "k8sw": "k8s_write"}


def test_write_tool_marked_read_only_fails(repo):
    edit_json(
        repo / "mcp/classification.json",
        lambda c: c.update({"k8sw___apply_scale_deployment": "READ_ONLY"}),
    )
    assert check_contract.check(repo) == [
        "k8sw___apply_scale_deployment: write tool must be MUTATIVE, not READ_ONLY"
    ]


def test_unclassified_tool_fails(repo):
    edit_json(repo / "mcp/k8s_read/tools.json", add_tool("list_nodes"))
    assert check_contract.check(repo) == [
        "k8sr___list_nodes: no classification in mcp/classification.json"
    ]


def test_classified_tool_that_does_not_exist_fails(repo):
    edit_json(repo / "mcp/classification.json", lambda c: c.update({"k8sr___gone": "READ_ONLY"}))
    assert check_contract.check(repo) == [
        "k8sr___gone: classified but not in mcp/k8s_read/tools.json"
    ]


def test_unknown_classification_value_fails(repo):
    edit_json(
        repo / "mcp/classification.json",
        lambda c: c.update({"k8sr___get_workload_status": "DESTRUCTIVE"}),
    )
    [error] = check_contract.check(repo)
    assert error.startswith("k8sr___get_workload_status: classification must be one of")


@pytest.mark.parametrize("name", ["Apply", "apply-scale", "x" * 60])
def test_invalid_full_name_fails(repo, name):
    edit_json(repo / "mcp/k8s_write/tools.json", add_tool(name))
    edit_json(repo / "mcp/classification.json", lambda c: c.update({f"k8sw___{name}": "MUTATIVE"}))
    assert check_contract.check(repo) == [
        f"k8sw___{name}: must be at most 64 characters of [a-z0-9_]"
    ]


def test_terraform_target_name_mismatch_fails(repo):
    main_tf = repo / "terraform/main.tf"
    main_tf.write_text(main_tf.read_text().replace('target      = "k8sw"', 'target      = "k8sx"'))
    errors = check_contract.check(repo)
    assert "terraform/main.tf must load mcp/k8s_write/tools.json as target k8sw" in errors
    assert "k8sw___apply_scale_deployment: target k8sw is not loaded by terraform/main.tf" in errors


def test_tools_json_not_loaded_by_terraform_fails(repo):
    main_tf = repo / "terraform/main.tf"
    main_tf.write_text(
        main_tf.read_text().replace("mcp/k8s_read/tools.json", "mcp/other/tools.json")
    )
    errors = check_contract.check(repo)
    assert "terraform/main.tf does not load mcp/k8s_read/tools.json" in errors


def test_apply_tool_on_the_read_target_must_be_mutative(repo):
    edit_json(repo / "mcp/k8s_read/tools.json", add_tool("apply_restart"))
    edit_json(
        repo / "mcp/classification.json", lambda c: c.update({"k8sr___apply_restart": "READ_ONLY"})
    )
    assert check_contract.check(repo) == [
        "k8sr___apply_restart: write tool must be MUTATIVE, not READ_ONLY"
    ]


def test_agents_md_missing_error_code_fails(repo):
    agents_md = repo / "AGENTS.md"
    agents_md.write_text(agents_md.read_text().replace("`flapping`", "flapping"))
    assert check_contract.check(repo) == ["AGENTS.md does not mention flapping"]


def test_contract_error_codes_match_envelope():
    from handrail_common.envelope import ERROR_CODES

    codes = check_contract.contract_error_codes((REPO / "mcp/CONTRACT.md").read_text())
    assert sorted(codes) == sorted(ERROR_CODES)


def test_agents_md_absent_fails_without_traceback(repo):
    (repo / "AGENTS.md").unlink()
    assert "AGENTS.md is missing" in check_contract.check(repo)
