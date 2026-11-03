# Task10A VM component. Task10B owns new NIC/PIP/network/admission entrypoint.
# Empty by default; no resource creation is currently admitted by the guard.
locals {
  group  = "rg-pathwatch-lab"
  prefix = "/subscriptions/${var.subscription_id}/resourceGroups/${local.group}"
}
resource "azurerm_linux_virtual_machine" "probe" {
  for_each                        = var.observers
  name                            = "vm-pathwatch-probe-${each.key}"
  resource_group_name             = local.group
  location                        = each.value.location
  size                            = each.value.vm_size
  admin_username                  = "pathwatch"
  disable_password_authentication = true
  allow_extension_operations      = false
  provision_vm_agent              = true
  network_interface_ids           = [each.value.nic_id]
  admin_ssh_key {
    username   = "pathwatch"
    public_key = var.admin_public_key
  }
  os_disk {
    name                 = "os-pathwatch-probe-${each.key}"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = 30
    caching              = "ReadWrite"
  }
  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = each.value.vm_size == "Standard_B2pts_v2" ? "server-arm64" : "server"
    version   = each.value.image_version
  }
  tags = {
    project          = "pathwatch"
    owner            = "codex-pathwatch"
    ttl              = each.value.ttl
    est_usd_per_hour = each.value.vm_hourly
  }
  lifecycle {
    precondition {
      condition     = startswith(lower(each.value.nic_id), lower("${local.prefix}/providers/Microsoft.Network/networkInterfaces/nic-pathwatch-probe-"))
      error_message = "Only an exact owned project probe NIC may be attached; admission verifies existence, NSG, TTL and reservation."
    }
    precondition {
      condition     = timecmp(each.value.ttl, var.parent_ttl) <= 0
      error_message = "Observer cannot outlive its parent resource group."
    }
  }
}
resource "azapi_update_resource" "disk_tags" {
  for_each    = var.observers
  type        = "Microsoft.Compute/disks@2024-03-02"
  resource_id = "${local.prefix}/providers/Microsoft.Compute/disks/os-pathwatch-probe-${each.key}"
  body = { tags = {
    project = "pathwatch", owner = "codex-pathwatch", ttl = each.value.ttl, est_usd_per_hour = each.value.disk_hourly
  } }
  depends_on = [azurerm_linux_virtual_machine.probe]
}
output "probe_ids" { value = keys(var.observers) }
