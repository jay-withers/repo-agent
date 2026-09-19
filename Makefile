TF_DIR := terraform
ENV ?= dev

IMAGE_REGISTRY ?= ghcr.io/jay-withers/repo-agent
# Defaults to the local commit, which is what you want when iterating: build,
# push, and the tag you just built is the one you reference.
IMAGE_TAG ?= $(shell git rev-parse --short HEAD)
# `file` means the ?= default fired rather than the caller passing one.
IMAGE_TAG_EXPLICIT := $(filter-out file,$(origin IMAGE_TAG))

.DEFAULT_GOAL := help

.PHONY: help install lint test run build push deploy start logs state init fmt validate plan apply secrets

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# Expected to be re-run after a dev container rebuild, not just after a clone:
# uv installs into ~/.local/bin, which is the container's writable layer and
# does not survive one.
install: ## Install pre-commit hooks and Python dependencies
	pre-commit install
	pre-commit install --hook-type commit-msg
	command -v uv >/dev/null || curl -fsSL https://astral.sh/uv/install.sh | sh
	uv sync --extra dev

test: ## Run the test suite
	# --extra dev: pytest is an extra, not a dependency group, so `uv run` does
	# not install it. Without this the target only works after `make install`.
	uv run --extra dev pytest

lint: ## Run every pre-commit hook against every file
	pre-commit run --all-files

# Reads real GitHub and prints the digest without sending it. Needs
# GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY in the environment or a .env —
# settings.py reads env first, so no Azure is involved.
run: ## Scan and print the digest locally, sending no email
	uv run repoagent render

build: ## Build the image for linux/amd64 (set IMAGE_TAG, defaults to the git SHA)
	docker buildx build --platform linux/amd64 --load \
		-t $(IMAGE_REGISTRY)/repoagent:$(IMAGE_TAG) .

push: ## Push the image to ghcr.io (needs write:packages)
	gh auth token | docker login ghcr.io -u $$(gh api user --jq .login) --password-stdin
	docker push $(IMAGE_REGISTRY)/repoagent:$(IMAGE_TAG)

# A bare `make deploy` is a hard error, unlike build/push. Those default the tag
# to the local git SHA, which is what you want when iterating. Deploying is
# different: the default would silently roll the job onto whatever commit
# happens to be checked out, which may never have been pushed to ghcr.io at all.
deploy: ## Roll an image tag onto the job (IMAGE_TAG required)
	@if [ -z "$(IMAGE_TAG_EXPLICIT)" ]; then \
		echo "error: pass a tag explicitly, e.g. make deploy IMAGE_TAG=v0.1.0" >&2; exit 1; fi
	@case "$(IMAGE_TAG)" in latest|main|unset) \
		echo "error: $(IMAGE_TAG) is a moving tag. Container Apps only creates a revision when the template changes, so re-pushing one deploys nothing and reports success." >&2; exit 1;; esac
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	az containerapp job update \
		--name "$$(terraform -chdir=$(TF_DIR) output -raw scan_job_name)" \
		--resource-group "$$(terraform -chdir=$(TF_DIR) output -raw resource_group_name)" \
		--image $(IMAGE_REGISTRY)/repoagent:$(IMAGE_TAG) \
		--set-env-vars IMAGE_TAG=$(IMAGE_TAG) \
		STATE_CONTAINER_URL="$$(terraform -chdir=$(TF_DIR) output -raw state_container_url)"

# Reads and writes the real state document, so it needs Storage Blob Data
# Contributor on the container — which whoever applied the Terraform has.
state: ## Print what the scan remembers between runs
	STATE_CONTAINER_URL="$$(terraform -chdir=$(TF_DIR) output -raw state_container_url)" \
		uv run repoagent state

# A scheduled job has no other way to be triggered.
start: ## Trigger one scan execution now
	az containerapp job start \
		--name "$$(terraform -chdir=$(TF_DIR) output -raw scan_job_name)" \
		--resource-group "$$(terraform -chdir=$(TF_DIR) output -raw resource_group_name)"

logs: ## Tail the deployed job's most recent execution logs
	az containerapp job logs show \
		--name "$$(terraform -chdir=$(TF_DIR) output -raw scan_job_name)" \
		--resource-group "$$(terraform -chdir=$(TF_DIR) output -raw resource_group_name)" \
		--container scan --follow

secrets: ## Print the az commands that populate this project's Key Vault
	@echo "az keyvault secret set --vault-name $$(terraform -chdir=$(TF_DIR) output -raw key_vault_name) --name GITHUB-APP-ID --value <app id>"
	@echo "az keyvault secret set --vault-name $$(terraform -chdir=$(TF_DIR) output -raw key_vault_name) --name GITHUB-APP-PRIVATE-KEY --file <path to .pem>"
	@echo "az keyvault secret set --vault-name $$(terraform -chdir=$(TF_DIR) output -raw key_vault_name) --name RESEND-API-KEY --value <resend key>"
	@echo "az keyvault secret set --vault-name $$(terraform -chdir=$(TF_DIR) output -raw key_vault_name) --name DIGEST-EMAIL-TO --value <address>"
	@echo "az keyvault secret set --vault-name $$(terraform -chdir=$(TF_DIR) output -raw key_vault_name) --name DEEPSEEK-API-KEY --value <deepseek key>"

init: ## terraform init, without configuring the state backend
	terraform -chdir=$(TF_DIR) init -backend=false

fmt: ## terraform fmt -recursive
	terraform -chdir=$(TF_DIR) fmt -recursive

validate: init ## terraform init + validate (no Azure credentials needed)
	terraform -chdir=$(TF_DIR) validate

plan: ## terraform init + plan
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	terraform -chdir=$(TF_DIR) plan -var-file=environments/$(ENV).tfvars

apply: ## terraform init + apply
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	terraform -chdir=$(TF_DIR) apply -var-file=environments/$(ENV).tfvars
