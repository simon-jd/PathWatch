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
  suffix = "pathwatch-xdp-${var.run_id}"
  prefix = "/subscriptions/${var.subscription_id}/resourceGroups/${local.group}/providers"
  tags   = { project = "pathwatch", owner = "codex-pathwatch", ttl = var.ttl, est_usd_per_hour = "0" }
}
resource "azurerm_virtual_network" "runner" {
  name                = "vnet-${local.suffix}"
  resource_group_name = local.group
  location            = var.location
  address_space       = ["10.203.0.0/24"]
  dns_servers         = []
  tags                = local.tags
}
resource "azurerm_subnet" "runner" {
  name                 = "runner"
  resource_group_name  = local.group
  virtual_network_name = "vnet-${local.suffix}"
  address_prefixes     = ["10.203.0.0/27"]
  depends_on           = [azurerm_virtual_network.runner]
}
resource "azurerm_network_security_group" "runner" {
  name                = "nsg-${local.suffix}"
  resource_group_name = local.group
  location            = var.location
  tags                = local.tags
  security_rule {
    name                       = "operator-ssh"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = var.operator_ipv4_cidr
    destination_address_prefix = "10.203.0.4"
  }
}
resource "azurerm_public_ip" "runner" {
  name                = "pip-${local.suffix}"
  resource_group_name = local.group
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  sku_tier            = "Regional"
  ip_version          = "IPv4"
  tags                = merge(local.tags, { est_usd_per_hour = var.hourly.ip })
}
resource "azurerm_network_interface" "runner" {
  name                           = "nic-${local.suffix}"
  resource_group_name            = local.group
  location                       = var.location
  ip_forwarding_enabled          = false
  accelerated_networking_enabled = false
  dns_servers                    = []
  tags                           = local.tags
  ip_configuration {
    name                          = "primary"
    primary                       = true
    private_ip_address_allocation = "Static"
    private_ip_address            = "10.203.0.4"
    subnet_id                     = "${local.prefix}/Microsoft.Network/virtualNetworks/vnet-${local.suffix}/subnets/runner"
    public_ip_address_id          = "${local.prefix}/Microsoft.Network/publicIPAddresses/pip-${local.suffix}"
  }
  depends_on = [azurerm_subnet.runner, azurerm_public_ip.runner]
}
resource "azurerm_network_interface_security_group_association" "runner" {
  network_interface_id      = "${local.prefix}/Microsoft.Network/networkInterfaces/nic-${local.suffix}"
  network_security_group_id = "${local.prefix}/Microsoft.Network/networkSecurityGroups/nsg-${local.suffix}"
  depends_on                = [azurerm_network_interface.runner, azurerm_network_security_group.runner]
}
resource "azurerm_linux_virtual_machine" "runner" {
  name                            = "vm-${local.suffix}"
  resource_group_name             = local.group
  location                        = var.location
  size                            = "Standard_B2ps_v2"
  admin_username                  = "pathwatch"
  disable_password_authentication = true
  network_interface_ids           = ["${local.prefix}/Microsoft.Network/networkInterfaces/nic-${local.suffix}"]
  provision_vm_agent              = true
  allow_extension_operations      = false
  patch_mode                      = "ImageDefault"
  priority                        = "Regular"
  admin_ssh_key {
    username   = "pathwatch"
    public_key = var.admin_public_key
  }
  os_disk {
    name                 = "os-${local.suffix}"
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = 32
  }
  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server-arm64"
    version   = "24.04.202610210"
  }
  tags       = merge(local.tags, { est_usd_per_hour = var.hourly.vm })
  depends_on = [azurerm_network_interface_security_group_association.runner]
}
# Tags do not propagate from VM. This tags-only manager does not delete the disk.
resource "azapi_update_resource" "disk_tags" {
  type        = "Microsoft.Compute/disks@2024-03-02"
  resource_id = "${local.prefix}/Microsoft.Compute/disks/os-${local.suffix}"
  body        = { tags = merge(local.tags, { est_usd_per_hour = var.hourly.disk }) }
  depends_on  = [azurerm_linux_virtual_machine.runner]
}
