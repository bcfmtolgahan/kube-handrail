# Releasing

How kube-handrail is versioned and released, and what each release supports.

## Versions

Releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), with tags `vMAJOR.MINOR.PATCH` and an entry in [CHANGELOG.md](../CHANGELOG.md). Until 1.0.0:

- **Minor** (0.y.0): anything an operator or agent must act on: a behaviour change of a guard, a Terraform variable or output that is removed or changes meaning, a new required step, a tool schema change, or an `AGENTS.md` or skill change.
- **Patch** (0.y.z): fixes that keep every interface: no tool schema, Terraform variable or guidance change, and no guard that refuses something it allowed before.

From 1.0.0, a removed variable, tool, argument or error code, or a guard that allows what it refused before, needs a major version.

The contract (`mcp/CONTRACT.md`), the tool schemas (`mcp/*/tools.json`), the error codes, the Terraform variables and outputs, and the plan record are the public interface. Something deprecated stays for at least one minor release, says so in the changelog's Deprecated section, and, for a Terraform variable, warns through a `check` block while it is set.

Releases before the changelog were not tagged; the changelog summarizes them by milestone.

## Supported versions

| Component | Supported | Where it is set |
|---|---|---|
| Kubernetes (EKS) | 1.30 or later for the admission policies (ValidatingAdmissionPolicy v1); the tools use `apps/v1`, `v1` and `autoscaling/v2`. CI tests on 1.31, with metrics-server v0.7.2. | `terraform/modules/namespace-policy`, `.github/workflows/ci.yml` |
| EKS authentication mode | `API` or `API_AND_CONFIG_MAP` | checked at plan time |
| Terraform | 1.5.0 or later | `terraform/versions.tf` |
| AWS provider | `~> 6.70` (lock file: 6.70.0) | `terraform/versions.tf`, `terraform/.terraform.lock.hcl` |
| archive and null providers | `~> 2.7` (2.8.1) and `~> 3.2` (3.3.2) | as above |
| Lambda runtime | Python 3.12, x86_64 | `terraform/main.tf` |
| Kubernetes Python client | 35.0.0 | `requirements.txt` |
| AWS DevOps Agent, AgentCore Gateway | the Region must offer both | [deploy](deploy.md#prerequisites) |

A cluster below 1.30 runs everything except the admission policy ([deploy](deploy.md#admission-policy)).

## One tool schema batch per release

Tool schema changes (a new tool, a new or changed argument, a changed tool description) ship at most once per release, together.

Why: every schema change costs the operator a sequence that takes effect only in full:

1. **`enable-write`**: the gateway's tool set changes before DevOps Agent's association knows it, so the drift sentinel reports drift and turns remediation off until `scripts/devops_agent_register.py enable-write` has written every tool's classification again.
2. **Sentinel**: the operator waits for a sentinel run that reports `ok`, then turns the kill switch back on by hand.
3. **`install-guidance`**: agents need the new `AGENTS.md` and skills to use the new arguments.
4. **Stale-skill window**: a DevOps Agent chat can keep reading the previous skill version for about half an hour after `install-guidance` (observed on a test deployment), so for that time agents may call the new schema with old instructions.

`make upgrade` ([deploy](deploy.md#upgrading)) runs these steps in order: it turns the kill switch off before such an apply and on again after a sentinel `ok`. Two schema changes in two releases mean two outages of remediation and two stale windows; one batch means one. A change that does not touch `tools.json` (a guard, a Terraform default, the contract text) needs none of this, and can ship in any release. When a fix would need a schema change, for example a tool description that has gone out of date, it waits for the next batch and the changelog says so.

## Release checklist

1. CI is green: `scripts/check_contract.py`, `ruff check`, `ruff format --check`, `pytest tests/unit`, `terraform fmt -check -recursive`, `terraform validate`, the admission job and the integration job.
2. The changelog's Unreleased section lists every change, says whether `enable-write` and `install-guidance` are needed, and points to the upgrade notes in [deploy](deploy.md#upgrading).
3. Behaviour changes have run on a test deployment (`terraform/test.tfvars.example`, `tests/e2e/e2e_gateway.py`, and a chat in `both` mode).
4. Rename Unreleased to the version and date, set `__version__` and `pyproject.toml` to it, tag `vX.Y.Z`, and reopen Unreleased.
