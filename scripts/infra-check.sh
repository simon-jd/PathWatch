#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/dev-env.sh"
cd "$PATHWATCH_ROOT"
# Discover every actual Terraform root/module/fixture; never traverse provider caches.
mapfile -t roots < <(rg --files --hidden infra -g '*.tf' -g '!**/.terraform/**' | sed 's@/[^/]*$@@' | sort -u)
[[ ${#roots[@]} -gt 0 ]] || { echo 'No Terraform roots discovered' >&2; exit 1; }
init_root() {
  # A developer root may already hold a real Azure backend in .terraform.
  # Even init -backend=false can otherwise load that cached backend/auth state.
  # Keep native checks in separate project-owned metadata with no backend init.
  export TF_DATA_DIR="$PATHWATCH_ROOT/.cache/infra-check/${1//\//__}"
  mkdir -p "$TF_DATA_DIR"
  terraform -chdir="$1" init -backend=false -input=false -lockfile=readonly -no-color
}
case "${1:-}" in
  fmt) terraform fmt -check -recursive infra ;;
  validate)
    for root in "${roots[@]}"; do init_root "$root"; terraform -chdir="$root" validate -no-color; done
    unset TF_DATA_DIR ;;
  tflint)
    tflint --version
    for root in "${roots[@]}"; do tflint --chdir="$root" --config="$PATHWATCH_ROOT/.tflint.hcl"; done ;;
  checkov)
    checkov --version
    checkov --directory infra --framework terraform --compact --skip-download
    checkov --directory .github --framework github_actions --compact --skip-download ;;
  tftest)
    # Native terraform tests use plan/mock providers only; nothing is applied.
    for root in "${roots[@]}"; do
      if [[ -d "$root/tests" ]] && [[ -n "$(rg --files "$root/tests" -g '*.tftest.hcl')" ]]; then
        init_root "$root"; terraform -chdir="$root" test -no-color
      fi
    done
    unset TF_DATA_DIR ;;
  test)
    python3 -m unittest discover -s scripts/tests -v
    bash "$0" tftest
    # Compiles actual Terratest harness; Azure applies are opt-in and skip in PR CI.
    go test ./infra/test -v -timeout 60m ;;
  fmt-selftest)
    fixture="$(mktemp -d "$PATHWATCH_ROOT/.cache/fmt-selftest.XXXXXX")"
    trap 'rm -rf "$fixture"' EXIT
    printf 'locals {\n  malformed=   "spacing"\n}\n' > "$fixture/negative.tf"
    if terraform fmt -check "$fixture"; then
      echo 'FAIL: malformed Terraform unexpectedly passed fmt' >&2; exit 1
    fi
    echo 'PASS: malformed Terraform rejected'
    terraform fmt "$fixture"
    terraform fmt -check "$fixture"
    echo 'PASS: corrected Terraform accepted' ;;
  *) echo "usage: $0 {fmt|validate|tflint|checkov|test|fmt-selftest}" >&2; exit 2 ;;
esac
