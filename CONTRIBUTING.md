# Contributing

Thanks for helping. kube-handrail gives AI agents write access to clusters, so changes are judged first by what they do to the guards.

## Before you start

- **Bugs and ideas:** open an issue with the templates. For a change of behaviour, a new tool or a new guard, open an issue first so the design can be agreed before the code.
- **Security issues:** never in a public issue; see [SECURITY.md](SECURITY.md).
- **Scope:** read [ADR 0001](docs/adr/0001-scope.md). Generic read tools, delete-class tools and a generic `apply_yaml` are out of scope.

## Development setup

Python 3.12, Terraform 1.5 or later; Docker and kind for the integration suite.

```sh
make bootstrap                      # .venv, requirements, terraform init
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest tests/unit
.venv/bin/python scripts/check_contract.py
terraform -chdir=terraform fmt -check -recursive && terraform -chdir=terraform validate
make integration                    # kind + moto, never touches AWS
```

CI runs the same checks on every pull request, plus the admission policy cases on kind. The end-to-end tests against a deployment are in [Testing](docs/testing.md).

## Pull requests

- One change per pull request, with tests. A guard change needs unit tests for both the refusal and the allowed case.
- `mcp/CONTRACT.md` is the binding definition: update it, and `AGENTS.md` and the skills when agents must act differently, in the same pull request. `scripts/check_contract.py` keeps the tool and error tables in sync.
- Add a line to the Unreleased section of [CHANGELOG.md](CHANGELOG.md), saying whether `enable-write` or `install-guidance` is needed.
- Tool schema changes (`mcp/*/tools.json`) ship once per release, together ([Releasing](docs/releasing.md#one-tool-schema-batch-per-release)).
- Docs have word ceilings (`tests/unit/test_guidance_sync.py`): lower one freely; raise one only with a reason.
- No account IDs, ARNs, hostnames or other values from a real environment in code, tests or docs; use `111122223333` and `example.com`.

By contributing, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).
