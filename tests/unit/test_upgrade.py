"""scripts/upgrade.py: the plan safety rules, kill switch ownership, resume and the apply
retry, with Terraform, SSM, Lambda and DevOps Agent replaced by fakes."""

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "upgrade"

_SPEC = importlib.util.spec_from_file_location("upgrade_script", ROOT / "scripts" / "upgrade.py")
upgrade = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(upgrade)

PARAM = "/kh-test/remediation-enabled"
INCONSISTENT = (
    "Error: Provider produced inconsistent final plan\n\nWhen expanding the plan for "
    'aws_lambda_function.approver to include new values learned so far during apply, provider "'
    'registry.terraform.io/hashicorp/aws" produced an invalid new value for .environment'
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


# --- Plan safety rules ------------------------------------------------------------------


def test_the_switch_removal_plan_is_moves_and_updates_only():
    report = upgrade.check_plan(fixture("plan_switch_removal"))
    assert report.violations == []
    assert report.migrations == [] and report.targets == []
    assert len(report.moves) == 12
    assert "aws_lambda_function.interceptor[0] -> aws_lambda_function.interceptor" in report.moves
    assert "terraform_data.caller_key[0] -> terraform_data.caller_key" in report.moves
    # Nothing but the build triggers is replaced, and nothing is created or destroyed.
    assert set(report.changes) == {"delete+create", "update"}
    assert all(a.startswith("terraform_data.build[") for a in report.changes["delete+create"])
    assert report.tool_set_in_plan is False


def test_a_destructive_plan_is_refused():
    report = upgrade.check_plan(fixture("plan_destructive"))
    assert report.violations == [
        "aws_bedrockagentcore_gateway.this: delete/create",
        "aws_dynamodb_table.plans: delete",
        'aws_ssm_parameter.agent_enabled["devops_agent"]: replace (would reset the switch to true)',
    ]


@pytest.mark.parametrize(
    ("kind", "name", "actions", "refused"),
    [
        ("aws_bedrockagentcore_gateway_target", "this", ["delete"], True),
        ("aws_cognito_user_pool", "this", ["delete", "create"], True),
        ("aws_cognito_user_pool_domain", "this", ["create", "delete"], True),
        ("aws_cognito_user_pool_client", "this", ["delete"], True),
        ("aws_s3_bucket", "audit", ["delete"], True),
        ("aws_ssm_parameter", "kill_switch", ["forget"], True),
        ("aws_ssm_parameter", "freezes", ["delete", "create"], True),
        # An agent removed from var.agents loses its switch: intended.
        ("aws_ssm_parameter", "agent_enabled", ["delete"], False),
        ("aws_dynamodb_table", "plans", ["update"], False),
        ("aws_dynamodb_table", "other", ["delete"], False),
        ("aws_lambda_function", "this", ["delete", "create"], False),
        ("aws_bedrockagentcore_gateway", "this", ["update"], False),
    ],
)
def test_protected_resources(kind, name, actions, refused):
    change = {
        "address": f"{kind}.{name}",
        "mode": "managed",
        "type": kind,
        "name": name,
        "change": {"actions": actions, "before": {}, "after": {}},
    }
    report = upgrade.check_plan({"resource_changes": [change]})
    assert bool(report.violations) is refused


def test_data_sources_are_ignored():
    change = {
        "address": "data.aws_eks_cluster.this",
        "mode": "data",
        "type": "aws_dynamodb_table",
        "name": "plans",
        "change": {"actions": ["delete"]},
    }
    assert upgrade.check_plan({"resource_changes": [change]}).violations == []


def test_the_legacy_migration_is_detected_with_its_targets():
    report = upgrade.check_plan(fixture("plan_legacy_migration"))
    assert report.violations == []
    assert [m.name for m in report.migrations] == ["interceptor-and-verifier"]
    assert report.targets == [
        "terraform_data.caller_key",
        "aws_iam_role.interceptor",
        "aws_lambda_function.interceptor",
        "aws_iam_role.verifier",
    ]


def test_a_tool_schema_batch_shows_in_the_plan():
    assert upgrade.check_plan(fixture("plan_tool_set_change")).tool_set_in_plan is True


def test_a_classification_change_alone_shows_in_the_plan():
    plan = fixture("plan_tool_set_change")
    for change in plan["resource_changes"]:
        if change["type"] == "aws_bedrockagentcore_gateway_target":
            change["change"]["actions"] = ["no-op"]
    assert upgrade.check_plan(plan).tool_set_in_plan is True


def test_the_tool_set_hash_matches_the_terraform_formula():
    files = [
        ROOT / "mcp/classification.json",
        ROOT / "mcp/k8s_read/tools.json",
        ROOT / "mcp/k8s_write/tools.json",
    ]
    digests = "".join(hashlib.sha256(f.read_bytes()).hexdigest() for f in files)
    assert upgrade.tool_set_hash(ROOT) == hashlib.sha256(digests.encode()).hexdigest()
    main_tf = (ROOT / "terraform/main.tf").read_text()
    assert 'filesha256("${local.repo_root}/mcp/classification.json")' in main_tf
    assert "sort(keys(local.functions))" in main_tf


# --- Fakes ------------------------------------------------------------------------------


class ParameterNotFound(Exception):
    pass


class FakeSSM:
    exceptions = type("Exceptions", (), {"ParameterNotFound": ParameterNotFound})

    def __init__(self, value: str | None = "true", log: list | None = None) -> None:
        self.values = {PARAM: value} if value is not None else {}
        self.log = log if log is not None else []

    def get_parameter(self, Name):
        if Name not in self.values:
            raise ParameterNotFound(Name)
        return {"Parameter": {"Value": self.values[Name]}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        assert (Type, Overwrite) == ("String", True)
        self.values[Name] = Value
        self.log.append(("ssm", Value))


class FakeLambda:
    def __init__(self, statuses=("ok",), log=None, on_invoke=None) -> None:
        self.statuses = list(statuses)
        self.log = log if log is not None else []
        self.on_invoke = on_invoke

    def invoke(self, FunctionName, InvocationType):
        self.log.append(("sentinel", FunctionName))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if self.on_invoke:
            self.on_invoke(status)
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps({"status": status}).encode())}


class FakeTerraform:
    def __init__(self, plans, applies=None, outputs=None, log=None) -> None:
        self.plans = list(plans)
        self.applies = list(applies or [upgrade.Result(0, "Apply complete!")])
        self._outputs = outputs if outputs is not None else default_outputs()
        self.log = log if log is not None else []
        self.plan_removed = False

    def remove_plan(self):
        self.plan_removed = True

    def outputs(self):
        return dict(self._outputs)

    def plan(self):
        self.log.append(("plan",))
        return self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]

    def apply_plan(self):
        self.log.append(("apply",))
        return self.applies.pop(0) if len(self.applies) > 1 else self.applies[0]

    def apply_targets(self, targets):
        self.log.append(("apply_targets", tuple(targets)))
        return upgrade.Result(0, "Apply complete!")


class FakeRegister:
    def __init__(self, log) -> None:
        self.log = log

    def enable_write(self, client, args):
        self.log.append(("enable_write", args.agent_space_id, args.association_id))

    def install_guidance(self, client, args):
        self.log.append(("install_guidance", args.agent_space_id, args.agent_type))

    def guidance_status(self, client, space):
        return {
            "agentsMd": [{"agentType": "GENERIC", "version": 3}],
            "skills": [{"name": "kube-handrail-scale", "version": 2}],
        }


def default_outputs(**overrides):
    outputs = {
        "kill_switch_parameter": PARAM,
        "sentinel_function_name": "kh-test-sentinel",
        "approval_mode": "server",
        "tool_set_hash": upgrade.tool_set_hash(ROOT),
        "devops_agent_space_id": "space-1",
        "devops_agent_association_id": "assoc-1",
        "gateway_url": "https://gw.example/mcp",
        "cognito_token_endpoint": "https://d.auth.eu-west-1.amazoncognito.com/oauth2/token",
        "cognito_scopes": ["kh-test/read", "kh-test/remediate"],
        "agent_client_ids": {"smoke_test": "client-smoke"},
        "cognito_user_pool_id": "eu-west-1_pool",
    }
    outputs.update(overrides)
    return outputs


class FakeCognito:
    def describe_user_pool_client(self, UserPoolId, ClientId):
        return {"UserPoolClient": {"ClientSecret": "s3cret"}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A deployment whose plan changes nothing dangerous, the tool set unchanged."""
    monkeypatch.delenv("AGENT_SPACE_ID", raising=False)
    monkeypatch.delenv("ASSOCIATION_ID", raising=False)
    monkeypatch.delenv("CLIENT_ID", raising=False)
    monkeypatch.delenv("CLIENT_SECRET", raising=False)
    log: list = []
    monkeypatch.setattr(upgrade, "guidance_differs", lambda *args: ["AGENTS.md"])

    class World:
        def __init__(self) -> None:
            self.log = log
            self.state_path = tmp_path / ".upgrade-state.json"
            self.ssm = FakeSSM(log=log)
            self.lam = FakeLambda(log=log)
            self.tf = FakeTerraform([fixture("plan_switch_removal")], log=log)
            self.smoke_calls: list = []
            self.smoke_result = 0

        def make(self, **options) -> "upgrade.Upgrade":
            options.setdefault("yes", True)

            def smoke(command, env):
                self.log.append(("smoke", tuple(command[2:])))
                self.smoke_calls.append((command, env))
                return self.smoke_result

            return upgrade.Upgrade(
                self.tf,
                upgrade.Clients(
                    ssm=self.ssm,
                    lambda_=self.lam,
                    devops=lambda: object(),
                    cognito=FakeCognito,
                ),
                upgrade.Options(**options),
                state_path=self.state_path,
                repo_root=ROOT,
                register=FakeRegister(log),
                smoke=smoke,
                sleep=lambda seconds: None,
            )

        def state(self) -> dict:
            return json.loads(self.state_path.read_text())

    return World()


def tool_set_changed(world) -> None:
    world.tf._outputs["tool_set_hash"] = "deployed-before"
    world.tf.plans = [fixture("plan_tool_set_change")]


# --- Runs -------------------------------------------------------------------------------


def test_an_upgrade_without_a_tool_change_leaves_the_kill_switch_alone(world):
    assert world.make().run() == 0
    assert world.log == [
        ("plan",),
        ("apply",),
        ("sentinel", "kh-test-sentinel"),
        ("install_guidance", "space-1", "GENERIC"),
        ("smoke", ("--read-only",)),
    ]
    assert world.ssm.values[PARAM] == "true"
    assert not world.state_path.exists()
    ((_, env),) = world.smoke_calls
    assert (env["CLIENT_ID"], env["CLIENT_SECRET"]) == ("client-smoke", "s3cret")
    assert env["SCOPE"] == "kh-test/read kh-test/remediate"


def test_client_mode_runs_the_full_smoke_test(world):
    world.tf._outputs["approval_mode"] = "client"
    world.make().run()
    assert ("smoke", ()) in world.log


def test_a_tool_change_turns_the_kill_switch_off_around_the_apply(world):
    tool_set_changed(world)
    assert world.make().run() == 0
    marker = next(entry[1] for entry in world.log if entry[0] == "ssm")
    assert marker.startswith("false:upgrade:")
    assert world.log == [
        ("plan",),
        ("ssm", marker),
        ("apply",),
        ("enable_write", "space-1", "assoc-1"),
        ("sentinel", "kh-test-sentinel"),
        ("ssm", "true"),
        ("install_guidance", "space-1", "GENERIC"),
        ("smoke", ("--read-only",)),
    ]


def test_without_a_deployed_hash_the_plan_decides(world):
    world.tf._outputs.pop("tool_set_hash")
    world.tf.plans = [fixture("plan_tool_set_change")]
    world.make().run()
    assert ("enable_write", "space-1", "assoc-1") in world.log
    world.log.clear()
    world.tf.plans = [fixture("plan_switch_removal")]
    world.make().run()
    assert not any(entry[0] in ("enable_write", "ssm") for entry in world.log)


def test_without_devops_agent_ids_enable_write_and_guidance_are_skipped(world, capsys):
    tool_set_changed(world)
    world.tf._outputs.update(devops_agent_space_id=None, devops_agent_association_id=None)
    world.make().run()
    assert not any(entry[0] in ("enable_write", "install_guidance") for entry in world.log)
    out = capsys.readouterr().out
    assert "run enable-write yourself" in out and "enable-write skipped" in out


def test_a_destructive_plan_stops_before_anything_changes(world):
    world.tf.plans = [fixture("plan_destructive")]
    with pytest.raises(upgrade.UpgradeError, match="aws_bedrockagentcore_gateway.this"):
        world.make().run()
    assert world.log == [("plan",)]
    assert not world.state_path.exists()


def test_a_dry_run_changes_nothing(world, capsys):
    tool_set_changed(world)
    assert world.make(dry_run=True).run() == 0
    assert world.log == [("plan",)]
    assert world.ssm.values[PARAM] == "true"
    assert not world.state_path.exists()
    out = capsys.readouterr().out
    assert "turn the kill switch off" in out and "enable-write" in out


def test_a_dry_run_only_warns_about_a_kill_switch_that_is_off(world, capsys):
    world.ssm.values[PARAM] = "false"
    assert world.make(dry_run=True).run() == 0
    assert "WARNING: the kill switch" in capsys.readouterr().out


def test_an_unconfirmed_plan_is_not_applied(world):
    upgrade_ = world.make(yes=False)
    upgrade_.confirm = lambda question: False
    with pytest.raises(upgrade.UpgradeError, match="not confirmed"):
        upgrade_.run()
    assert world.log == [("plan",)]
    assert not world.state_path.exists()


# --- Kill switch ownership --------------------------------------------------------------


@pytest.mark.parametrize("value", ["false", "", None, "false:upgrade:someone-else"])
def test_a_kill_switch_this_script_did_not_turn_off_is_refused(world, value):
    world.ssm.values.pop(PARAM)
    if value is not None:
        world.ssm.values[PARAM] = value
    with pytest.raises(upgrade.UpgradeError, match="did not turn it off"):
        world.make().run()
    assert world.log == []
    assert not world.state_path.exists()


def test_drift_found_by_the_sentinel_keeps_remediation_off(world):
    tool_set_changed(world)

    def sentinel_trips(status):
        world.ssm.values[PARAM] = "false"

    world.lam = FakeLambda(statuses=("drift",), log=world.log, on_invoke=sentinel_trips)
    with pytest.raises(upgrade.UpgradeError, match="'drift'"):
        world.make().run()
    assert world.ssm.values[PARAM] == "false"
    assert world.state()["done"] == [
        "preflight",
        "plan",
        "disable_kill_switch",
        "migrate",
        "apply",
        "k8s",
        "enable_write",
    ]
    # The switch is no longer this run's: a resume refuses rather than turning it on.
    with pytest.raises(upgrade.UpgradeError, match="overwrote this run.s marker"):
        world.make(resume=True).run()
    assert world.state_path.exists()


def test_a_kill_switch_changed_after_the_sentinel_is_left_alone(world):
    tool_set_changed(world)

    def operator_writes(status):
        world.ssm.values[PARAM] = "false"

    world.lam = FakeLambda(log=world.log, on_invoke=operator_writes)
    with pytest.raises(upgrade.UpgradeError, match="overwrote this run.s marker"):
        world.make().run()
    assert world.ssm.values[PARAM] == "false"


def test_an_unknown_sentinel_result_is_retried(world):
    world.lam = FakeLambda(statuses=("unknown", "unknown", "ok"), log=world.log)
    assert world.make().run() == 0
    assert [e for e in world.log if e[0] == "sentinel"] == [("sentinel", "kh-test-sentinel")] * 3


# --- Resume -----------------------------------------------------------------------------


def test_a_failed_apply_resumes_with_a_new_plan_and_keeps_the_switch_owned(world):
    tool_set_changed(world)
    world.tf.applies = [
        upgrade.Result(1, "Error: timeout waiting for gateway"),
        upgrade.Result(0, ""),
    ]
    with pytest.raises(upgrade.UpgradeError, match="--resume"):
        world.make().run()
    state = world.state()
    assert state["done"] == ["preflight", "plan", "disable_kill_switch", "migrate"]
    assert state["kill_switch_disabled"] is True
    assert world.ssm.values[PARAM] == state["kill_switch_marker"]

    # A plain run refuses while the state file exists.
    with pytest.raises(upgrade.UpgradeError, match="--resume"):
        world.make().run()

    world.log.clear()
    assert world.make(resume=True).run() == 0
    assert world.log == [
        ("plan",),
        ("apply",),
        ("enable_write", "space-1", "assoc-1"),
        ("sentinel", "kh-test-sentinel"),
        ("ssm", "true"),
        ("install_guidance", "space-1", "GENERIC"),
        ("smoke", ("--read-only",)),
    ]
    assert not world.state_path.exists()


def test_a_failed_smoke_test_resumes_with_the_smoke_test_only(world):
    world.smoke_result = 1
    with pytest.raises(upgrade.UpgradeError, match="smoke test failed"):
        world.make().run()
    assert world.state()["done"][-1] == "guidance"
    world.log.clear()
    world.smoke_result = 0
    assert world.make(resume=True).run() == 0
    assert world.log == [("smoke", ("--read-only",))]


def test_resume_without_a_state_file_is_refused(world):
    with pytest.raises(upgrade.UpgradeError, match="no .upgrade-state.json"):
        world.make(resume=True).run()


def test_a_state_file_of_another_version_is_refused(world):
    world.state_path.write_text(json.dumps({"version": 99, "run_id": "x", "started_at": 0}))
    with pytest.raises(upgrade.UpgradeError, match="another version"):
        world.make(resume=True).run()


def test_the_marker_is_recorded_before_the_switch_is_written(world):
    tool_set_changed(world)
    seen = []
    original = world.ssm.put_parameter

    def put(**kwargs):
        seen.append(world.state()["kill_switch_marker"])
        original(**kwargs)

    world.ssm.put_parameter = put
    world.tf.applies = [upgrade.Result(1, "boom")]
    with pytest.raises(upgrade.UpgradeError):
        world.make().run()
    assert seen == [world.ssm.values[PARAM]]


# --- Apply retry and migrations ---------------------------------------------------------


def test_an_inconsistent_final_plan_is_retried_once_after_a_new_plan(world):
    world.tf.applies = [upgrade.Result(1, INCONSISTENT), upgrade.Result(0, "Apply complete!")]
    assert world.make().run() == 0
    assert world.log[:4] == [("plan",), ("apply",), ("plan",), ("apply",)]


def test_a_second_failure_is_not_retried(world):
    world.tf.applies = [upgrade.Result(1, INCONSISTENT), upgrade.Result(1, INCONSISTENT)]
    with pytest.raises(upgrade.UpgradeError, match="failed twice"):
        world.make().run()
    assert world.log == [("plan",), ("apply",), ("plan",), ("apply",)]


def test_other_apply_errors_are_not_retried(world):
    world.tf.applies = [upgrade.Result(1, "Error: AccessDenied")]
    with pytest.raises(upgrade.UpgradeError, match="apply failed"):
        world.make().run()
    assert world.log == [("plan",), ("apply",)]


def test_a_retry_plan_that_turned_destructive_is_refused(world):
    world.tf.plans = [fixture("plan_switch_removal"), fixture("plan_destructive")]
    world.tf.applies = [upgrade.Result(1, INCONSISTENT)]
    with pytest.raises(upgrade.UpgradeError, match="must survive"):
        world.make().run()
    assert world.log == [("plan",), ("apply",), ("plan",)]


def test_the_legacy_migration_applies_its_targets_first(world):
    world.tf.plans = [fixture("plan_legacy_migration"), fixture("plan_switch_removal")]
    assert world.make().run() == 0
    assert world.log[:4] == [
        ("plan",),
        (
            "apply_targets",
            (
                "terraform_data.caller_key",
                "aws_iam_role.interceptor",
                "aws_lambda_function.interceptor",
                "aws_iam_role.verifier",
            ),
        ),
        ("plan",),
        ("apply",),
    ]


def test_region_from_the_token_endpoint():
    assert upgrade.region_from(default_outputs()) == "eu-west-1"
    assert upgrade.region_from({}) is None


# --- Terraform wrapper ------------------------------------------------------------------


def test_the_terraform_commands(tmp_path):
    calls = []
    plan_json = json.dumps(fixture("plan_tool_set_change"))

    def stream(command):
        calls.append(command)
        return upgrade.Result(0, "")

    def quiet(command):
        calls.append(command)
        if command[2] == "output":
            return upgrade.Result(0, json.dumps({"approval_mode": {"value": "client"}}))
        return upgrade.Result(0, plan_json)

    tf = upgrade.Terraform(tmp_path, ["prod.tfvars"], stream=stream, quiet=quiet)
    assert tf.outputs() == {"approval_mode": "client"}
    assert tf.plan()["resource_changes"]
    tf.apply_plan()
    tf.apply_targets(["terraform_data.caller_key", "aws_iam_role.verifier"])
    chdir = f"-chdir={tmp_path}"
    assert calls == [
        ["terraform", chdir, "output", "-json"],
        # A module the release added is installed before the plan.
        ["terraform", chdir, "get", "-no-color"],
        [
            "terraform",
            chdir,
            "plan",
            "-input=false",
            "-lock-timeout=60s",
            "-out=upgrade.tfplan",
            "-var-file=prod.tfvars",
        ],
        ["terraform", chdir, "show", "-json", "upgrade.tfplan"],
        ["terraform", chdir, "apply", "-input=false", "-lock-timeout=60s", "upgrade.tfplan"],
        [
            "terraform",
            chdir,
            "apply",
            "-input=false",
            "-lock-timeout=60s",
            "-auto-approve",
            "-target=terraform_data.caller_key",
            "-target=aws_iam_role.verifier",
            "-var-file=prod.tfvars",
        ],
    ]


def test_a_failed_terraform_command_is_an_upgrade_error(tmp_path):
    def fail(command):
        return upgrade.Result(1, "Error: No configuration files")

    tf = upgrade.Terraform(tmp_path, [], stream=fail, quiet=fail)
    with pytest.raises(upgrade.UpgradeError, match="terraform output failed"):
        tf.outputs()
    with pytest.raises(upgrade.UpgradeError, match="terraform get failed"):
        tf.plan()
    tf = upgrade.Terraform(tmp_path, [], stream=fail, quiet=lambda command: upgrade.Result(0, ""))
    with pytest.raises(upgrade.UpgradeError, match="terraform plan failed"):
        tf.plan()


# --- Guidance comparison ----------------------------------------------------------------


class NotFound(Exception):
    pass


class FakeAssets:
    """DevOps Agent assets holding the given AGENTS.md and skill texts."""

    exceptions = type("Exceptions", (), {"ResourceNotFoundException": NotFound})

    def __init__(self, agents_md: str | None, skills: dict[str, str]) -> None:
        self.files = {}
        self.items = {"agents_md": [], "skill": []}
        if agents_md is not None:
            self.items["agents_md"].append({"assetId": "a1", "metadata": {"agent_type": "GENERIC"}})
            self.files["a1"] = agents_md
        for i, (name, text) in enumerate(skills.items()):
            self.items["skill"].append({"assetId": f"s{i}", "metadata": {"name": name}})
            self.files[f"s{i}"] = text

    def list_assets(self, agentSpaceId, assetType, **_):
        return {"items": self.items[assetType]}

    def get_asset_file(self, agentSpaceId, assetId, path):
        if assetId not in self.files:
            raise NotFound(assetId)
        return {"file": {"content": {"text": self.files[assetId]}}}


def repo_guidance():
    register = upgrade.register_module()
    skills = {s.name: s.text for s in register.load_skills(register.SKILLS_DIR)}
    return register, register.load_agents_md(register.AGENTS_MD), skills


def test_installed_guidance_that_matches_the_repository_is_current():
    register, agents_md, skills = repo_guidance()
    client = FakeAssets(agents_md, skills)
    assert upgrade.guidance_differs(register, client, "space-1", "GENERIC") == []


def test_changed_or_missing_guidance_is_reported():
    register, agents_md, skills = repo_guidance()
    name = sorted(skills)[0]
    client = FakeAssets(agents_md + "\nold line\n", {**skills, name: "old"})
    assert upgrade.guidance_differs(register, client, "space-1", "GENERIC") == [
        "AGENTS.md",
        f"skill {name}",
    ]
    client = FakeAssets(None, {})
    assert upgrade.guidance_differs(register, client, "space-1", "GENERIC") == ["AGENTS.md"] + [
        f"skill {n}" for n in sorted(skills)
    ]


# --- The kill switch is checked again before every apply --------------------------------


def ssm_writes_before_apply(world) -> list:
    """The kill switch writes logged before the last apply."""
    last_apply = max(i for i, e in enumerate(world.log) if e == ("apply",))
    return [e for e in world.log[:last_apply] if e[0] == "ssm"]


def test_a_tool_change_that_appears_on_resume_turns_the_kill_switch_off(world):
    world.tf.applies = [upgrade.Result(1, "Error: AccessDenied"), upgrade.Result(0, "")]
    with pytest.raises(upgrade.UpgradeError, match="apply failed"):
        world.make().run()
    assert world.ssm.values[PARAM] == "true"

    # The operator changes a tools.json while fixing the cause, then resumes.
    tool_set_changed(world)
    world.log.clear()
    assert world.make(resume=True).run() == 0
    [(_, marker)] = ssm_writes_before_apply(world)
    assert marker.startswith(upgrade.MARKER_PREFIX)
    assert ("enable_write", "space-1", "assoc-1") in world.log
    assert world.ssm.values[PARAM] == "true"


def test_a_switch_turned_on_by_hand_is_turned_off_again_for_the_apply(world):
    tool_set_changed(world)
    world.tf.applies = [upgrade.Result(1, "Error: timeout"), upgrade.Result(0, "")]
    with pytest.raises(upgrade.UpgradeError):
        world.make().run()
    marker = world.state()["kill_switch_marker"]

    # Something overwrote the marker with false while the run was stopped.
    world.ssm.values[PARAM] = "false"
    with pytest.raises(upgrade.UpgradeError, match="overwrote this run's marker"):
        world.make(resume=True).run()

    # The operator turns it on by hand and resumes: the apply still runs with it off.
    world.ssm.values[PARAM] = "true"
    world.log.clear()
    assert world.make(resume=True).run() == 0
    assert ssm_writes_before_apply(world) == [("ssm", marker)]
    assert world.ssm.values[PARAM] == "true"


def test_a_tool_change_seen_only_after_the_migration_turns_the_kill_switch_off(world):
    world.tf._outputs.pop("tool_set_hash")
    world.tf.plans = [fixture("plan_legacy_migration"), fixture("plan_tool_set_change")]
    assert world.make().run() == 0
    [(_, marker)] = ssm_writes_before_apply(world)
    assert marker.startswith(upgrade.MARKER_PREFIX)
    assert world.ssm.values[PARAM] == "true"


def test_a_tool_change_seen_only_by_the_retry_plan_turns_the_kill_switch_off(world):
    world.tf._outputs.pop("tool_set_hash")
    world.tf.plans = [fixture("plan_switch_removal"), fixture("plan_tool_set_change")]
    world.tf.applies = [upgrade.Result(1, INCONSISTENT), upgrade.Result(0, "Apply complete!")]
    assert world.make().run() == 0
    [(_, marker)] = ssm_writes_before_apply(world)
    assert marker.startswith(upgrade.MARKER_PREFIX)
    assert world.ssm.values[PARAM] == "true"


# --- Bad input is a clean error ---------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"version": upgrade.STATE_VERSION, "run_id": "x", "started_at": 0, "x": 1}),
        json.dumps({"version": upgrade.STATE_VERSION}),
        "{not json",
        json.dumps([1]),
    ],
)
def test_an_unreadable_state_file_is_an_upgrade_error(world, content):
    world.state_path.write_text(content)
    with pytest.raises(upgrade.UpgradeError, match="cannot read .upgrade-state.json"):
        world.make(resume=True).run()
    assert world.state_path.read_text() == content


def test_plan_json_that_is_not_json_is_an_upgrade_error(tmp_path):
    tf = upgrade.Terraform(
        tmp_path,
        [],
        stream=lambda command: upgrade.Result(0, ""),
        quiet=lambda command: upgrade.Result(0, "Warning: something\n{"),
    )
    with pytest.raises(upgrade.UpgradeError, match="terraform show did not return JSON"):
        tf.plan()


def test_run_quiet_keeps_stderr_out_of_the_output():
    command = [sys.executable, "-c", "import sys; print('{}'); print('warn', file=sys.stderr)"]
    result = upgrade.run_quiet(command)
    assert (result.returncode, result.output.strip(), result.error.strip()) == (0, "{}", "warn")


# --- Leftovers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setup, ok",
    [
        (lambda world: None, True),
        (lambda world: world.options_.update(dry_run=True), True),
        (lambda world: setattr(world, "smoke_result", 1), False),
        (lambda world: world.tf.__setattr__("applies", [upgrade.Result(1, "boom")]), False),
    ],
)
def test_the_saved_plan_is_removed_however_the_run_ends(world, setup, ok):
    # The saved plan holds the caller key and client secrets in plain text.
    world.options_ = {}
    setup(world)
    upgrade_ = world.make(**world.options_)
    if ok:
        assert upgrade_.run() == 0
    else:
        with pytest.raises(upgrade.UpgradeError):
            upgrade_.run()
    assert world.tf.plan_removed


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
def test_an_interrupted_confirmation_is_a_no(world, interrupt):
    upgrade_ = world.make(yes=False)

    def confirm(question):
        raise interrupt

    upgrade_.confirm = confirm
    with pytest.raises(upgrade.UpgradeError, match="not confirmed"):
        upgrade_.run()
    assert world.log == [("plan",)]
    assert not world.state_path.exists()


def test_terraform_removes_its_saved_plan(tmp_path):
    (tmp_path / upgrade.PLAN_FILE).write_text("secret")
    tf = upgrade.Terraform(tmp_path, [])
    tf.remove_plan()
    tf.remove_plan()
    assert not (tmp_path / upgrade.PLAN_FILE).exists()


# --- Cluster manifests (--with-k8s) -------------------------------------------------------

MANIFESTS = {
    "rbac.yaml": (
        "kind: Role\nmetadata:\n  name: kube-handrail-reader\n  namespace: team-a\n---\n"
        "kind: RoleBinding\nmetadata:\n  name: kube-handrail-reader\n  namespace: team-a\n"
    ),
    "admission-policy.yaml": "kind: ValidatingAdmissionPolicy\nmetadata:\n  name: x\n",
}


class FakeKubectl:
    def __init__(self, log, actions=("Deny",), labeled=None, fail=None) -> None:
        self.log = log
        self.actions = list(actions)
        self.labeled = labeled
        self.fail = fail
        self.applied: list = []

    def current_context(self):
        return "test-context"

    def apply(self, directory, dry_run):
        self.log.append(("kubectl", "dry-run" if dry_run else "apply"))
        self.applied.append(sorted(p.name for p in directory.glob("*.yaml")))
        failed = self.fail == ("dry-run" if dry_run else "apply")
        return upgrade.Result(1 if failed else 0, "")

    def binding_actions(self):
        return self.actions

    def labeled_roles(self):
        return self.labeled


def k8s_world(world, tmp_path, manifests_before=None, **kubectl):
    world.tf._outputs["k8s_manifests"] = MANIFESTS
    world.tf._outputs["guard_settings"] = {"admission_mode": "Deny", "approval_mode": "server"}
    world.kubectl = FakeKubectl(world.log, **kubectl)
    before = dict(world.tf._outputs, k8s_manifests=manifests_before or MANIFESTS)
    outputs = [before]

    def outputs_fn():
        return dict(outputs.pop(0)) if outputs else dict(world.tf._outputs)

    world.tf.outputs = outputs_fn
    root = tmp_path / "repo"
    root.mkdir()

    def make(**options):
        options.setdefault("yes", True)
        upgrade_ = world.make(**options)
        upgrade_.kubectl = world.kubectl
        upgrade_.k8s_dir = root / "build/k8s"
        return upgrade_

    return make, root


def test_with_k8s_writes_and_applies_the_manifests_after_a_dry_run(world, tmp_path):
    make, root = k8s_world(
        world,
        tmp_path,
        labeled=["team-a/Role/kube-handrail-reader", "team-a/RoleBinding/kube-handrail-reader"],
    )
    assert make(with_k8s=True).run() == 0
    assert world.log[:4] == [("plan",), ("apply",), ("kubectl", "dry-run"), ("kubectl", "apply")]
    assert sorted(p.name for p in (root / "build/k8s").glob("*.yaml")) == sorted(MANIFESTS)
    assert (root / "build/k8s/rbac.yaml").read_text() == MANIFESTS["rbac.yaml"]


def test_a_failed_dry_run_applies_nothing(world, tmp_path):
    make, _ = k8s_world(world, tmp_path, fail="dry-run")
    with pytest.raises(upgrade.UpgradeError, match="nothing was applied"):
        make(with_k8s=True).run()
    assert ("kubectl", "apply") not in world.log
    assert world.state()["done"][-1] == "apply"


def test_a_binding_left_in_warn_fails_the_step(world, tmp_path):
    make, _ = k8s_world(world, tmp_path, actions=("Warn", "Audit"))
    with pytest.raises(upgrade.UpgradeError, match="validationActions"):
        make(with_k8s=True).run()


def test_stale_roles_are_listed_not_deleted(world, tmp_path, capsys):
    make, _ = k8s_world(
        world,
        tmp_path,
        labeled=[
            "team-a/Role/kube-handrail-reader",
            "team-a/RoleBinding/kube-handrail-reader",
            "old-team/Role/kube-handrail-writer",
        ],
    )
    assert make(with_k8s=True).run() == 0
    out = capsys.readouterr().out
    assert "kubectl delete role kube-handrail-writer -n old-team" in out
    assert "1 stale kube-handrail Role(s) or binding(s)" in out


def test_without_with_k8s_a_manifest_change_is_only_reported(world, tmp_path, capsys):
    make, root = k8s_world(world, tmp_path, manifests_before={"rbac.yaml": "older"})
    assert make().run() == 0
    assert not any(entry[0] == "kubectl" for entry in world.log)
    assert not (root / "build/k8s").exists()
    assert "the cluster manifests changed and were not applied" in capsys.readouterr().out


def test_without_with_k8s_unchanged_manifests_say_nothing(world, tmp_path, capsys):
    make, _ = k8s_world(world, tmp_path)
    assert make().run() == 0
    assert "cluster manifests" not in capsys.readouterr().out


def test_the_rendered_roles_are_read_from_the_template_layout():
    golden = ROOT / "tests/k8s_render/golden/two-namespaces"
    manifests = {p.name: p.read_text() for p in golden.glob("*.yaml")}
    assert upgrade.rendered_roles(manifests) == {
        f"{ns}/{kind}/kube-handrail-{who}"
        for ns in ("kube-handrail-sandbox", "kube-handrail-sandbox-2")
        for kind in ("Role", "RoleBinding")
        for who in ("reader", "writer")
    }


def test_manifest_names_must_be_plain_yaml_files(tmp_path):
    with pytest.raises(upgrade.UpgradeError, match="unexpected manifest name"):
        upgrade.write_manifests({"../x.yaml": ""}, tmp_path)


def test_kubectl_commands_use_the_given_context():
    calls = []

    def run(command):
        calls.append(command)
        return upgrade.Result(0, '["Deny"]')

    kubectl = upgrade.Kubectl("ctx", stream=run, quiet=run)
    assert kubectl.current_context() == "ctx"
    kubectl.apply(Path("build/k8s"), dry_run=True)
    assert kubectl.binding_actions() == ["Deny"]
    assert calls[0] == [
        "kubectl",
        "--context",
        "ctx",
        "apply",
        "--dry-run=server",
        "-f",
        "build/k8s",
    ]
    assert calls[1][:3] == ["kubectl", "--context", "ctx"]
