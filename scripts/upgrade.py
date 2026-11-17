#!/usr/bin/env python3
"""Upgrade a kube-handrail deployment in one resumable command.

Steps, in order (docs/deploy.md, Upgrading):

 1. preflight   The global kill switch must be on, unless this script turned it off in the
                run being resumed (its value is then this run's marker).
 2. plan        terraform plan, saved, and its JSON checked: no delete or replace of the
                gateway, its targets, the Cognito user pool, domain or clients, the plan
                table, the audit bucket, the kill switch or the freezes parameter, and no
                replace of an agent's kill switch. Known two-step migrations are detected,
                and whether the tool set (tools.json, classification.json) changes.
                --dry-run stops here and changes nothing; otherwise the plan is applied
                after a confirmation (--yes skips it).
 3. kill switch Turned off first when the tool set changes, so no agent writes while the
                gateway and DevOps Agent disagree about the tools.
 4. migrate     The targeted apply of a known two-step migration, then a new plan.
 5. apply       The saved plan; once more, after a new plan and check, when Terraform fails
                with "inconsistent final plan".
 5b. k8s        With --with-k8s: the cluster manifests (the k8s_manifests output) written to
                build/k8s and applied with kubectl, after a server-side dry run of all of
                them; then the admission policy binding is checked against admission_mode,
                and kube-handrail Roles the manifests no longer contain are listed (never
                deleted). Without it, a warning when the manifests changed. --k8s-only runs
                just this step against the deployed state (after a confirmation).
 6. enable-write  Only when the tool set changed and the DevOps Agent IDs are known.
 7. sentinel    Invoked at once; its result must be ok.
 8. kill switch Turned back on only if this script turned it off and nothing else wrote it
                since (the drift sentinel writes "false" when it finds drift).
 9. guidance    install-guidance when AGENTS.md or a skill differs from what is installed;
                prints the asset versions and the stale-skill warning.
10. smoke       scripts/smoke_test.py; --read-only in server and both mode.

terraform get runs before every plan, so a module added by the release is installed (it
touches neither the backend nor the providers).

Progress is kept in .upgrade-state.json at the repository root (not committed); after a
failure, fix the cause and run again with --resume. Until the apply is done, a resume runs
the plan, the kill switch step and the migration again. The state file is removed when the
upgrade finishes.

The DevOps Agent IDs come from --agent-space-id / --association-id, AGENT_SPACE_ID /
ASSOCIATION_ID, or the Terraform outputs devops_agent_space_id and
devops_agent_association_id. Without them enable-write and install-guidance are skipped
with a warning. The smoke test client's secret is read from Cognito and never printed.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / ".upgrade-state.json"
PLAN_FILE = "upgrade.tfplan"
STATE_VERSION = 1

# Re-run on resume until "apply" is done.
BEFORE_APPLY = ("plan", "disable_kill_switch", "migrate")

STEPS = (
    "preflight",
    "plan",
    "disable_kill_switch",
    "migrate",
    "apply",
    "k8s",
    "enable_write",
    "sentinel",
    "enable_kill_switch",
    "guidance",
    "smoke",
)

# (resource type, name or None for any name): a delete, a replace or a forget of these
# loses state that cannot be recreated: the gateway URL and its registration, the agents'
# clients and secrets, the plan history, the audit record, operator-set switch values.
PROTECTED = (
    ("aws_bedrockagentcore_gateway", None),
    ("aws_bedrockagentcore_gateway_target", None),
    ("aws_cognito_user_pool", None),
    ("aws_cognito_user_pool_domain", None),
    ("aws_cognito_user_pool_client", None),
    ("aws_dynamodb_table", "plans"),
    ("aws_s3_bucket", "audit"),
    ("aws_ssm_parameter", "kill_switch"),
    ("aws_ssm_parameter", "freezes"),
)
# An agent removed from var.agents loses its switch, which is intended; a replace would
# reset a switch an operator turned off back to "true".
NO_REPLACE = (("aws_ssm_parameter", "agent_enabled"),)
DESTRUCTIVE_ACTIONS = frozenset({"delete", "forget"})

INCONSISTENT_PLAN = "inconsistent final plan"
SENTINEL_OK = "ok"
SENTINEL_ATTEMPTS = 3
SENTINEL_RETRY_SECONDS = 20
# The kill switch value this script writes: anything but "true" turns remediation off, and
# the run ID shows whose it is.
MARKER_PREFIX = "false:upgrade:"
# The cluster manifests (terraform/k8s.tf): where --with-k8s writes them, the binding it
# checks, and the label of the objects it rendered (and of the earlier static manifests).
K8S_DIR = Path("build") / "k8s"
BINDING = "kube-handrail-writer-bounds"
HPA_BINDING = "kube-handrail-writer-hpa-bounds"
BINDINGS = (BINDING, HPA_BINDING)
PART_OF_SELECTOR = "app.kubernetes.io/part-of in (kube-handrail,kube-handrail-sandbox)"
# DevOps Agent may keep serving the previous skill text to running sessions for a while.
STALE_SKILL_MINUTES = 35
GENERIC = "GENERIC"


class UpgradeError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    """A change Terraform cannot apply in one step: `creates` are resources whose new
    values other resources' existing settings read (an unknown value added to an existing
    Lambda environment makes the provider fail with an inconsistent final plan). They are
    applied first with -target."""

    name: str
    description: str
    creates: tuple[str, ...]


MIGRATIONS = (
    Migration(
        name="interceptor-and-verifier",
        description=(
            "a deployment without the interceptor or the verifier (legacy mode or "
            "verify_enabled = false) gets them: the caller key, the interceptor and the "
            "verifier role are created first, because the tool Lambdas, the approver and "
            "the sentinel read them"
        ),
        creates=(
            "terraform_data.caller_key",
            "aws_iam_role.interceptor",
            "aws_iam_role.verifier",
            "aws_lambda_function.interceptor",
        ),
    ),
)


# --- Plan checks ------------------------------------------------------------------------


@dataclass
class PlanReport:
    violations: list[str] = field(default_factory=list)
    migrations: list[Migration] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    moves: list[str] = field(default_factory=list)
    changes: dict[str, list[str]] = field(default_factory=dict)
    tool_set_in_plan: bool = False


def _protected(kind: str, name: str, rules: Iterable[tuple[str, str | None]]) -> bool:
    return any(kind == k and (n is None or n == name) for k, n in rules)


def _actions(change: Mapping[str, Any]) -> list[str]:
    return list((change.get("change") or {}).get("actions") or [])


def _environment(values: Mapping[str, Any] | None) -> dict[str, Any]:
    blocks = (values or {}).get("environment") or []
    return (blocks[0] or {}).get("variables") or {} if blocks else {}


def check_plan(plan: Mapping[str, Any]) -> PlanReport:
    """The safety rules, migrations and tool set signals of a `terraform show -json` plan."""
    report = PlanReport()
    for change in plan.get("resource_changes") or []:
        if change.get("mode", "managed") != "managed":
            continue
        address, kind, name = change["address"], change["type"], change["name"]
        actions = _actions(change)
        if change.get("previous_address"):
            report.moves.append(f"{change['previous_address']} -> {address}")
        if actions and actions != ["no-op"]:
            report.changes.setdefault("+".join(actions), []).append(address)
        destructive = DESTRUCTIVE_ACTIONS.intersection(actions)
        if destructive and _protected(kind, name, PROTECTED):
            report.violations.append(f"{address}: {'/'.join(actions)}")
        if "delete" in actions and "create" in actions and _protected(kind, name, NO_REPLACE):
            report.violations.append(f"{address}: replace (would reset the switch to true)")
        if actions == ["create"]:
            for migration in MIGRATIONS:
                if address in migration.creates:
                    if migration not in report.migrations:
                        report.migrations.append(migration)
                    report.targets.append(address)
        if actions != ["no-op"] and kind == "aws_bedrockagentcore_gateway_target":
            report.tool_set_in_plan = True
        if kind == "aws_lambda_function" and name == "sentinel" and "update" in actions:
            before = _environment((change.get("change") or {}).get("before"))
            after = _environment((change.get("change") or {}).get("after"))
            if before.get("TOOL_CLASSIFICATION") != after.get("TOOL_CLASSIFICATION"):
                report.tool_set_in_plan = True
    return report


# --- Local hashes ---------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tool_set_hash(root: Path = REPO_ROOT) -> str:
    """What Terraform's tool_set_hash output computes: SHA-256 over the hex digests of
    mcp/classification.json and each target's tools.json, by target name."""
    files = [root / "mcp/classification.json"]
    files += sorted((root / "mcp").glob("*/tools.json"), key=lambda p: p.parent.name)
    return hashlib.sha256("".join(file_sha256(f) for f in files).encode()).hexdigest()


# --- State ------------------------------------------------------------------------------


@dataclass
class State:
    run_id: str
    started_at: float
    done: list[str] = field(default_factory=list)
    tool_set_changed: bool | None = None
    targets: list[str] = field(default_factory=list)
    kill_switch_marker: str | None = None
    kill_switch_disabled: bool = False
    sentinel: str | None = None
    notes: list[str] = field(default_factory=list)
    # --with-k8s and the kubectl context it resolved to, kept so a --resume applies the
    # cluster manifests to the same context even without the flag.
    with_k8s: bool = False
    kube_context: str | None = None
    # Hash of the k8s_manifests output before the apply, so a resume after the apply can
    # still tell that the manifests changed.
    k8s_manifests_before: str | None = None

    @classmethod
    def new(cls) -> "State":
        return cls(run_id=uuid.uuid4().hex[:12], started_at=time.time())

    @classmethod
    def load(cls, path: Path) -> "State":
        unreadable = UpgradeError(
            f"cannot read {path.name}; finish the upgrade by hand (docs/deploy.md, Upgrading) "
            "and delete the file"
        )
        try:
            data = json.loads(path.read_text())
        except ValueError:
            raise unreadable from None
        if not isinstance(data, dict):
            raise unreadable
        if data.pop("version", None) != STATE_VERSION:
            raise UpgradeError(f"{path.name} was written by another version of this script")
        try:
            return cls(**data)
        except TypeError:
            raise unreadable from None

    def save(self, path: Path) -> None:
        payload = {"version": STATE_VERSION, **self.__dict__}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(path)


# --- Terraform --------------------------------------------------------------------------


@dataclass
class Result:
    returncode: int
    output: str
    error: str = ""  # stderr, when it was captured apart from the output

    def text(self) -> str:
        return self.output + self.error


Runner = Callable[[list[str]], Result]


def run_streaming(command: list[str]) -> Result:
    """Run a command, echoing its output as it comes, and return all of it."""
    lines: list[str] = []
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            lines.append(line)
    return Result(process.returncode, "".join(lines))


def run_quiet(command: list[str]) -> Result:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    # stdout only in the output: a warning on stderr must not break the JSON it carries.
    return Result(completed.returncode, completed.stdout, completed.stderr)


class Terraform:
    def __init__(
        self,
        tf_dir: Path,
        var_files: list[str],
        stream: Runner = run_streaming,
        quiet: Runner = run_quiet,
    ) -> None:
        self.tf_dir = tf_dir
        self.var_files = var_files
        self.stream = stream
        self.quiet = quiet

    def _base(self, *args: str) -> list[str]:
        return ["terraform", f"-chdir={self.tf_dir}", *args]

    def _vars(self) -> list[str]:
        return [f"-var-file={f}" for f in self.var_files]

    def outputs(self) -> dict[str, Any]:
        result = self.quiet(self._base("output", "-json"))
        if result.returncode != 0:
            raise UpgradeError(f"terraform output failed: {result.text().strip()[-500:]}")
        try:
            raw = json.loads(result.output or "{}")
        except ValueError:
            raise UpgradeError("terraform output did not return JSON") from None
        return {name: entry.get("value") for name, entry in raw.items()}

    def get(self) -> None:
        """Install the modules the configuration uses; no backend or provider change."""
        result = self.quiet(self._base("get", "-no-color"))
        if result.returncode != 0:
            raise UpgradeError(f"terraform get failed: {result.text().strip()[-500:]}")

    def plan(self) -> dict[str, Any]:
        self.get()
        result = self.stream(
            self._base("plan", "-input=false", "-lock-timeout=60s", f"-out={PLAN_FILE}")
            + self._vars()
        )
        if result.returncode != 0:
            raise UpgradeError("terraform plan failed")
        shown = self.quiet(self._base("show", "-json", PLAN_FILE))
        if shown.returncode != 0:
            raise UpgradeError(f"terraform show failed: {shown.text().strip()[-500:]}")
        try:
            plan = json.loads(shown.output)
        except ValueError:
            raise UpgradeError("terraform show did not return JSON") from None
        if not isinstance(plan, dict):
            raise UpgradeError("terraform show did not return JSON")
        return plan

    def remove_plan(self) -> None:
        """The saved plan holds the caller key and client secrets in plain text."""
        (self.tf_dir / PLAN_FILE).unlink(missing_ok=True)

    def apply_plan(self) -> Result:
        return self.stream(self._base("apply", "-input=false", "-lock-timeout=60s", PLAN_FILE))

    def apply_targets(self, targets: list[str]) -> Result:
        return self.stream(
            self._base("apply", "-input=false", "-lock-timeout=60s", "-auto-approve")
            + [f"-target={t}" for t in targets]
            + self._vars()
        )


# --- Kubernetes -------------------------------------------------------------------------


class Kubectl:
    """kubectl with the operator's kubeconfig, on --kube-context or the current context."""

    def __init__(
        self,
        context: str | None = None,
        stream: Runner = run_streaming,
        quiet: Runner = run_quiet,
    ) -> None:
        self.context = context
        self.stream = stream
        self.quiet = quiet

    def _base(self, *args: str) -> list[str]:
        return ["kubectl", *(["--context", self.context] if self.context else []), *args]

    def current_context(self) -> str:
        if self.context:
            return self.context
        result = self.quiet(["kubectl", "config", "current-context"])
        if result.returncode != 0 or not result.output.strip():
            raise UpgradeError(
                "--with-k8s: kubectl has no current context; set KUBECONFIG or --kube-context"
            )
        return result.output.strip()

    def pin(self) -> str:
        """Resolve the context once, so every later command names it with --context even
        if the kubeconfig's current context changes during the run."""
        self.context = self.current_context()
        return self.context

    def apply(self, files: list[Path], dry_run: bool) -> Result:
        """Apply the named files only, so nothing else in the directory is applied."""
        extra = ["--dry-run=server"] if dry_run else []
        names = [arg for path in files for arg in ("-f", str(path))]
        return self.stream(self._base("apply", *extra, *names))

    def binding_actions(self, name: str = BINDING) -> list[str] | None:
        result = self.quiet(
            self._base(
                "get",
                "validatingadmissionpolicybinding",
                name,
                "-o",
                "jsonpath={.spec.validationActions}",
            )
        )
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.output or "null")
        except ValueError:
            return None
        return value if isinstance(value, list) else None

    def labeled_roles(self) -> list[str] | None:
        """namespace/kind/name of every Role and RoleBinding with a kube-handrail part-of
        label, or None when they cannot be listed (no cluster-wide list right)."""
        result = self.quiet(
            self._base(
                "get",
                "roles.rbac.authorization.k8s.io,rolebindings.rbac.authorization.k8s.io",
                "--all-namespaces",
                "-l",
                PART_OF_SELECTOR,
                "-o",
                "json",
            )
        )
        if result.returncode != 0:
            return None
        try:
            items = json.loads(result.output).get("items") or []
        except (ValueError, AttributeError):
            return None
        return sorted(
            f"{i['metadata']['namespace']}/{i['kind']}/{i['metadata']['name']}" for i in items
        )


def rendered_roles(manifests: Mapping[str, str]) -> set[str]:
    """namespace/kind/name of the Roles and RoleBindings in the rendered manifests, read
    from their fixed layout (terraform/modules/namespace-policy/templates/rbac.yaml.tftpl)."""
    found: set[str] = set()
    for text in manifests.values():
        for document in text.split("\n---\n"):
            kind = re.search(r"^kind: (Role|RoleBinding)$", document, re.MULTILINE)
            name = re.search(r"^  name: (\S+)$", document, re.MULTILINE)
            namespace = re.search(r"^  namespace: (\S+)$", document, re.MULTILINE)
            if kind and name and namespace:
                found.add(f"{namespace.group(1)}/{kind.group(1)}/{name.group(1)}")
    return found


def write_manifests(manifests: Mapping[str, str], directory: Path) -> list[Path]:
    """Replace the YAML files in `directory` with the rendered manifests; return their
    paths, which are what gets applied."""
    for name in manifests:
        if "/" in name or not name.endswith(".yaml"):
            raise UpgradeError(f"unexpected manifest name {name!r} in the k8s_manifests output")
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.yaml"):
        old.unlink()
    written = []
    for name, text in sorted(manifests.items()):
        (directory / name).write_text(text)
        written.append(directory / name)
    return written


def manifests_hash(manifests: Any) -> str:
    return hashlib.sha256(json.dumps(manifests, sort_keys=True).encode()).hexdigest()


# --- Guidance ---------------------------------------------------------------------------


def register_module() -> Any:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import devops_agent_register  # noqa: PLC0415

    return devops_agent_register


def installed_text(client: Any, space_id: str, asset: dict, path: str) -> str:
    try:
        file = client.get_asset_file(agentSpaceId=space_id, assetId=asset["assetId"], path=path)
    except client.exceptions.ResourceNotFoundException:
        return ""
    return file["file"]["content"].get("text", "")


def guidance_differs(register: Any, client: Any, space_id: str, agent_type: str) -> list[str]:
    """The guidance files whose installed text is missing or differs from the working tree."""
    differs: list[str] = []
    agents_md = register.load_agents_md(register.AGENTS_MD)
    current = [
        a
        for a in register.list_assets(client, space_id, register.AGENTS_MD_TYPE)
        if a["metadata"].get("agent_type") == agent_type
    ]
    if (
        len(current) != 1
        or installed_text(client, space_id, current[0], register.AGENTS_MD_PATH) != agents_md
    ):
        differs.append("AGENTS.md")
    installed = register.list_assets(client, space_id, register.SKILL_TYPE)
    for skill in register.load_skills(register.SKILLS_DIR):
        found = [a for a in installed if a["metadata"].get("name") == skill.name]
        if (
            len(found) != 1
            or installed_text(client, space_id, found[0], register.SKILL_PATH) != skill.text
        ):
            differs.append(f"skill {skill.name}")
    return differs


# --- The upgrade ------------------------------------------------------------------------


@dataclass
class Clients:
    ssm: Any
    lambda_: Any
    devops: Callable[[], Any]
    cognito: Callable[[], Any]


@dataclass
class Options:
    dry_run: bool = False
    resume: bool = False
    agent_space_id: str | None = None
    association_id: str | None = None
    agent_type: str = GENERIC
    skip_smoke: bool = False
    smoke_namespace: str | None = None
    smoke_name: str | None = None
    yes: bool = False
    with_k8s: bool = False
    python: str = sys.executable


def say(message: str) -> None:
    print(f"[upgrade] {message}", flush=True)


def ask(question: str) -> bool:
    if not sys.stdin.isatty():
        raise UpgradeError("no terminal to confirm on; pass --yes to apply without asking")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


class Upgrade:
    def __init__(
        self,
        terraform: Terraform,
        clients: Clients,
        options: Options,
        state_path: Path = STATE_FILE,
        repo_root: Path = REPO_ROOT,
        register: Any = None,
        smoke: Callable[[list[str], dict[str, str]], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        confirm: Callable[[str], bool] | None = None,
        kubectl: Kubectl | None = None,
    ) -> None:
        self.tf = terraform
        self.kubectl = kubectl or Kubectl()
        self.k8s_dir = repo_root / K8S_DIR
        self.clients = clients
        self.options = options
        self.state_path = state_path
        self.repo_root = repo_root
        self._register = register
        self.smoke_runner = smoke or self._run_smoke
        self.sleep = sleep
        self.confirm = confirm or ask
        self.state: State | None = None
        self.outputs: dict[str, Any] = {}
        self.report: PlanReport | None = None

    # -- helpers --

    @property
    def register(self) -> Any:
        if self._register is None:
            self._register = register_module()
        return self._register

    def save(self) -> None:
        if self.state is not None and not self.options.dry_run:
            self.state.save(self.state_path)

    def done(self, step: str) -> None:
        assert self.state is not None
        if step not in self.state.done:
            self.state.done.append(step)
        self.save()

    def output(self, name: str) -> Any:
        return self.outputs.get(name)

    def kill_switch_param(self) -> str:
        param = self.output("kill_switch_parameter")
        if not param:
            raise UpgradeError(
                "the Terraform output kill_switch_parameter is missing; deploy first"
            )
        return param

    def kill_switch_value(self) -> str | None:
        try:
            response = self.clients.ssm.get_parameter(Name=self.kill_switch_param())
        except self.clients.ssm.exceptions.ParameterNotFound:
            return None
        return response["Parameter"]["Value"]

    def devops_ids(self) -> tuple[str | None, str | None]:
        space = (
            self.options.agent_space_id
            or os.environ.get("AGENT_SPACE_ID")
            or self.output("devops_agent_space_id")
        )
        association = (
            self.options.association_id
            or os.environ.get("ASSOCIATION_ID")
            or self.output("devops_agent_association_id")
        )
        return space or None, association or None

    def approval_mode(self) -> str:
        return (
            self.output("approval_mode")
            or (self.output("guard_settings") or {}).get("approval_mode")
            or "server"
        )

    # -- run --

    def run(self) -> int:
        self.state = self.start()
        try:
            return self.run_steps()
        except UpgradeError:
            # A run that failed before changing anything leaves no state behind.
            self.abandon_if_unchanged()
            raise
        finally:
            # A resume plans again before it applies, so the saved plan is never reused.
            self.tf.remove_plan()

    def run_steps(self) -> int:
        assert self.state is not None
        steps = {
            "preflight": self.preflight,
            "plan": self.plan,
            "disable_kill_switch": self.disable_kill_switch,
            "migrate": self.migrate,
            "apply": self.apply,
            "k8s": self.k8s,
            "enable_write": self.enable_write,
            "sentinel": self.sentinel,
            "enable_kill_switch": self.enable_kill_switch,
            "guidance": self.guidance,
            "smoke": self.smoke,
        }
        for step in STEPS:
            # The preflight always runs. Until the apply is done, the plan and the steps
            # that depend on it run again on resume: a saved plan may be stale, a new one
            # may change the tool set or need a migration, and the kill switch may have
            # been turned on by hand in between. Each of them is idempotent.
            again = step == "preflight" or (step in BEFORE_APPLY and "apply" not in self.state.done)
            if step in self.state.done and not again:
                continue
            say(f"step {step}")
            steps[step]()
            if step == "plan":
                if self.options.dry_run:
                    self.dry_run_summary()
                    return 0
                if not self.options.yes and not self.confirmed("Apply this plan and upgrade?"):
                    raise UpgradeError("not confirmed; nothing was applied")
            self.done(step)
        self.finish()
        return 0

    def confirmed(self, question: str) -> bool:
        try:
            return self.confirm(question)
        except (KeyboardInterrupt, EOFError):
            print()
            return False

    def start(self) -> State:
        exists = self.state_path.exists()
        if self.options.dry_run:
            return State.new()
        if self.options.resume:
            if not exists:
                raise UpgradeError(f"--resume: no {self.state_path.name}; run without --resume")
            state = State.load(self.state_path)
            say(f"resuming run {state.run_id} after {', '.join(state.done) or 'nothing'}")
            if state.with_k8s and not self.options.with_k8s:
                say("--with-k8s from the run being resumed")
                self.options.with_k8s = True
            state.with_k8s = self.options.with_k8s
            return state
        if exists:
            raise UpgradeError(
                f"{self.state_path.name} exists: an earlier upgrade did not finish. Run with "
                "--resume to continue it, or delete the file if you finished it by hand."
            )
        state = State.new()
        state.with_k8s = self.options.with_k8s
        state.save(self.state_path)
        return state

    def abandon_if_unchanged(self) -> None:
        """Forget a run that has changed nothing yet, so the next one starts fresh."""
        if self.state is None or self.options.dry_run:
            return
        changed = {"disable_kill_switch", "migrate", "apply"} & set(self.state.done)
        if not changed and not self.state.kill_switch_disabled and self.state_path.exists():
            self.state_path.unlink()

    def preflight(self) -> None:
        assert self.state is not None
        self.outputs = self.tf.outputs()
        if "apply" not in self.state.done and self.state.k8s_manifests_before is None:
            self.state.k8s_manifests_before = manifests_hash(self.outputs.get("k8s_manifests"))
        if self.options.with_k8s:
            self.pin_context()
            # Shown before the confirmation, so the operator sees which cluster it is.
            say(f"--with-k8s: build/k8s goes to kubectl context {self.kubectl.context}")
        value = self.kill_switch_value()
        ours = self.state.kill_switch_marker
        if value == "true":
            if self.state.kill_switch_disabled:
                # Turned back on by hand; nothing for this script to restore. Before the
                # apply, disable_kill_switch turns it off again if the tool set changes.
                self.state.kill_switch_disabled = False
                self.state.notes.append("the kill switch was turned on by someone else")
                if "apply" not in self.state.done:
                    say("the kill switch was turned on by hand; it goes off again for the apply")
            return
        if ours is not None and value == ours:
            say("the kill switch is off with this run's marker; continuing")
            return
        if ours is not None and self.state.kill_switch_disabled:
            message = (
                f"the kill switch {self.kill_switch_param()} is {value!r}: something overwrote "
                "this run's marker after this script turned it off (the drift sentinel writes "
                "false on drift; so may an operator). Find out why, turn it on by hand, and "
                "run with --resume"
                + (
                    "; this script turns it off again for the apply."
                    if "apply" not in self.state.done
                    else "."
                )
            )
        else:
            message = (
                f"the kill switch {self.kill_switch_param()} is {value!r}, and this script did "
                "not turn it off. Find out why (a drift alert, an operator) and turn it on by "
                "hand before upgrading."
            )
        if self.options.dry_run:
            say(f"WARNING: {message}")
            return
        raise UpgradeError(message)

    def plan(self) -> None:
        assert self.state is not None
        plan = self.tf.plan()
        report = check_plan(plan)
        self.report = report
        if report.violations:
            raise UpgradeError(
                "the plan deletes or replaces resources that must survive an upgrade:\n  "
                + "\n  ".join(report.violations)
            )
        deployed = self.output("tool_set_hash")
        if deployed:
            changed = deployed != tool_set_hash(self.repo_root)
        else:
            # Deployed before the output existed: fall back to what the plan changes.
            changed = report.tool_set_in_plan
        if self.state.tool_set_changed is None or "apply" not in self.state.done:
            self.state.tool_set_changed = self.state.tool_set_changed or changed
        self.state.targets = list(report.targets)
        for line in report.moves:
            say(f"move {line}")
        for action, addresses in sorted(report.changes.items()):
            say(f"{action}: {len(addresses)}")
        for migration in report.migrations:
            say(f"two-step migration {migration.name}: {migration.description}")
        say(f"tool set changes: {'yes' if self.state.tool_set_changed else 'no'}")

    def dry_run_summary(self) -> None:
        assert self.state is not None and self.report is not None
        space, association = self.devops_ids()
        steps = []
        if self.state.tool_set_changed:
            steps.append("turn the kill switch off")
        if self.state.targets:
            steps.append(f"apply -target {' '.join(self.state.targets)}")
        steps.append("apply")
        if self.options.with_k8s:
            steps.append("kubectl apply build/k8s (a server-side dry run first)")
        if self.state.tool_set_changed:
            steps.append(
                "enable-write"
                if space and association
                else "enable-write (skipped unless the apply adds the DevOps Agent ID outputs)"
            )
        steps.append("invoke the sentinel")
        if self.state.tool_set_changed:
            steps.append("turn the kill switch on")
        steps.append(
            "install-guidance if the guidance differs"
            if space
            else "guidance (skipped unless the apply adds the DevOps Agent ID outputs)"
        )
        steps.append("smoke test" + (" --read-only" if self.approval_mode() != "client" else ""))
        say("dry run: the plan passed the safety check; nothing was changed")
        say("would: " + "; ".join(steps))

    def disable_kill_switch(self) -> None:
        assert self.state is not None
        if not self.state.tool_set_changed:
            return
        if self.state.kill_switch_disabled:
            return
        marker = f"{MARKER_PREFIX}{self.state.run_id}"
        # Recorded before the write: a crash right after it still knows the switch is ours.
        self.state.kill_switch_marker = marker
        self.state.kill_switch_disabled = True
        self.save()
        self.clients.ssm.put_parameter(
            Name=self.kill_switch_param(), Value=marker, Type="String", Overwrite=True
        )
        say(f"kill switch off ({marker}) while the tool set changes")

    def migrate(self) -> None:
        assert self.state is not None
        if not self.state.targets:
            return
        say(f"targeted apply first: {', '.join(self.state.targets)}")
        result = self.tf.apply_targets(self.state.targets)
        if result.returncode != 0:
            raise UpgradeError("the targeted apply failed; fix the cause and run with --resume")
        # The saved plan is stale now, and the new one may change the tool set.
        self.plan()
        self.disable_kill_switch()
        if self.state.targets:
            raise UpgradeError(
                f"still to create after the targeted apply: {', '.join(self.state.targets)}"
            )

    def apply(self) -> None:
        result = self.tf.apply_plan()
        if result.returncode == 0:
            return
        if INCONSISTENT_PLAN not in result.output.lower():
            raise UpgradeError("terraform apply failed; fix the cause and run with --resume")
        say("inconsistent final plan: planning again and applying once more")
        self.plan()
        self.disable_kill_switch()
        if self.tf.apply_plan().returncode != 0:
            raise UpgradeError("terraform apply failed twice; fix the cause and run with --resume")

    def k8s_only(self, outputs: dict[str, Any]) -> int:
        """--k8s-only: the manifests of the deployed state, without a Terraform run."""
        self.state = State.new()
        self.outputs = outputs
        say(f"build/k8s goes to kubectl context {self.kubectl.pin()}")
        if not self.options.yes and not self.confirmed("Apply build/k8s to this context?"):
            raise UpgradeError("not confirmed; nothing was applied")
        self.k8s()
        for note in self.state.notes:
            say(f"note: {note}")
        return 0

    def pin_context(self) -> None:
        """The kubectl context of the run: the one a resumed run used, unless
        --kube-context names another, which is refused."""
        assert self.state is not None
        saved = self.state.kube_context
        if saved and self.kubectl.context and self.kubectl.context != saved:
            raise UpgradeError(
                f"--kube-context {self.kubectl.context}: the run being resumed applies to "
                f"context {saved}; resume without --kube-context or with that one"
            )
        if saved:
            self.kubectl.context = saved
        self.state.kube_context = self.kubectl.pin()

    def k8s(self) -> None:
        """The cluster manifests of this release, after the apply that rendered them."""
        assert self.state is not None
        before = self.state.k8s_manifests_before or manifests_hash(self.output("k8s_manifests"))
        self.outputs = self.tf.outputs()
        manifests = self.output("k8s_manifests")
        if not isinstance(manifests, dict) or not manifests:
            say("WARNING: no k8s_manifests output; the cluster manifests were not checked")
            return
        if not self.options.with_k8s:
            if manifests_hash(manifests) != before:
                say(
                    "WARNING: the cluster manifests changed and were not applied: plans that "
                    "need them are refused (conflict) until they are. Run make upgrade "
                    "UPGRADE_FLAGS=--k8s-only now, or kubectl apply -f build/k8s/ yourself"
                )
                self.state.notes.append("cluster manifests changed: apply build/k8s")
            return
        files = write_manifests(manifests, self.k8s_dir)
        say(f"kubectl apply --dry-run=server of {', '.join(p.name for p in files)} in {K8S_DIR}")
        if self.kubectl.apply(files, dry_run=True).returncode != 0:
            raise UpgradeError(
                "the server-side dry run of build/k8s failed, nothing was applied to the "
                "cluster: fix the cause (a missing namespace, RBAC) and run with --resume "
                "(it keeps --with-k8s)"
            )
        if self.kubectl.apply(files, dry_run=False).returncode != 0:
            raise UpgradeError(
                "kubectl apply of build/k8s failed; fix the cause and run with --resume "
                "(it keeps --with-k8s)"
            )
        mode = (self.output("guard_settings") or {}).get("admission_mode", "Deny")
        expected = ["Deny"] if mode == "Deny" else ["Warn", "Audit"]
        for binding in BINDINGS:
            actions = self.kubectl.binding_actions(binding)
            if actions != expected:
                raise UpgradeError(
                    f"the admission policy binding {binding} has validationActions {actions}, "
                    f"not {expected} after the apply; check it with kubectl and run with --resume"
                )
            say(f"admission policy binding {binding}: {actions}")
        labeled = self.kubectl.labeled_roles()
        if labeled is None:
            say("note: could not list kube-handrail Roles cluster-wide; stale ones not checked")
            return
        stale = sorted(set(labeled) - rendered_roles(manifests))
        for entry in stale:
            namespace, kind, name = entry.split("/")
            say(
                f"WARNING: {kind} {namespace}/{name} is no longer rendered (a namespace removed, "
                f"or its actions narrowed); delete it: kubectl delete {kind.lower()} {name} -n {namespace}"
            )
        if stale:
            self.state.notes.append(f"{len(stale)} stale kube-handrail Role(s) or binding(s)")

    def enable_write(self) -> None:
        assert self.state is not None
        self.outputs = self.tf.outputs()
        if not self.state.tool_set_changed:
            return
        space, association = self.devops_ids()
        if not (space and association):
            say(
                "WARNING: the tool set changed but no DevOps Agent IDs are known; run enable-write yourself"
            )
            self.state.notes.append("enable-write skipped: no DevOps Agent IDs")
            return
        args = argparse.Namespace(agent_space_id=space, association_id=association)
        try:
            self.register.enable_write(self.clients.devops(), args)
        except SystemExit as exc:
            raise UpgradeError(f"enable-write failed: {exc}") from None

    def sentinel(self) -> None:
        assert self.state is not None
        name = self.output("sentinel_function_name")
        if not name:
            say("WARNING: no sentinel deployed (sentinel_enabled = false); nothing checks drift")
            self.state.sentinel = "absent"
            return
        status = None
        for attempt in range(1, SENTINEL_ATTEMPTS + 1):
            response = self.clients.lambda_.invoke(
                FunctionName=name, InvocationType="RequestResponse"
            )
            body = response["Payload"].read()
            if response.get("FunctionError"):
                raise UpgradeError(f"the sentinel failed: {body[:300]!r}")
            status = (json.loads(body or b"{}") or {}).get("status")
            say(f"sentinel: {status}")
            if status != "unknown" or attempt == SENTINEL_ATTEMPTS:
                break
            self.sleep(SENTINEL_RETRY_SECONDS)
        self.state.sentinel = status
        if status != SENTINEL_OK:
            raise UpgradeError(
                f"the sentinel reported {status!r}, not ok; remediation stays off. Read its "
                "log and the drift alert, fix the cause and run with --resume"
            )

    def enable_kill_switch(self) -> None:
        assert self.state is not None
        if not self.state.kill_switch_disabled:
            return
        value = self.kill_switch_value()
        if value != self.state.kill_switch_marker:
            raise UpgradeError(
                f"the kill switch is now {value!r}: something overwrote this run's marker after "
                "this script turned it off (a drift sentinel of an earlier release writes false "
                "on drift; so may an operator). It stays as it is: find out why and turn it on "
                "by hand."
            )
        self.clients.ssm.put_parameter(
            Name=self.kill_switch_param(), Value="true", Type="String", Overwrite=True
        )
        self.state.kill_switch_disabled = False
        say("kill switch on again")

    def guidance(self) -> None:
        assert self.state is not None
        space, _ = self.devops_ids()
        if not space:
            say("WARNING: no agent space ID; the guidance was not checked or installed")
            self.state.notes.append("guidance skipped: no agent space ID")
            return
        client = self.clients.devops()
        try:
            differs = guidance_differs(self.register, client, space, self.options.agent_type)
            if differs:
                say(f"guidance differs ({', '.join(differs)}): install-guidance")
                args = argparse.Namespace(
                    agent_space_id=space,
                    agent_type=self.options.agent_type,
                    replace_instructions=False,
                )
                self.register.install_guidance(client, args)
            else:
                say("guidance is current")
            status = self.register.guidance_status(client, space)
        except SystemExit as exc:
            raise UpgradeError(f"install-guidance failed: {exc}") from None
        for asset in status["agentsMd"]:
            say(f"AGENTS.md ({asset['agentType']}): version {asset['version']}")
        for asset in status["skills"]:
            say(f"skill {asset['name']}: version {asset['version']}")
        if differs:
            say(
                f"WARNING: sessions started before now may use the old guidance for about "
                f"{STALE_SKILL_MINUTES} minutes; start a new chat to get the new one"
            )

    def smoke_env(self) -> dict[str, str]:
        ids = self.output("agent_client_ids") or {}
        client_id = os.environ.get("CLIENT_ID") or ids.get("smoke_test")
        if not client_id:
            raise UpgradeError("no smoke_test agent: set CLIENT_ID and CLIENT_SECRET")
        secret = os.environ.get("CLIENT_SECRET")
        if not secret:
            pool = self.output("cognito_user_pool_id")
            if not pool:
                raise UpgradeError(
                    "set CLIENT_SECRET, or apply first so cognito_user_pool_id exists"
                )
            secret = self.clients.cognito().describe_user_pool_client(
                UserPoolId=pool, ClientId=client_id
            )["UserPoolClient"]["ClientSecret"]
        env = {
            "GATEWAY_URL": self.output("gateway_url") or "",
            "TOKEN_URL": self.output("cognito_token_endpoint") or "",
            "SCOPE": " ".join(self.output("cognito_scopes") or []),
            "CLIENT_ID": client_id,
            "CLIENT_SECRET": secret,
        }
        if self.options.smoke_namespace:
            env["NAMESPACE"] = self.options.smoke_namespace
        if self.options.smoke_name:
            env["NAME"] = self.options.smoke_name
        return env

    def smoke(self) -> None:
        if self.options.skip_smoke:
            say("smoke test skipped (--skip-smoke)")
            return
        command = [self.options.python, str(self.repo_root / "scripts/smoke_test.py")]
        if self.approval_mode() != "client":
            command.append("--read-only")
        if self.smoke_runner(command, self.smoke_env()) != 0:
            raise UpgradeError("the smoke test failed; fix the cause and run with --resume")

    @staticmethod
    def _run_smoke(command: list[str], env: dict[str, str]) -> int:
        return subprocess.run(command, env={**os.environ, **env}, check=False).returncode

    def finish(self) -> None:
        assert self.state is not None
        if self.state_path.exists():
            self.state_path.unlink()
        for note in self.state.notes:
            say(f"note: {note}")
        say(f"upgrade finished in {int(time.time() - self.state.started_at)}s")


# --- CLI ------------------------------------------------------------------------------


def region_from(outputs: Mapping[str, Any]) -> str | None:
    """The Region in the Cognito token endpoint (https://<domain>.auth.<region>.amazoncognito.com)."""
    match = re.search(
        r"\.auth\.([a-z0-9-]+)\.amazoncognito\.com", outputs.get("cognito_token_endpoint") or ""
    )
    return match.group(1) if match else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="stop after the plan safety check")
    parser.add_argument(
        "--resume", action="store_true", help=f"continue the run in {STATE_FILE.name}"
    )
    parser.add_argument("--tf-dir", type=Path, default=REPO_ROOT / "terraform")
    parser.add_argument(
        "--var-file",
        action="append",
        default=[],
        help="passed to plan and to a targeted apply, relative to --tf-dir; repeatable",
    )
    parser.add_argument(
        "--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    )
    parser.add_argument("--agent-space-id")
    parser.add_argument("--association-id")
    parser.add_argument("--agent-type", default=GENERIC, help="agent type of the AGENTS.md asset")
    parser.add_argument("--yes", action="store_true", help="apply the checked plan without asking")
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--smoke-namespace", default=os.environ.get("NAMESPACE"))
    parser.add_argument("--smoke-name", default=os.environ.get("NAME"))
    parser.add_argument(
        "--smoke-only", action="store_true", help="run only the smoke test (make smoke)"
    )
    parser.add_argument(
        "--with-k8s",
        action="store_true",
        help="after the apply, kubectl apply the cluster manifests (build/k8s), dry run first",
    )
    parser.add_argument(
        "--k8s-only",
        action="store_true",
        help="only write and apply the cluster manifests of the deployed state (dry run first)",
    )
    parser.add_argument(
        "--kube-context", help="kubectl context for --with-k8s (default: the current one)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import boto3  # noqa: PLC0415

    terraform = Terraform(args.tf_dir.resolve(), args.var_file)
    try:
        outputs = terraform.outputs()
    except UpgradeError as exc:
        print(f"upgrade: {exc}", file=sys.stderr)
        return 1
    region = args.region or region_from(outputs)
    clients = Clients(
        ssm=boto3.client("ssm", region_name=region),
        lambda_=boto3.client("lambda", region_name=region),
        devops=lambda: boto3.client("devops-agent", region_name=region),
        cognito=lambda: boto3.client("cognito-idp", region_name=region),
    )
    options = Options(
        dry_run=args.dry_run,
        resume=args.resume,
        agent_space_id=args.agent_space_id,
        association_id=args.association_id,
        agent_type=args.agent_type,
        skip_smoke=args.skip_smoke,
        smoke_namespace=args.smoke_namespace,
        smoke_name=args.smoke_name,
        yes=args.yes,
        with_k8s=args.with_k8s or args.k8s_only,
    )
    upgrade = Upgrade(terraform, clients, options, kubectl=Kubectl(args.kube_context))
    try:
        if args.smoke_only:
            upgrade.outputs = outputs
            upgrade.smoke()
            return 0
        if args.k8s_only:
            return upgrade.k8s_only(outputs)
        return upgrade.run()
    except UpgradeError as exc:
        print(f"upgrade: {exc}", file=sys.stderr)
        if upgrade.state is not None and not args.dry_run and upgrade.state_path.exists():
            print(
                f"upgrade: progress is in {upgrade.state_path.name}; run again with --resume",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
