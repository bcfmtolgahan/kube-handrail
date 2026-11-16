# Day-to-day commands (docs/deploy.md). Variables: PYTHON (default .venv/bin/python),
# TF_DIR (default terraform), UPGRADE_FLAGS (for example --dry-run, --resume, --yes),
# CLEANUP_FLAGS (scripts/cleanup.sh flags), MCP_NAME (register).

PYTHON ?= .venv/bin/python
TF_DIR ?= terraform
UPGRADE_FLAGS ?=
CLEANUP_FLAGS ?=
MCP_NAME ?= kube-handrail
TF := terraform -chdir=$(TF_DIR)

.PHONY: help bootstrap deploy register upgrade smoke destroy integration

help:
	@echo "make bootstrap   create .venv, install the requirements, terraform init"
	@echo "make deploy      terraform apply (first install; afterwards use make upgrade)"
	@echo "make register    register the gateway with DevOps Agent, read tools only"
	@echo "                 (AGENT_SPACE_ID, CLIENT_ID, CLIENT_SECRET of devops_agent)"
	@echo "make upgrade     scripts/upgrade.py \$$(UPGRADE_FLAGS), e.g. UPGRADE_FLAGS=--dry-run"
	@echo "                 UPGRADE_FLAGS=--with-k8s also applies build/k8s with kubectl"
	@echo "make smoke       the smoke test (--read-only in server and both mode)"
	@echo "make destroy     scripts/cleanup.sh \$$(CLEANUP_FLAGS)"
	@echo "make integration the kind and moto integration suite on a throwaway kind cluster"

bootstrap:
	test -x $(PYTHON) || python3 -m venv .venv
	$(PYTHON) -m pip install --quiet -r requirements-dev.txt
	$(TF) init

deploy:
	$(TF) apply

# Then check the read path in chat, run enable-write and set devops_agent_space_id and
# devops_agent_association_id (docs/deploy.md, Order of steps).
register:
	@test -n "$$AGENT_SPACE_ID" || { echo "set AGENT_SPACE_ID (devops_agent_register.py create-space)"; exit 2; }
	@test -n "$$CLIENT_ID" -a -n "$$CLIENT_SECRET" || { echo "set CLIENT_ID and CLIENT_SECRET of the devops_agent client"; exit 2; }
	set -e; \
	  service=$$(GATEWAY_URL="$$($(TF) output -raw gateway_url)" \
	    TOKEN_URL="$$($(TF) output -raw cognito_token_endpoint)" \
	    $(PYTHON) scripts/devops_agent_register.py register-mcp --name "$(MCP_NAME)" \
	      --scope "$$($(TF) output -json cognito_scopes | $(PYTHON) -c "import json,sys; print(\" \".join(json.load(sys.stdin)))")" \
	    | $(PYTHON) -c "import json,sys; print(json.load(sys.stdin)[\"serviceId\"])"); \
	  echo "serviceId: $$service"; \
	  $(PYTHON) scripts/devops_agent_register.py associate-read --service-id "$$service"

upgrade:
	$(PYTHON) scripts/upgrade.py --tf-dir $(TF_DIR) $(UPGRADE_FLAGS)

smoke:
	$(PYTHON) scripts/upgrade.py --tf-dir $(TF_DIR) --smoke-only

destroy:
	TF_DIR=$(TF_DIR) PYTHON=$(PYTHON) scripts/cleanup.sh $(CLEANUP_FLAGS)

# Needs docker, kind and terraform; never touches AWS or the default kubeconfig
# (tests/integration/README.md).
integration:
	PYTHON=$(PYTHON) tests/integration/run.sh
