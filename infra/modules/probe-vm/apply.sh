#!/usr/bin/env bash
# Apply only a canonical-reserved plan from the complete Task10B root.
set -euo pipefail
root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
source "$root/scripts/dev-env.sh"
if [[ $# != 2 ]]; then echo 'usage: apply.sh saved.tfplan proposal.json' >&2; exit 2; fi
plan="$(realpath -- "$1")"
[[ "$(dirname -- "$plan")" == "$root/infra/envs/dev/azure-probes" ]] || { echo 'Use the complete guarded azure-probes Terraform root' >&2; exit 2; }
python3 "$root/scripts/azure_budget.py" check --plan "$plan" --proposal "$2"
terraform -chdir="$root/infra/envs/dev/azure-probes" apply -input=false "$plan"
