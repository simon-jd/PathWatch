SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c
ROOT := $(abspath ../..)
VARS ?= terraform.tfvars
PLAN ?= reviewed.tfplan
PROPOSAL ?= proposal.json
BACKEND ?=
.PHONY: init plan reserve up down
init:
	cd $(ROOT)
	source scripts/dev-env.sh
	terraform -chdir="$(CURDIR)" init -input=false -lockfile=readonly $(if $(BACKEND),-backend-config="$(BACKEND)")
plan: init
	cd $(ROOT)
	source scripts/dev-env.sh
	terraform -chdir="$(CURDIR)" plan -input=false -var-file="$(VARS)" -out="$(PLAN)"
reserve:
	cd $(ROOT)
	source scripts/dev-env.sh
	python3 scripts/azure_budget.py reserve --plan "$(CURDIR)/$(PLAN)" --proposal "$(CURDIR)/$(PROPOSAL)"
up:
	cd $(ROOT)
	source scripts/dev-env.sh
	python3 scripts/azure_budget.py check --plan "$(CURDIR)/$(PLAN)" --proposal "$(CURDIR)/$(PROPOSAL)"
	terraform -chdir="$(CURDIR)" apply -input=false "$(PLAN)"

# This root owns the retained backend; never delete it through ENV-routed down.
down:
	@echo 'REFUSED: bootstrap owns retained state. Review/export/migrate state first; whole owned-RG deletion is only explicit make azure-down.' >&2
	@exit 2
