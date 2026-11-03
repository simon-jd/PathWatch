# deploy：Task 14 本机部分（kind + Argo CD Core + Argo Rollouts）

这是 kind 上的演练环境，不是 k3s 或 AKS。集群名 `pathwatch-canary`，kubeconfig 只用 `.cache/kind/kubeconfig`。

## 组成

| 路径 | 内容 |
|---|---|
| `charts/ingest` | 唯一 core（StatefulSet，OnDelete；ClickHouse 作为原生 sidecar，只监听 127.0.0.1）和 frontend Rollout（frontend + Rust edge-proxy 原生 sidecar + adminbridge 指标桥），canary 的 AnalysisTemplate |
| `charts/edge-proxy` | library chart：edge-proxy 的 sidecar 和 TOML 配置；TLS 在 pod IP 的 8443，转发到 pod 内 loopback 的查询 API 和 Report |
| `charts/api` | `api` Service（选中所有 frontend pod，stable 和 canary 都收真实流量）和 `querygen` 合成客户端（20 rps，每个请求新连接） |
| `charts/observability` | Prometheus，经 mTLS 抓取 adminbridge；只在 `pathwatch-app` 里做 pod 发现 |
| `argocd/app-of-apps.yaml`、`argocd/apps/` | 根应用 → AppProject + 3 个子应用 |
| `argocd/bootstrap/repo-server-git-daemon.yaml` | 给 repo-server 加只读 git daemon（127.0.0.1:9418），源是 `.cache/task14/gitops/pathwatch-deploy` |
| `rollouts/ingest-rollout.yaml` | ingest chart 渲染出的 Rollout + AnalysisTemplate（`make deploy-check UPDATE=1` 生成，`make deploy-check` 比对） |
| `kind/lab.py` | `make kind-up / kind-images / kind-publish / kind-drill VARIANT=bad500|control / kind-status / kind-down` |

期望状态仓库在 `.cache/task14/gitops/pathwatch-deploy`（本机，不 push）。`publish` 用 `git archive <源码提交> deploy/` 生成内容，再写入 `values-kind.yaml`（该版本的镜像 tag）和 `provenance.json`，然后提交。镜像 tag 是 `git-<源码 sha>`；坏版本是 `-bad500`，用 `-tags pathwatch_lab_bad500` 编译：查询照常读 core，然后统一返回 500（`ingest/internal/queryapi/labfault_*.go`）。`-control` 是与好版本相同的镜像换一个 tag，用来做"应当晋升"的正对照。

金丝雀判定：Prometheus 中候选 pod（`rollouts_pod_template_hash`）1 分钟内查询请求的失败比例；> 5% 或请求少于 5 个（没有数据）都算失败，`failureLimit: 0`。2026-10-28 起 frontend 4 个副本，第一步 setWeight 25（maxSurge 1：多出 1 个候选 pod，占 1/5），initialDelay 20 s，interval 10 s（之前是 2 副本、50%、40 s/15 s）。

NetworkPolicy（`charts/ingest/templates/networkpolicy.yaml`）：`pathwatch-app` 默认拒绝所有入站和出站。入站只放行 frontend → core:core-rpc、querygen → frontend:https、`pathwatch-observe` 的 Prometheus → core/frontend:metrics。出站只放行 DNS（kube-system:53）、frontend → core:9444、querygen → frontend 的 443 和 8443。`make kind-netpol` 从 Prometheus pod 用 `nc -z` 验证哪些端口通、哪些不通。querygen 同时打查询和 Report（`-report-rps`，默认 2）。

## 工具来源

- kind v0.33.0、kubeconform v0.8.0：GitHub release，sha256 已核对。
- Argo CD v3.5.6 `core-install.yaml`（sha256 d3d9d355…）、Argo Rollouts v1.10.1 `install.yaml`（与 GitHub 资产 digest 149148… 一致）：放在 `.cache/task14/dl/`。
- helm v4.2.1：从 `quay.io/argoproj/argocd:v3.5.6`（index digest 8e5cc5bb…）里复制，也就是 Argo CD 渲染用的那一个；get.helm.sh 不在允许的下载源里。
- kubectl：从 kind 节点镜像复制。
- kubeconform 内置类型的 schema 来自 yannh/kubernetes-json-schema 的 v1.34.12-standalone-strict（仓库里最新的版本，比集群 1.36 旧；`networkpolicy-networking-v1.json` 10-28 经 api.github.com contents 接口下载，git blob sha 7154b244… 已核对）；CRD schema 由 `scripts/crd_schemas.py` 从上面两个安装清单生成。
