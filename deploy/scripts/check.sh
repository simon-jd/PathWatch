#!/usr/bin/env bash
# Offline chart checks (make deploy-check): helm lint, strict kubeconform on the
# rendered charts and Argo CD manifests, and the rendered deploy/rollouts file.
# helm is the binary shipped in quay.io/argoproj/argocd:v3.5.6 (what Argo renders
# with); schemas live in .cache/task14/schemas (deploy/README.md says how to fetch).
# UPDATE=1 rewrites deploy/rollouts/ingest-rollout.yaml instead of diffing it.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$root/.cache/task14/check
schemas=$root/.cache/task14/schemas
rm -rf "$work"
mkdir -p "$work"
cp -r "$root/deploy/charts" "$work/charts"
mkdir -p "$work/charts/ingest/charts"
cp -r "$work/charts/edge-proxy" "$work/charts/ingest/charts/edge-proxy"
cat > "$work/values-test.yaml" <<'EOF'
images:
  ingest: pathwatch/ingest:git-000000000000
  frontend: pathwatch/ingest:git-000000000000
  edgeProxy: pathwatch/edge-proxy:git-000000000000
  querygen: pathwatch/querygen:git-000000000000
EOF
kubeconform_args=(-strict -summary -schema-location "$schemas/k8s/{{.ResourceKind}}{{.KindSuffix}}.json"
  -schema-location "$schemas/crd/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json")
helm lint "$work/charts/edge-proxy"
for chart in ingest api observability; do
  helm lint --strict "$work/charts/$chart" -f "$work/values-test.yaml"
done
helm template observability "$work/charts/observability" -n pathwatch-observe -f "$work/values-test.yaml" > "$work/observability.yaml"
helm template api "$work/charts/api" -n pathwatch-app -f "$work/values-test.yaml" > "$work/api.yaml"
helm template ingest "$work/charts/ingest" -n pathwatch-app -f "$work/values-test.yaml" > "$work/ingest.yaml"
kubeconform "${kubeconform_args[@]}" "$work/ingest.yaml" "$work/api.yaml" "$work/observability.yaml" \
  "$root/deploy/argocd/app-of-apps.yaml" "$root/deploy/argocd/apps/"*.yaml "$root/deploy/argocd/bootstrap/root-project.yaml"
# Missing required image tags must fail rendering, never fall back to a default.
if helm template ingest "$work/charts/ingest" > /dev/null 2>&1; then
  echo "ingest chart rendered without image tags" >&2; exit 1
fi
"$root/.tools/py/bin/python" -I "$root/deploy/scripts/extract_rollout.py" "$work/ingest.yaml" > "$work/ingest-rollout.yaml"
if [[ "${UPDATE:-}" == 1 ]]; then
  cp "$work/ingest-rollout.yaml" "$root/deploy/rollouts/ingest-rollout.yaml"
else
  diff -u "$root/deploy/rollouts/ingest-rollout.yaml" "$work/ingest-rollout.yaml"
fi
echo "deploy-check: ok"
