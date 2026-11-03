locals {
  group     = "rg-pathwatch-lab"
  prefix    = "/subscriptions/${var.subscription_id}/resourceGroups/${local.group}/providers"
  regions   = { a = "westus", b = "swedencentral" }
  cidrs     = { a = "10.10.0.0/16", b = "10.20.0.0/16" }
  subnets   = { a = "10.10.0.0/24", b = "10.20.0.0/24" }
  ips       = { a = "10.10.0.4", b = "10.20.0.4" }
  records   = merge(local.ips, { home = "10.30.0.10" })
  zone      = "dns.pathwatch.internal"
  label     = "pathwatch-${substr(sha256(var.subscription_id), 0, 10)}-dns"
  tags      = { project = "pathwatch", owner = "codex-pathwatch", ttl = var.ttl }
  free_tags = merge(local.tags, { est_usd_per_hour = "0" })
  rule_kinds = {
    ssh          = { priority = 100, protocol = "Tcp", port = "22", source = var.owner_cidr }
    wg           = { priority = 110, protocol = "Udp", port = "51824", source = var.owner_cidr }
    owner_http   = { priority = 120, protocol = "Tcp", port = "80", source = var.owner_cidr }
    managed_http = { priority = 130, protocol = "Tcp", port = "80", source = "AzureTrafficManager" }
    dns_tcp      = { priority = 140, protocol = "Tcp", port = "53", source = "10.30.0.10/32" }
    dns_udp      = { priority = 150, protocol = "Udp", port = "53", source = "10.30.0.10/32" }
    deny         = { priority = 4096, protocol = "*", port = "*", source = "*" }
  }
  rules = merge([for site in keys(local.regions) : { for name, rule in local.rule_kinds : "${site}-${name}" => merge(rule, { site = site, kind = name }) }]...)
}
data "azurerm_resource_group" "project" { name = local.group }
resource "azurerm_virtual_network" "this" {
  for_each            = local.regions
  name                = "vnet-pathwatch-dns-${each.key}"
  resource_group_name = local.group
  location            = each.value
  dns_servers         = []
  address_space       = [local.cidrs[each.key]]
  tags                = local.free_tags
  lifecycle {
    precondition {
      condition     = data.azurerm_resource_group.project.tags["project"] == "pathwatch" && data.azurerm_resource_group.project.tags["owner"] == "codex-pathwatch" && timecmp(var.ttl, data.azurerm_resource_group.project.tags["ttl"]) <= 0
      error_message = "Retained parent ownership/TTL must cover DNS lab."
    }
  }
}
resource "azurerm_subnet" "this" {
  #checkov:skip=CKV2_AZURE_31:Both subnets have exact named NSG associations with explicit dependency; deterministic ARM IDs are validated by the DNS scope guard, not discovered by this static graph.
  for_each                        = local.regions
  name                            = "snet-dns"
  resource_group_name             = local.group
  virtual_network_name            = azurerm_virtual_network.this[each.key].name
  address_prefixes                = [local.subnets[each.key]]
  default_outbound_access_enabled = false
}
resource "azurerm_network_security_group" "this" {
  for_each            = local.regions
  name                = "nsg-pathwatch-dns-${each.key}"
  resource_group_name = local.group
  location            = each.value
  tags                = local.free_tags
}
resource "azurerm_network_security_rule" "this" {
  for_each                    = local.rules
  name                        = each.value.kind
  resource_group_name         = local.group
  network_security_group_name = azurerm_network_security_group.this[each.value.site].name
  priority                    = each.value.priority
  direction                   = "Inbound"
  access                      = each.value.kind == "deny" ? "Deny" : "Allow"
  protocol                    = each.value.protocol
  source_port_range           = "*"
  destination_port_range      = each.value.port
  source_address_prefix       = each.value.source
  destination_address_prefix  = each.value.kind == "deny" ? "*" : "${local.ips[each.value.site]}/32"
}
resource "azurerm_subnet_network_security_group_association" "this" {
  for_each                  = local.regions
  depends_on                = [azurerm_subnet.this, azurerm_network_security_group.this]
  subnet_id                 = "${local.prefix}/Microsoft.Network/virtualNetworks/vnet-pathwatch-dns-${each.key}/subnets/snet-dns"
  network_security_group_id = "${local.prefix}/Microsoft.Network/networkSecurityGroups/nsg-pathwatch-dns-${each.key}"
}
resource "azurerm_public_ip" "this" {
  for_each             = local.regions
  name                 = "pip-pathwatch-dns-${each.key}"
  resource_group_name  = local.group
  location             = each.value
  ddos_protection_mode = "VirtualNetworkInherited"
  allocation_method    = "Static"
  sku                  = "Standard"
  sku_tier             = "Regional"
  ip_version           = "IPv4"
  domain_name_label    = "${local.label}-${each.key}"
  tags                 = merge(local.tags, { est_usd_per_hour = var.hourly.ip })
}
resource "azurerm_network_interface" "this" {
  #checkov:skip=CKV_AZURE_119:Explicit priced Standard public IPv4 avoids prohibited NAT; SSH/WG/custom HTTP are owner /32, managed HTTP is AzureTrafficManager only.
  for_each                       = local.regions
  depends_on                     = [azurerm_subnet_network_security_group_association.this, azurerm_public_ip.this]
  name                           = "nic-pathwatch-dns-${each.key}"
  resource_group_name            = local.group
  location                       = each.value
  ip_forwarding_enabled          = false
  accelerated_networking_enabled = false
  dns_servers                    = []
  ip_configuration {
    name                          = "primary"
    primary                       = true
    subnet_id                     = "${local.prefix}/Microsoft.Network/virtualNetworks/vnet-pathwatch-dns-${each.key}/subnets/snet-dns"
    private_ip_address_allocation = "Static"
    private_ip_address            = local.ips[each.key]
    public_ip_address_id          = "${local.prefix}/Microsoft.Network/publicIPAddresses/pip-pathwatch-dns-${each.key}"
  }
  tags = local.free_tags
}
resource "azurerm_linux_virtual_machine" "this" {
  for_each                        = local.regions
  depends_on                      = [azurerm_network_interface.this]
  name                            = "vm-pathwatch-dns-${each.key}"
  resource_group_name             = local.group
  location                        = each.value
  priority                        = "Regular"
  size                            = "Standard_B2pts_v2"
  admin_username                  = "pathwatch"
  disable_password_authentication = true
  network_interface_ids           = ["${local.prefix}/Microsoft.Network/networkInterfaces/nic-pathwatch-dns-${each.key}"]
  provision_vm_agent              = true
  allow_extension_operations      = false
  patch_mode                      = "ImageDefault"
  admin_ssh_key {
    username   = "pathwatch"
    public_key = var.admin_public_key
  }
  os_disk {
    name                 = "os-pathwatch-dns-${each.key}"
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = 30
  }
  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server-arm64"
    version   = "24.04.202610210"
  }
  tags = merge(local.tags, { est_usd_per_hour = var.hourly.vm })
}
resource "azapi_update_resource" "disk_tags" {
  for_each    = local.regions
  depends_on  = [azurerm_linux_virtual_machine.this]
  type        = "Microsoft.Compute/disks@2024-03-02"
  resource_id = "${local.prefix}/Microsoft.Compute/disks/os-pathwatch-dns-${each.key}"
  body        = { tags = merge(local.tags, { est_usd_per_hour = var.hourly.disk }) }
}
resource "azurerm_private_dns_zone" "this" {
  name                = local.zone
  resource_group_name = local.group
  tags                = merge(local.tags, { est_usd_per_hour = var.hourly.dns })
}
resource "azurerm_private_dns_a_record" "this" {
  for_each            = local.records
  name                = each.key
  resource_group_name = local.group
  zone_name           = azurerm_private_dns_zone.this.name
  ttl                 = 30
  records             = [each.value]
  tags                = local.free_tags
}
resource "azurerm_private_dns_zone_virtual_network_link" "this" {
  for_each              = local.regions
  depends_on            = [azurerm_virtual_network.this]
  name                  = "link-pathwatch-dns-${each.key}"
  resource_group_name   = local.group
  private_dns_zone_name = azurerm_private_dns_zone.this.name
  virtual_network_id    = "${local.prefix}/Microsoft.Network/virtualNetworks/vnet-pathwatch-dns-${each.key}"
  # resolution_policy is only valid for privatelink zones; omit for this custom zone.
  registration_enabled = false
  tags                 = local.free_tags
}
resource "azurerm_traffic_manager_profile" "this" {
  name                   = local.label
  resource_group_name    = local.group
  profile_status         = "Enabled"
  traffic_routing_method = "Priority"
  traffic_view_enabled   = false
  dns_config {
    relative_name = local.label
    ttl           = 30
  }
  monitor_config {
    protocol                     = "HTTP"
    port                         = 80
    path                         = "/healthz"
    interval_in_seconds          = 30
    timeout_in_seconds           = 5
    tolerated_number_of_failures = 2
  }
  tags = merge(local.tags, { est_usd_per_hour = var.hourly.tm })
}
resource "azurerm_traffic_manager_azure_endpoint" "this" {
  for_each             = local.regions
  depends_on           = [azurerm_traffic_manager_profile.this, azurerm_public_ip.this]
  name                 = each.key
  profile_id           = "${local.prefix}/Microsoft.Network/trafficManagerProfiles/${local.label}"
  target_resource_id   = "${local.prefix}/Microsoft.Network/publicIPAddresses/pip-pathwatch-dns-${each.key}"
  priority             = each.key == "a" ? 1 : 2
  enabled              = true
  always_serve_enabled = false
}
