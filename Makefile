SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

# Every toolchain and cache lives inside this checkout (see scripts/dev-env.sh).
ROOT := $(CURDIR)
export PATH := $(ROOT)/.tools/bin:$(ROOT)/.tools/go/bin:$(ROOT)/.tools/rust/bin:$(ROOT)/.tools/py/bin:$(ROOT)/.tools/checkov/bin:$(PATH)
export GOTOOLCHAIN := local
export GOFLAGS := -mod=readonly
export GOPATH := $(ROOT)/.cache/gopath
export GOCACHE := $(ROOT)/.cache/go-build
export GOMODCACHE := $(ROOT)/.cache/go-mod
export CARGO_HOME := $(ROOT)/.cache/cargo
export CARGO_TARGET_DIR := $(ROOT)/.cache/cargo-target
export UV_CACHE_DIR := $(ROOT)/.cache/uv
export UV_PYTHON_INSTALL_DIR := $(ROOT)/.tools/python
export TF_PLUGIN_CACHE_DIR := $(ROOT)/.cache/terraform-providers
export TFLINT_PLUGIN_DIR := $(ROOT)/.cache/tflint-plugins
export CHECKPOINT_DISABLE := 1
export PYTHONDONTWRITEBYTECODE := 1

PY := $(ROOT)/.tools/py/bin/python
PYTHON_VERSION := 3.12
# Python unittest suites; each directory is discovered on its own so imports never leak between them.
PY_SUITES := scripts/tests bench/tests drills/tests slo/tests infra/modules/clickhouse deploy/tests lab/tests
CARGO_WORKSPACES := proxy xdp

.PHONY: help tools tools-checkov versions fmt test test-go test-rust test-python test-tf test-proto check lint \
	infra-check up down azure-down azure-reap probe-test bench-build bench-matrix proto-generate proto-check \
	kind-up kind-images kind-status kind-down kind-publish kind-drill kind-test deploy-check

help:
	@echo 'make tools       install pinned toolchains into .tools/ (Go, Python venv, terraform, tflint)'
	@echo 'make test        run every offline test suite (Go, Rust, Python, Terraform, proto)'
	@echo 'make check       lint and typecheck: gofmt, go vet, cargo fmt/clippy, terraform fmt'
	@echo 'opt-in suites:   PATHWATCH_TEST_DOCKER=1 (ClickHouse testcontainers), cloud tests use their own PATHWATCH_* switches'

tools:
	python3 scripts/bootstrap-tools.py
	uv venv --allow-existing --python $(PYTHON_VERSION) --managed-python .tools/py
	uv pip install --python $(PY) pyyaml==6.0.3
	go mod download

# checkov is only needed by infra-check; kept separate because the venv is large.
tools-checkov:
	uv venv --allow-existing --python $(PYTHON_VERSION) --managed-python .tools/checkov
	uv pip sync --python .tools/checkov/bin/python --require-hashes scripts/checkov-requirements.lock

versions:
	go version; cargo --version; $(PY) --version; terraform version; tflint --version

# ---- tests -----------------------------------------------------------------

test: test-go test-rust test-python test-tf test-proto
	@echo 'make test: all offline suites passed'

# The ClickHouse integration package needs Docker and a pinned image; it only runs with PATHWATCH_TEST_DOCKER=1.
GO_DOCKER_PKGS := ./ingest/internal/server
test-go:
	pkgs=$$(go list ./... | grep -v -x -F "$$(go list $(GO_DOCKER_PKGS))")
	go test -count=1 -shuffle=on $$pkgs
	# The canary drill's deliberately broken frontend build (deploy/README.md).
	go test -count=1 -tags pathwatch_lab_bad500 -run 'TestLabBad500Build' ./ingest/internal/queryapi
	if [[ "$${PATHWATCH_TEST_DOCKER:-}" == 1 ]]; then go test -count=1 -timeout 20m $(GO_DOCKER_PKGS); \
	else echo 'SKIP $(GO_DOCKER_PKGS): set PATHWATCH_TEST_DOCKER=1 to run ClickHouse testcontainers'; fi

# One package, verbose; e.g. make go-test-one PKG=./probe/internal/traceprobe RUN=RecvErr
go-test-one:
	go test -count=1 -v -run '$(RUN)' $(PKG)

test-rust:
	for ws in $(CARGO_WORKSPACES); do cargo test --offline --locked --manifest-path $$ws/Cargo.toml; done

test-python:
	for suite in $(PY_SUITES); do
		echo "== $$suite"
		(cd $$suite && $(PY) -m unittest discover -t . -s . -p '*test*.py')
	done

# terraform test runs only with mock providers / plan; nothing is applied.
test-tf:
	bash scripts/infra-check.sh fmt
	bash scripts/infra-check.sh tftest

test-proto:
	bash scripts/generate-proto.sh --check

# ---- lint / typecheck --------------------------------------------------------

check lint:
	test -z "$$(gofmt -l probe ingest proto infra bench deploy 2>/dev/null)" || { gofmt -l probe ingest proto infra bench deploy; exit 1; }
	go vet ./...
	for ws in $(CARGO_WORKSPACES); do cargo fmt --all --manifest-path $$ws/Cargo.toml --check; \
		cargo clippy --offline --locked --manifest-path $$ws/Cargo.toml --all-targets -- -D warnings; done
	terraform fmt -check -recursive infra

fmt:
	gofmt -w probe ingest proto infra bench deploy
	for ws in $(CARGO_WORKSPACES); do cargo fmt --all --manifest-path $$ws/Cargo.toml; done
	terraform fmt -recursive infra

infra-check:
	for check in fmt validate tflint checkov tftest fmt-selftest; do
		bash scripts/infra-check.sh "$$check"
	done

# ---- Azure (cost guards: ledger, TTL, reaper, rg-pathwatch-lab only) ----

up:
	@case "$(ENV)" in infra/bootstrap|infra/envs/dev/global|infra/envs/dev/azure-a|infra/envs/dev/azure-b|infra/envs/dev/azure-hybrid) ;; *) echo 'Use ENV=infra/bootstrap|infra/envs/dev/{global,azure-a,azure-b,azure-hybrid}'; exit 2 ;; esac
	$(MAKE) -C "$(ENV)" up

down:
	@case "$(ENV)" in infra/bootstrap|infra/envs/dev/global|infra/envs/dev/azure-a|infra/envs/dev/azure-b|infra/envs/dev/azure-hybrid) ;; *) echo 'Use ENV=infra/bootstrap|infra/envs/dev/{global,azure-a,azure-b,azure-hybrid}'; exit 2 ;; esac
	$(MAKE) -C "$(ENV)" down

azure-reap:
	bash scripts/azure_reaper.sh reap

azure-down:
	bash scripts/azure_reaper.sh down

# ---- single components -------------------------------------------------------

probe-test:
	go test ./probe/... ./proto/...

# Task 13 benchmark (bench/README.md). Tools come from bench/setup.sh in .cache/task13.
bench-build:
	cargo build --offline --locked --release --manifest-path proxy/Cargo.toml -p edge-proxy

# One protocol, 4 rates x 5 rounds x 120 s; e.g. make bench-matrix PROTOCOL=h2 PROXY=pathwatch OUTPUT=name
# ROUNDS=0,1,2 runs a subset of rounds (each keeps its proxy rotation).
bench-matrix: bench-build
	$(PY) bench/run.py --matrix --protocol $(PROTOCOL) $(if $(PROXY),--proxy $(PROXY)) $(if $(ROUNDS),--rounds $(ROUNDS)) --output $(OUTPUT)

# Short H2 single-variable diagnostic (30 s cells); e.g. make bench-diag VARIANTS=pathwatch,pathwatch-w1,nginx OUTPUT=name
bench-diag: bench-build
	$(PY) bench/run.py --diag $(VARIANTS) $(if $(RATES),--diag-rates $(RATES)) $(if $(DIAG_ROUNDS),--diag-rounds $(DIAG_ROUNDS)) --output $(OUTPUT)
	$(PY) bench/analyze.py --diag docs/evidence/raw/task13/$(OUTPUT) --output docs/evidence/raw/task13/$(OUTPUT)-summary.json | tee docs/evidence/raw/task13/$(OUTPUT)-summary.md

# Task 9 path-change lab (lab/pathchange/lab.py): unprivileged UDP/IP_RECVERR probe, spool in .cache/task09/spool.
t9-probe-bin:
	CGO_ENABLED=0 go build -trimpath -o .cache/task09/probe ./probe/cmd/probe
t9-up t9-start t9-switch t9-status t9-down:
	$(PY) lab/pathchange/lab.py $(subst t9-,,$@)
# Replays the recorded spool through store.Commit and the anomaly job (ClickHouse testcontainer).
t9-replay:
	cd ingest/internal/server && PATHWATCH_TEST_DOCKER=1 PATHWATCH_T9_SPOOL=$(ROOT)/.cache/task09/spool PATHWATCH_T9_OUT=$(ROOT)/$(OUT) go test -count=1 -v -run TestAnomaly_ReplayRecordedPathChange .

# Task 14 local lab (deploy/README.md): kind cluster pathwatch-canary, kubeconfig .cache/kind/kubeconfig.
KIND_KUBECONFIG := $(ROOT)/.cache/kind/kubeconfig
kind-logs:
	$(PY) deploy/kind/lab.py logs $(NS) $(TARGET) $(CONTAINER) --tail $(or $(TAIL),40)
kind-netpol:
	$(PY) deploy/kind/lab.py netpol
kind-up kind-images kind-status kind-diag kind-wait kind-replace-core kind-down:
	$(PY) deploy/kind/lab.py $(subst kind-,,$@)
kind-publish:
	$(PY) deploy/kind/lab.py publish --variant $(or $(VARIANT),good)
kind-drill:
	$(PY) deploy/kind/lab.py drill --variant $(VARIANT)
# Delete the frontend pod that holds querygen's Report connection N times (default 5); OUT= keeps the run elsewhere.
kind-podkill:
	$(PY) deploy/kind/lab.py podkill --count $(or $(N),5) $(if $(OUT),--output $(OUT))
kind-test:
	PATHWATCH_KIND_KUBECONFIG=$(KIND_KUBECONFIG) go test -count=1 -v -run '^TestClusters$$' ./infra/test
# Task 17 BGP lab (lab/bgp/README.md): plain Docker networks pathwatch-bgp-*, no containerlab.
lab-up lab-rpki lab-nearest lab-status lab-down:
	$(PY) lab/bgp/lab.py $(subst lab-,,$@)
lab-inject:
	$(PY) lab/bgp/lab.py inject $(INCIDENT) $(if $(WITHDRAW),--withdraw)
lab-detect:
	$(PY) lab/bgp/detect.py --seconds $(or $(SECONDS),60)
lab-drill:
	$(PY) lab/bgp/drill.py $(INCIDENT)
lab-test:
	cd lab/tests && PATHWATCH_BGP_LAB=1 $(PY) -m unittest -v test_lab
# Offline chart checks: helm lint + kubeconform (strict) on rendered charts, and the rendered Rollout file.
deploy-check:
	bash deploy/scripts/check.sh

proto-generate:
	bash scripts/generate-proto.sh

proto-check: test-proto
