locals {
  group       = "rg-pathwatch-lab"
  prefix      = "/subscriptions/${var.subscription_id}/resourceGroups/${local.group}"
  nodes       = var.include_client ? toset(["nva", "client"]) : toset(["nva"])
  common_tags = { project = "pathwatch", owner = "codex-pathwatch", ttl = var.ttl }
}
resource "azurerm_public_ip" "this" {
  for_each            = local.nodes
  name                = "pip-pathwatch-hybrid-${each.key}"
  resource_group_name = local.group
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  sku_tier            = "Regional"
  domain_name_label   = "pathwatch-${substr(sha256(var.subscription_id), 0, 10)}-${each.key}"
  ip_version          = "IPv4"
  tags                = merge(local.common_tags, { est_usd_per_hour = var.hourly.ip })
}
resource "azurerm_network_interface" "this" {
  #checkov:skip=CKV_AZURE_118:Only NVA forwards; required hybrid routing appliance, client forwarding is disabled.
  #checkov:skip=CKV_AZURE_119:Explicit priced Standard public IP avoids prohibited NAT gateway; inbound is owner/32 NSG only.
  depends_on                     = [azurerm_public_ip.this]
  for_each                       = local.nodes
  name                           = "nic-pathwatch-hybrid-${each.key}"
  resource_group_name            = local.group
  location                       = var.location
  ip_forwarding_enabled          = each.key == "nva"
  accelerated_networking_enabled = false
  ip_configuration {
    name                          = "primary"
    primary                       = true
    subnet_id                     = "${local.prefix}/providers/Microsoft.Network/virtualNetworks/vnet-pathwatch-azure-a/subnets/snet-${each.key == "nva" ? "nva" : "workloads"}"
    private_ip_address_allocation = "Static"
    private_ip_address            = each.key == "nva" ? "10.10.1.4" : "10.10.0.4"
    public_ip_address_id          = "${local.prefix}/providers/Microsoft.Network/publicIPAddresses/pip-pathwatch-hybrid-${each.key}"
  }
  tags = merge(local.common_tags, { est_usd_per_hour = "0" })
}
resource "azurerm_linux_virtual_machine" "this" {
  depends_on                      = [azurerm_network_interface.this]
  for_each                        = local.nodes
  name                            = "vm-pathwatch-hybrid-${each.key}"
  resource_group_name             = local.group
  location                        = var.location
  size                            = var.vm_size
  admin_username                  = "pathwatch"
  disable_password_authentication = true
  network_interface_ids           = ["${local.prefix}/providers/Microsoft.Network/networkInterfaces/nic-pathwatch-hybrid-${each.key}"]
  provision_vm_agent              = true
  allow_extension_operations      = false
  patch_mode                      = "ImageDefault"
  admin_ssh_key {
    username   = "pathwatch"
    public_key = var.admin_public_key
  }
  os_disk {
    name                 = "os-pathwatch-hybrid-${each.key}"
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = var.disk_size_gb
  }
  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server-arm64"
    version   = "24.04.202610210"
  }
  tags = merge(local.common_tags, { est_usd_per_hour = var.hourly.vm })
}
# The VM API does not promise tag propagation to its taggable managed OS disk.
# Bind one PATCH containing tags only to the deterministic owned disk ARM ID.
# Destroying this tag manager performs no ARM mutation; VM delete removes OS disk
# (explicit provider feature in root), independently verified in cloud acceptance.
resource "azapi_update_resource" "disk_tags" {
  for_each    = local.nodes
  type        = "Microsoft.Compute/disks@2024-03-02"
  resource_id = "${local.prefix}/providers/Microsoft.Compute/disks/os-pathwatch-hybrid-${each.key}"
  body        = { tags = merge(local.common_tags, { est_usd_per_hour = var.hourly.disk }) }
  depends_on  = [azurerm_linux_virtual_machine.this]
}
