# 临时 Azure A NVA / 工作负载

Task3 当前只允许 westus / Standard_B2pts_v2（2vCPU、1GiB、Arm64）和固定 Canonical Ubuntu24.04 ARM64 V2 镜像24.04.202610210。SKU metadata无restriction不是分配成功；创建前controller仍须读取image show、SKU和6vCPU配额余量。NVA+client共4vCPU。此模块不创建RG，也不持有state backend。独立根目录`envs/dev/azure-hybrid`将既有network-azure与本模块放在同一state，remote backend沿用Task1；Task2 azure-a与此root不可同时管理同名网络。

NVA=10.10.1.4、client=10.10.0.4。NIC只有NVA打开IP forwarding；工作负载/AKS子网UDR指向NVA，NVA子网绝不关联UDR。client用于真实转发验收，不能以ping NVA代替。两台VM各有单独计价的Regional/Standard/Static/IPv4 PIP和30–32GB Standard_LRS HDD OS盘。每个PIP提供免费Azure DNS label（project+订阅hash+角色）、ID/FQDN供后续Traffic Manager接口使用，不购买域名。

VM/PIP/NIC均打四标签。VM内联OS盘不支持tags；`azapi_update_resource.disk_tags`对确定的本项目OS盘ARM ID执行**仅tags** PATCH，显式覆盖四标签。该资源台账行承载实际OS盘容量/transactions费用，不能按免费子资源处理。azapi删除tag manager本身不调用ARM删除；根provider显式`delete_os_disk_on_deletion=true`，真正OS盘由VM销毁时删除。Controller必须实测disk四标签及销毁后disk/NIC/PIP无残留；mock不证明这点。未打标签的窗口仅在VM创建至依赖的tag PATCH之间，失败时保留预留并按本state销毁。

azurerm4.63.0 / azapi2.8.0固定并提交lockfile。provider不会注册订阅级providers。没有额外数据盘、收费Marketplace、GPU、NAT、VPN gateway、VM extension、默认路由或公网BGP/BFD规则。Checkov118/119例外只挂NIC：NVA转发为功能要求、PIP替代预算禁止的NAT；client转发仍false，入站SSH只owner/32。

官方语义：[VM与OS盘删除](https://registry.terraform.io/providers/hashicorp/azurerm/4.63.0/docs/resources/linux_virtual_machine)、[azapi tag部分更新/删除不撤销](https://github.com/Azure/terraform-provider-azapi/blob/v2.8.0/docs/resources/update_resource.md)、[Azure HDD S4/S6 16KiB transaction拆分](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-types)。
