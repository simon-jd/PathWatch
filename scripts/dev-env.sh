#!/usr/bin/env bash
# Source from scripts or an interactive shell. Keep all caches in this checkout.
PATHWATCH_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PATHWATCH_ROOT
export PATH="$PATHWATCH_ROOT/.tools/bin:$PATHWATCH_ROOT/.tools/go/bin:$PATHWATCH_ROOT/.tools/rust/bin:$PATHWATCH_ROOT/.tools/py/bin:$PATHWATCH_ROOT/.tools/checkov/bin:$PATH"
export GOTOOLCHAIN=local
export GOPATH="$PATHWATCH_ROOT/.cache/gopath"
export GOCACHE="$PATHWATCH_ROOT/.cache/go-build"
export GOMODCACHE="$PATHWATCH_ROOT/.cache/go-mod"
export CARGO_HOME="$PATHWATCH_ROOT/.cache/cargo"
export RUSTUP_HOME="$PATHWATCH_ROOT/.tools/rustup"
export XDG_CACHE_HOME="$PATHWATCH_ROOT/.cache"
export UV_CACHE_DIR="$PATHWATCH_ROOT/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PATHWATCH_ROOT/.tools/python"
export TF_PLUGIN_CACHE_DIR="$PATHWATCH_ROOT/.cache/terraform-providers"
export CHECKPOINT_DISABLE=1
export AZURE_LOGGING_ENABLE_LOG_FILE=no
export AZURE_LOGGING_LOG_DIR="$PATHWATCH_ROOT/.cache/azure-logs"
export AZURE_CORE_COLLECT_TELEMETRY=no
export TFLINT_PLUGIN_DIR="$PATHWATCH_ROOT/.cache/tflint-plugins"
mkdir -p "$GOCACHE" "$GOMODCACHE" "$TF_PLUGIN_CACHE_DIR"
