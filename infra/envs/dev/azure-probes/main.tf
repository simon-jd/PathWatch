provider "azurerm" {
  features {
    virtual_machine { delete_os_disk_on_deletion = true }
  }
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
}
provider "azapi" {
  subscription_id            = var.subscription_id
  enable_preflight           = false
  skip_provider_registration = true
}
locals {
  group  = "rg-pathwatch-lab"
  prefix = "/subscriptions/${var.subscription_id}/resourceGroups/${local.group}/providers/Microsoft.Network"
  stem   = var.probe_site == "azure-a" ? "10.10" : "10.20"
  tags   = { project = "pathwatch", owner = "codex-pathwatch", ttl = var.ttl }
}
module "network" {
  source                           = "../../../modules/network-azure"
  subscription_id                  = var.subscription_id
  resource_group_name              = local.group
  name                             = "pathwatch-${var.probe_site}"
  location                         = var.location
  address_space                    = "${local.stem}.0.0/16"
  tags                             = merge(local.tags, { est_usd_per_hour = "0" })
  wireguard_peer_cidrs             = var.owner_peer_cidrs
  wireguard_alternate_enabled      = true
  management_peer_cidrs            = var.owner_peer_cidrs
  nva_private_ip                   = "${local.stem}.1.4"
  home_routes_confirmed_no_overlap = var.home_routes_confirmed_no_overlap
}
resource "azurerm_public_ip" "probe" {
  name                = "pip-pathwatch-probe-${var.probe_site}"
  resource_group_name = local.group
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  sku_tier            = "Regional"
  ip_version          = "IPv4"
  tags                = merge(local.tags, { est_usd_per_hour = var.hourly.ip })
}
resource "azurerm_network_interface" "probe" {
  depends_on                     = [module.network, azurerm_public_ip.probe]
  name                           = "nic-pathwatch-probe-${var.probe_site}"
  resource_group_name            = local.group
  location                       = var.location
  ip_forwarding_enabled          = true
  accelerated_networking_enabled = false
  ip_configuration {
    name                          = "primary"
    primary                       = true
    subnet_id                     = "${local.prefix}/virtualNetworks/vnet-pathwatch-${var.probe_site}/subnets/snet-nva"
    private_ip_address_allocation = "Static"
    private_ip_address            = "${local.stem}.1.4"
    public_ip_address_id          = "${local.prefix}/publicIPAddresses/pip-pathwatch-probe-${var.probe_site}"
  }
  tags = merge(local.tags, { est_usd_per_hour = "0" })
}
module "probe" {
  depends_on       = [azurerm_network_interface.probe]
  source           = "../../../modules/probe-vm"
  subscription_id  = var.subscription_id
  admin_public_key = var.admin_public_key
  parent_ttl       = var.parent_ttl
  observers = {
    (var.probe_site) = {
      location = var.location, vm_size = var.vm_size, image_version = var.image_version
      nic_id   = "${local.prefix}/networkInterfaces/nic-pathwatch-probe-${var.probe_site}"
      ttl      = var.ttl, vm_hourly = var.hourly.vm, disk_hourly = var.hourly.disk
    }
  }
}
output "public_ip" { value = azurerm_public_ip.probe.ip_address }
