mock_provider "azurerm" {
  mock_data "azurerm_resource_group" {
    defaults = {
      id   = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab"
      name = "rg-pathwatch-lab"
      tags = { project = "pathwatch", owner = "codex-pathwatch", ttl = "2026-12-01T00:00:00Z", est_usd_per_hour = "0.001" }
    }
  }
}
mock_provider "azapi" {}
variables {
  subscription_id                  = "11111111-1111-1111-1111-111111111111"
  probe_site                       = "azure-a"
  location                         = "westus"
  ttl                              = "2026-10-25T00:00:00Z"
  parent_ttl                       = "2026-12-01T00:00:00Z"
  campaign_id                      = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
  vm_size                          = "Standard_B2pts_v2"
  image_version                    = "24.04.202610210"
  admin_public_key                 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU1tU6KxrPjsnIB9dI9Zut3zjS9gKVT9ngP3gYOdr1p pathwatch"
  hourly                           = { vm = "0.012", ip = "0.005", disk = "0.003" }
  owner_peer_cidrs                 = ["8.8.8.8/32"]
  home_routes_confirmed_no_overlap = true
}
run "single_probe_including_owned_new_network" {
  command = plan
  assert {
    condition     = azurerm_network_interface.probe.ip_forwarding_enabled && azurerm_network_interface.probe.ip_configuration[0].private_ip_address == "10.10.1.4" && length(module.probe.probe_ids) == 1 && contains(module.probe.probe_ids, "azure-a")
    error_message = "Exactly one A VM doubles as NVA; NIC is owned in this new state."
  }
}
run "single_temporary_b_including_owned_new_network" {
  command = plan
  variables {
    probe_site  = "azure-b"
    location    = "swedencentral"
    campaign_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
  }
  assert {
    condition     = azurerm_network_interface.probe.ip_configuration[0].private_ip_address == "10.20.1.4" && length(module.probe.probe_ids) == 1 && contains(module.probe.probe_ids, "azure-b")
    error_message = "B uses a distinct single temporary VM and newly owned NIC/network."
  }
}
