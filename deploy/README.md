# deploy

Kubernetes packaging for ingest and the edge proxy, plus a local kind lab for the canary
drill. Results: [docs/evidence/README.md](../docs/evidence/README.md#canary-rollback-with-argo-rollouts-kind-one-machine).

| Path | Contents |
|---|---|
| `charts/ingest` | single `core` StatefulSet (ClickHouse as a loopback-only sidecar) and the `frontend` Argo Rollout (frontend + Rust edge-proxy sidecar + metrics bridge), the canary AnalysisTemplate and a default-deny NetworkPolicy |
| `charts/edge-proxy` | library chart for the edge-proxy sidecar and its TOML config (TLS on 8443) |
| `charts/api` | `api` Service across all frontend pods and the `querygen` synthetic client |
| `charts/observability` | Prometheus scraping the metrics bridge over mTLS |
| `argocd/` | app-of-apps root, AppProject, and a repo-server git daemon for a local GitOps repo |
| `rollouts/ingest-rollout.yaml` | rendered Rollout + AnalysisTemplate, checked against the chart |
| `kind/lab.py` | drives the kind lab (`make kind-*`) |

Canary rule: fail if more than 5% of the candidate's queries fail over 1 minute, or if it
served fewer than 5 (no data is a failure).

```sh
make deploy-check            # helm lint + strict kubeconform on rendered charts (offline)
make kind-up kind-images kind-publish
make kind-replace-core kind-wait
make kind-drill VARIANT=control   # good build under a new tag: should promote
make kind-drill VARIANT=bad500    # build that returns 500: should roll back
make kind-netpol kind-test
make kind-down
```

The kind targets need Docker and `kind`, `kubectl`, `helm` and `kubeconform` in
`.tools/bin`; the desired-state repository lives in `.cache/task14/gitops` and is never
pushed. This is kind only; k3s and AKS are not covered.
Tool sources and detailed notes (Chinese): [docs/dev/notes/deploy.md](../docs/dev/notes/deploy.md).
