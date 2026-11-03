"""install-guidance, remove-guidance and status against a stubbed devops-agent client.

Stubber validates every request and canned response against the botocore service model,
so the shapes here are the real ones.
"""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from botocore.stub import ANY, Stubber

REPO = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "devops_agent_register", REPO / "scripts" / "devops_agent_register.py"
)
register = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(register)

SPACE = "space-1"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
AGENTS_MD_TEXT = (REPO / "AGENTS.md").read_text()
SKILL = register.parse_skill(REPO / "agent/skills/kube-handrail-scale/SKILL.md")


def asset(asset_id: str, asset_type: str, metadata: dict, version: int = 1) -> dict:
    return {
        "assetId": asset_id,
        "assetType": asset_type,
        "metadata": metadata,
        "version": version,
        "createdAt": NOW,
        "updatedAt": NOW,
    }


def skill_metadata() -> dict:
    return {"name": SKILL.name, "description": SKILL.description, "agent_types": ["GENERIC"]}


def args(**kwargs):
    defaults = {"agent_space_id": SPACE, "agent_type": "GENERIC", "replace_instructions": False}
    return type("Args", (), {**defaults, **kwargs})()


@pytest.fixture
def stubbed():
    client = boto3.client(
        "devops-agent", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def expect_list(stubber, asset_type: str, items: list, next_token: str | None = None, after: str | None = None):
    params = {"agentSpaceId": SPACE, "assetType": asset_type}
    if after:
        params["nextToken"] = after
    response = {"items": items, **({"nextToken": next_token} if next_token else {})}
    stubber.add_response("list_assets", response, params)


def expect_agents_md_file(stubber, asset_id: str, text: str):
    stubber.add_response(
        "get_asset_file",
        {"file": {"path": "AGENTS.md", "content": {"text": text}, "version": 1, "createdAt": NOW, "updatedAt": NOW}},
        {"agentSpaceId": SPACE, "assetId": asset_id, "path": "AGENTS.md"},
    )


def agents_md_content() -> dict:
    return {"file": {"path": "AGENTS.md", "body": {"text": AGENTS_MD_TEXT}}}


def skill_content() -> dict:
    return {"file": {"path": "SKILL.md", "body": {"text": SKILL.text}}}


def test_repository_guidance_is_valid():
    assert register.load_agents_md(REPO / "AGENTS.md") == AGENTS_MD_TEXT
    assert [s.name for s in register.load_skills(REPO / "agent/skills")] == ["kube-handrail-scale"]
    assert SKILL.text.startswith("---\nname: kube-handrail-scale\n")
    assert 100 <= len(SKILL.description) <= register.SKILL_DESCRIPTION_MAX


def test_install_creates_when_absent(stubbed, capsys):
    client, stubber = stubbed
    expect_list(stubber, "agents_md", [])
    stubber.add_response(
        "create_asset",
        {"asset": asset("am-1", "agents_md", {"agent_type": "GENERIC"})},
        {
            "agentSpaceId": SPACE,
            "assetType": "agents_md",
            "metadata": {"agent_type": "GENERIC"},
            "content": agents_md_content(),
            "clientToken": ANY,
        },
    )
    expect_list(stubber, "skill", [])
    stubber.add_response(
        "create_asset",
        {"asset": asset("sk-1", "skill", skill_metadata())},
        {
            "agentSpaceId": SPACE,
            "assetType": "skill",
            "metadata": skill_metadata(),
            "content": skill_content(),
            "clientToken": ANY,
        },
    )
    register.install_guidance(client, args())
    out = json.loads(capsys.readouterr().out)
    assert out["agentsMd"] == {"agentType": "GENERIC", "assetId": "am-1", "version": 1, "action": "created"}
    assert out["skills"] == [{"name": SKILL.name, "assetId": "sk-1", "version": 1, "action": "created"}]


def test_install_updates_existing_across_pages(stubbed, capsys):
    client, stubber = stubbed
    other_type = asset("am-chat", "agents_md", {"agent_type": "CHAT"})
    ours = asset("am-1", "agents_md", {"agent_type": "GENERIC"})
    expect_list(stubber, "agents_md", [other_type], next_token="t1")
    expect_list(stubber, "agents_md", [ours], after="t1")
    expect_agents_md_file(stubber, "am-1", "<!-- kube-handrail:agents-md -->\n# older text\n")
    stubber.add_response(
        "update_asset",
        {"asset": asset("am-1", "agents_md", {"agent_type": "GENERIC"}, version=2)},
        {
            "agentSpaceId": SPACE,
            "assetId": "am-1",
            "metadata": {"agent_type": "GENERIC"},
            "content": agents_md_content(),
        },
    )
    unrelated = asset("sk-other", "skill", {"name": "rds-investigation", "agent_types": ["GENERIC"]})
    existing = asset("sk-1", "skill", {**skill_metadata(), "status": "INACTIVE"})
    expect_list(stubber, "skill", [unrelated, existing])
    stubber.add_response(
        "update_asset",
        {"asset": asset("sk-1", "skill", skill_metadata(), version=3)},
        {"agentSpaceId": SPACE, "assetId": "sk-1", "metadata": skill_metadata(), "content": skill_content()},
    )
    register.install_guidance(client, args())
    out = json.loads(capsys.readouterr().out)
    assert out["agentsMd"]["action"] == "updated" and out["agentsMd"]["version"] == 2
    assert out["skills"] == [{"name": SKILL.name, "assetId": "sk-1", "version": 3, "action": "updated"}]


def test_install_refuses_foreign_agents_md(stubbed):
    client, stubber = stubbed
    expect_list(stubber, "agents_md", [asset("am-9", "agents_md", {"agent_type": "GENERIC"})])
    expect_agents_md_file(stubber, "am-9", "# Our team's standing instructions\n")
    with pytest.raises(SystemExit, match="was not installed from this repository"):
        register.install_guidance(client, args())


def test_install_refuses_agents_md_without_file(stubbed):
    client, stubber = stubbed
    expect_list(stubber, "agents_md", [asset("am-9", "agents_md", {"agent_type": "GENERIC"})])
    stubber.add_client_error("get_asset_file", service_error_code="ResourceNotFoundException", http_status_code=404)
    with pytest.raises(SystemExit, match="was not installed from this repository"):
        register.install_guidance(client, args())


def test_install_replaces_foreign_agents_md_only_when_asked(stubbed, capsys):
    client, stubber = stubbed
    expect_list(stubber, "agents_md", [asset("am-9", "agents_md", {"agent_type": "CHAT"})])
    stubber.add_response(
        "update_asset",
        {"asset": asset("am-9", "agents_md", {"agent_type": "CHAT"}, version=4)},
        {"agentSpaceId": SPACE, "assetId": "am-9", "metadata": {"agent_type": "CHAT"}, "content": agents_md_content()},
    )
    expect_list(stubber, "skill", [])
    stubber.add_response(
        "create_asset",
        {"asset": asset("sk-1", "skill", skill_metadata())},
        {
            "agentSpaceId": SPACE,
            "assetType": "skill",
            # Skills are GENERIC whatever the agent type of the AGENTS.md.
            "metadata": skill_metadata(),
            "content": skill_content(),
            "clientToken": ANY,
        },
    )
    register.install_guidance(client, args(agent_type="CHAT", replace_instructions=True))
    assert json.loads(capsys.readouterr().out)["agentsMd"]["assetId"] == "am-9"


def test_install_fails_on_duplicate_skill(stubbed):
    client, stubber = stubbed
    expect_list(stubber, "agents_md", [])
    stubber.add_response(
        "create_asset", {"asset": asset("am-1", "agents_md", {"agent_type": "GENERIC"})}, None
    )
    expect_list(stubber, "skill", [asset("a", "skill", skill_metadata()), asset("b", "skill", skill_metadata())])
    with pytest.raises(SystemExit, match="more than one asset"):
        register.install_guidance(client, args())


def test_local_files_are_validated_before_any_call(tmp_path, monkeypatch, stubbed):
    client, _ = stubbed  # no responses queued: any call would fail
    bad = tmp_path / "AGENTS.md"
    bad.write_text("# no marker\n")
    monkeypatch.setattr(register, "AGENTS_MD", bad)
    with pytest.raises(SystemExit, match="first line must be"):
        register.install_guidance(client, args())


def write_skill(root: Path, dirname: str, text: str) -> Path:
    path = root / dirname / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    "text, message",
    [
        ("# no frontmatter\n", "must start with frontmatter"),
        ("---\nname: my-skill\n---\n", "needs name and description"),
        ("---\nname: my-skill\ndescription: d\nother: x\n---\n", "frontmatter must be one-line"),
        ("---\nname: my-skill\ndescription:\n  folded\n---\n", "frontmatter must be one-line"),
        ("---\nname: My_Skill\ndescription: d\n---\n", "must match"),
        ("---\nname: other-name\ndescription: d\n---\n", "must match"),
        ("---\nname: my-skill\ndescription: " + "x" * 1025 + "\n---\n", "at most 1024"),
    ],
)
def test_parse_skill_rejects(tmp_path, text, message):
    with pytest.raises(SystemExit, match=message):
        register.parse_skill(write_skill(tmp_path, "my-skill", text))


def test_load_skills_rejects_extra_files(tmp_path):
    write_skill(tmp_path, "my-skill", "---\nname: my-skill\ndescription: d\n---\n")
    (tmp_path / "my-skill" / "references").mkdir()
    (tmp_path / "my-skill" / "references" / "notes.md").write_text("x")
    with pytest.raises(SystemExit, match="must contain only SKILL.md"):
        register.load_skills(tmp_path)


def test_load_agents_md_rejects_oversize(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text(register.AGENTS_MD_MARKER + "\n" + "x" * register.AGENTS_MD_MAX_BYTES)
    with pytest.raises(SystemExit, match="at most"):
        register.load_agents_md(path)


def test_remove_deletes_only_repository_assets(stubbed, capsys):
    client, stubber = stubbed
    expect_list(
        stubber,
        "agents_md",
        [asset("am-1", "agents_md", {"agent_type": "GENERIC"}), asset("am-2", "agents_md", {"agent_type": "CHAT"})],
    )
    expect_agents_md_file(stubber, "am-1", AGENTS_MD_TEXT)
    stubber.add_response("delete_asset", {}, {"agentSpaceId": SPACE, "assetId": "am-1"})
    expect_agents_md_file(stubber, "am-2", "# someone else's\n")
    expect_list(
        stubber,
        "skill",
        [asset("sk-1", "skill", skill_metadata()), asset("sk-2", "skill", {"name": "rds-investigation"})],
    )
    stubber.add_response("delete_asset", {}, {"agentSpaceId": SPACE, "assetId": "sk-1"})
    register.remove_guidance(client, args())
    assert json.loads(capsys.readouterr().out)["removed"] == [
        {"assetId": "am-1", "agentType": "GENERIC"},
        {"assetId": "sk-1", "name": SKILL.name},
    ]


def test_status_lists_guidance(stubbed, capsys):
    client, stubber = stubbed
    stubber.add_response(
        "get_agent_space",
        {"agentSpace": {"agentSpaceId": SPACE, "name": "test", "createdAt": NOW, "updatedAt": NOW}},
        {"agentSpaceId": SPACE},
    )
    stubber.add_response("list_associations", {"associations": []}, {"agentSpaceId": SPACE})
    expect_list(stubber, "agents_md", [asset("am-1", "agents_md", {"agent_type": "GENERIC"})])
    expect_list(
        stubber,
        "skill",
        [
            asset("sk-1", "skill", {**skill_metadata(), "status": "ACTIVE"}, version=2),
            asset("sk-2", "skill", {"name": "rds-investigation"}),
        ],
    )
    register.status(client, args(association_id=None))
    guidance = json.loads(capsys.readouterr().out)["guidance"]
    assert guidance["agentsMd"] == [
        {"assetId": "am-1", "agentType": "GENERIC", "version": 1, "updatedAt": "2026-01-01 00:00:00+00:00"}
    ]
    assert guidance["skills"] == [
        {
            "assetId": "sk-1",
            "name": SKILL.name,
            "status": "ACTIVE",
            "agentTypes": ["GENERIC"],
            "version": 2,
            "updatedAt": "2026-01-01 00:00:00+00:00",
        }
    ]
