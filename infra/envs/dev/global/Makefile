SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c
ROOT := $(abspath ../../../..)
VARS ?= terraform.tfvars
PLAN ?= reviewed.tfplan
PROPOSAL ?= proposal.json
DESTROY_PLAN ?= destroy.tfplan
REVIEWED_PLAN_SHA256 ?=
BACKEND ?=
.PHONY: init plan reserve up down-plan down
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

# Plan only this root; retained bootstrap/backend is not in this state.
down-plan: init
	cd $(ROOT)
	source scripts/dev-env.sh
	terraform -chdir="$(CURDIR)" plan -destroy -input=false -var-file="$(VARS)" -out="$(DESTROY_PLAN)"
down:
	cd $(ROOT)
	source scripts/dev-env.sh
	python3 scripts/azure_teardown.py --plan "$(abspath $(DESTROY_PLAN))" --reviewed-sha256 "$(REVIEWED_PLAN_SHA256)"
	terraform -chdir="$(CURDIR)" apply -input=false "$(DESTROY_PLAN)"
