mock_provider "azurerm" {
  override_during = plan
  mock_data "azurerm_resource_group" {
    defaults = {
      id   = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab"
      tags = { project = "pathwatch", owner = "codex-pathwatch", ttl = "2026-10-25T03:03:27Z" }
    }
  }
}
mock_provider "azapi" {}
variables {
  subscription_id                  = "11111111-1111-1111-1111-111111111111"
  location                         = "westus"
  ttl                              = "2026-10-25T02:00:00Z"
  owner_peer_cidrs                 = ["8.8.8.8/32"]
  home_routes_confirmed_no_overlap = true
  admin_public_key                 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU1tU6KxrPjsnIB9dI9Zut3zjS9gKVT9ngP3gYOdr1p pathwatch"
  hourly                           = { vm = "0.07", ip = "0.005", disk = "0.015" }
}
run "hybrid_scoped_tunnels_udr" {
  command = plan
  assert {
    condition     = module.network.subnet_prefixes.nva == "10.10.1.0/27" && module.appliance.private_ips.nva == "10.10.1.4" && module.appliance.private_ips.client == "10.10.0.4"
    error_message = "Actual client must be on workload subnet; NVA static IP reserved."
  }
}
run "missing_owner_refused" {
  command = plan
  variables { owner_peer_cidrs = [] }
  expect_failures = [var.owner_peer_cidrs]
}
