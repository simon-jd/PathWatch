# PENDING execution: native provider binaries are deliberately not installed.
mock_provider "azurerm" {}
mock_provider "azapi" {}
variables {
  subscription_id    = "11111111-1111-1111-1111-111111111111"
  run_id             = "test0001"
  ttl                = "2026-10-25T02:00:00Z"
  operator_ipv4_cidr = "8.8.8.8/32"
  admin_public_key   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU1tU6KxrPjsnIB9dI9Zut3zjS9gKVT9ngP3gYOdr1p pathwatch"
  hourly             = { vm = "0.17", ip = "0.005", disk = "0.505" }
}
run "exact_arm_runner" {
  command = plan
  assert {
    condition     = azurerm_linux_virtual_machine.runner.size == "Standard_B2ps_v2" && azurerm_linux_virtual_machine.runner.os_disk[0].storage_account_type == "Standard_LRS" && azurerm_linux_virtual_machine.runner.os_disk[0].disk_size_gb == 32
    error_message = "Pinned ARM/HDD profile required."
  }
  assert {
    condition     = !azurerm_network_interface.runner.accelerated_networking_enabled && length(azurerm_network_security_group.runner.security_rule) == 1 && azapi_update_resource.disk_tags.type == "Microsoft.Compute/disks@2024-03-02"
    error_message = "No accelerated networking, extra ingress or arbitrary patch."
  }
}
run "reject_region" {
  command = plan
  variables { location = "westus2" }
  expect_failures = [var.location]
}
run "reject_scope" {
  command = plan
  variables { xdp_scope = "legacy" }
  expect_failures = [var.xdp_scope]
}
