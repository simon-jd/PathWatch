mock_provider "azurerm" {}
mock_provider "azapi" {}
variables {
  subscription_id  = "11111111-1111-1111-1111-111111111111"
  admin_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU1tU6KxrPjsnIB9dI9Zut3zjS9gKVT9ngP3gYOdr1p pathwatch"
  parent_ttl       = "2026-12-01T00:00:00Z"
}
run "default_no_cloud" {
  command = plan
  assert {
    condition     = length(azurerm_linux_virtual_machine.probe) == 0
    error_message = "No default paid resources."
  }
}
run "explicit_component_contract" {
  command = plan
  variables {
    observers = {
      azure-a = {
        location = "westus", vm_size = "Standard_B2pts_v2", image_version = "24.04.202610210"
        nic_id   = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/networkInterfaces/nic-pathwatch-probe-azure-a"
        ttl      = "2026-11-28T00:00:00Z", vm_hourly = "0.01", disk_hourly = "0.001"
      }
    }
  }
  assert {
    condition     = azurerm_linux_virtual_machine.probe["azure-a"].os_disk[0].storage_account_type == "Standard_LRS" && azurerm_linux_virtual_machine.probe["azure-a"].os_disk[0].disk_size_gb == 30 && azurerm_linux_virtual_machine.probe["azure-a"].source_image_reference[0].sku == "server-arm64"
    error_message = "Explicit 30GB HDD and compatible architecture required."
  }
  assert {
    condition     = azapi_update_resource.disk_tags["azure-a"].body.tags.owner == "codex-pathwatch"
    error_message = "Implicit OS disk needs explicit ownership tags."
  }
}
