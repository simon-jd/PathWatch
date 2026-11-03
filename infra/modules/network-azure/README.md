# Azure regional network

这是 Azure Task 2；旧 AWS/GCP Task 2 已停用。本模块只读取 Task 1 的 `rg-pathwatch-lab`，核对显式 subscription、精确 RG ARM ID 与 ownership tags，然后创建 VNet、三个子网、NSG、UDR 表及关联。不创建 RG、VM、公共 IP、NAT、peering 或收费网络服务。`resource_provider_registrations="none"` 在调用 root 配置；`Microsoft.Network` 必须预先已注册，代码不修改订阅注册。

| 用途 | 区域 A | 区域 B |
|---|---|---|
| VNet 预留 | 10.10.0.0/16 | 10.20.0.0/16 |
| workloads | 10.10.0.0/24 | 10.20.0.0/24 |
| nva | 10.10.1.0/27 | 10.20.1.0/27 |
| aks_nodes | 10.10.16.0/20 | 10.20.16.0/20 |
| AKS pods 预留，未建子网 | 10.10.64.0/18 | 10.20.64.0/18 |
| AKS services 预留，未建子网 | 10.10.32.0/20 | 10.20.32.0/20 |

home `10.30.0.0/16` 和 WireGuard transit `10.255.0.0/24` 保留。实际 home LAN、容器、现有 VPN 是否重叠仍须由操作者检查，当前没有检查或修改主机网络。预留不表示 AKS 已可部署；Task 14 仍需选网络插件并配置集群所需的端口和路由。

自定义 NSG `deny-all-inbound` 优先级4096，高于 Azure 内置 AllowVNetInBound 的65000；允许规则必须更高优先级且明确来源、目的子网、协议和端口。`private_ingress_rules` 只接受上述自有私网来源，拒绝 SSH22/RDP3389、通配 TCP/UDP 端口和任意公网来源。`wireguard_peer_cidrs` 默认空，只接受明确 owned public IPv4 /32 到 NVA 子网 UDP51820；这些输入不能证明地址所有权，真实运行必须由操作者确认。模块没有开启管理入口，也未修改出站 NSG 默认规则。子网显式关闭 implicit outbound；未来 VM/NVA 的 Standard 公共 IP 由 Task 10 预算授权后创建。

三个子网均关联 NSG；只有 workloads/aks_nodes 关联 UDR 表。默认无自定义路由，保留 Azure system route。只有非空 `nva_private_ip` 且 `home_routes_confirmed_no_overlap=true` 时，创建 home 和 transit 两条特定前缀 UDR，下一跳限当前 NVA `/27` 的`.1.4`–`.1.30`。NVA 子网不关联此表，避免把 NVA 自身流量送回自己。没有 `0.0.0.0/0` UDR，不宣称区域互联、WireGuard、FRR/BGP/BFD 已完成。

VNet、NSG、路由表带四个 required tags；Azure 子网、NSG rules、routes 和 association 不支持这些 tags，生命周期归 tagged parent，预算台账仍每个 Terraform resource 单独登记。销毁 regional state 不拥有共享 RG 或远端 state account；不要用 `make azure-down` 代替区域拆除。

输出 `vnet_id/name`、按用途的 `subnet_ids/names/prefixes`、`nsg_id/name`、`route_table_id/name`、`address_spaces`、`reserved_address_spaces`、`location` 和 `resource_group_name`。VM/NVA 消费 nva subnet 和 NSG；AKS 消费 aks_nodes subnet 与预留地址段。外部数据面 readiness 仍须相应任务真实验收。

依据：[NSG 优先级与默认规则](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview)、[显式出网与 private subnet](https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access)、[Azure UDR](https://learn.microsoft.com/en-us/azure/virtual-network/virtual-networks-udr-overview)。

Task3默认关闭的窄扩展：`wireguard_alternate_enabled`只为已有owned public/32 peer增加UDP51821；`management_peer_cidrs`最多两个owner公网/32，仅SSH22到NVA/workload子网；`hybrid_forwarding_enabled`创建两条workload子网→home10.30/16的Inbound ICMP/TCP5201规则，以及两条home10.30/16→workload子网的Outbound ICMP/TCP5201规则。入站在NVA NIC接收UDR transit时按原始远端destination匹配；出站在隧道解封装后保留原始home source。NVA子网无home UDR，默认AllowVnetOutBound的VirtualNetwork源可能不包含home；因此新发起home流量需要精确Outbound allow，不能靠反向已建立流量的stateful回复证明可达。没有公网BGP/BFD、任意目的地或默认路由输入。所有新rule仍由同一taggedNSG父生命周期负责，免费也逐项登记台账；旧Task2默认行为不变。
