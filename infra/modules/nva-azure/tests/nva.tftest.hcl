mock_provider "azurerm" {}
mock_provider "azapi" {}
variables {
  subscription_id  = "11111111-1111-1111-1111-111111111111"
  location         = "westus"
  ttl              = "2026-10-25T02:00:00Z"
  admin_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU1tU6KxrPjsnIB9dI9Zut3zjS9gKVT9ngP3gYOdr1p pathwatch"
  hourly           = { vm = "0.02", ip = "0.01", disk = "0.001" }
}
run "nva_contract" {
  command = plan
  assert {
    condition     = azurerm_network_interface.this["nva"].ip_forwarding_enabled && azurerm_network_interface.this["nva"].ip_configuration[0].private_ip_address == "10.10.1.4"
    error_message = "NVA needs static reserved IP and forwarding."
  }
  assert {
    condition     = azurerm_linux_virtual_machine.this["nva"].size == "Standard_B2pts_v2" && azurerm_linux_virtual_machine.this["nva"].os_disk[0].storage_account_type == "Standard_LRS" && azurerm_linux_virtual_machine.this["nva"].os_disk[0].disk_size_gb == 30
    error_message = "Small ARM VM must use explicit 30GB Standard HDD."
  }
  assert {
    condition     = azurerm_linux_virtual_machine.this["nva"].source_image_reference[0].sku == "server-arm64" && azurerm_linux_virtual_machine.this["nva"].source_image_reference[0].version == "24.04.202610210"
    error_message = "Pinned compatible image required."
  }
  assert {
    condition     = azapi_update_resource.disk_tags["nva"].resource_id == "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Compute/disks/os-pathwatch-hybrid-nva" && azapi_update_resource.disk_tags["nva"].body.tags.owner == "codex-pathwatch"
    error_message = "Implicit managed disk requires actual four-tag write."
  }
}
run "client" {
  command = plan
  variables { include_client = true }
  assert {
    condition     = length(azurerm_linux_virtual_machine.this) == 2 && !azurerm_network_interface.this["client"].ip_forwarding_enabled && azurerm_network_interface.this["client"].ip_configuration[0].private_ip_address == "10.10.0.4"
    error_message = "Temporary client must be in workload subnet with no IP forwarding."
  }
}
run "bad_sku" {
  command = plan
  variables { vm_size = "Standard_NC6" }
  expect_failures = [var.vm_size]
}
run "bad_disk" {
  command = plan
  variables { disk_size_gb = 64 }
  expect_failures = [var.disk_size_gb]
}
run "bad_region" {
  command = plan
  variables { location = "westus2" }
  expect_failures = [var.location]
}
