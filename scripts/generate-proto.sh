#!/usr/bin/env bash
# Existing pinned tools only: generation never installs or downloads anything.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ $# -le 1 && ( $# -eq 0 || "$1" == --check ) ]] || { echo 'Usage: scripts/generate-proto.sh [--check]' >&2; exit 2; }
[[ "$(/usr/bin/protoc --version)" == 'libprotoc 3.21.12' ]]
[[ "$(.tools/bin/protoc-gen-go --version)" == 'protoc-gen-go v1.36.1' ]]
[[ "$(.tools/bin/protoc-gen-go-grpc --version)" == 'protoc-gen-go-grpc 1.5.1' ]]
# These are the verified existing generator builds, not merely matching labels.
echo '4d1fd69edc90d34118bb8005df3d4a4d49d54d2eec8cbef52844ee7c0ead55c5  .tools/bin/protoc-gen-go' | sha256sum --check --status
echo '692015891da79bfbd4506c84078d7ced2485904cfb46259e91f9515496e37044  .tools/bin/protoc-gen-go-grpc' | sha256sum --check --status
output="$PWD"
if [[ "${1:-}" == --check ]]; then
 output="$(mktemp -d "$PWD/.cache/proto-check.XXXXXX")"
 trap 'rm -rf -- "$output"' EXIT
fi
/usr/bin/protoc --proto_path=. --proto_path=/usr/include \
 --plugin="protoc-gen-go=$PWD/.tools/bin/protoc-gen-go" \
 --plugin="protoc-gen-go-grpc=$PWD/.tools/bin/protoc-gen-go-grpc" \
 --go_out="$output" --go_opt=paths=source_relative \
 --go-grpc_out="$output" --go-grpc_opt=paths=source_relative \
 proto/pathwatch/v1/probe.proto proto/pathwatch/core/v1/core.proto
if [[ "${1:-}" == --check ]]; then
 for file in proto/pathwatch/v1/{probe.pb.go,probe_grpc.pb.go} proto/pathwatch/core/v1/{core.pb.go,core_grpc.pb.go}; do diff -u "$file" "$output/$file"; done
 echo 'Generated bindings match pinned schema and generators.'
fi
