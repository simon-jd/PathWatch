mock_provider "azurerm" {
  override_during = plan
  mock_data "azurerm_resource_group" {
    defaults = {
      id   = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab"
      tags = { project = "pathwatch", owner = "codex-pathwatch", ttl = "2026-10-25T03:03:27Z" }
    }
  }
  mock_resource "azurerm_private_dns_zone_virtual_network_link" {
    defaults = { resolution_policy = "" }
  }
}
mock_provider "azapi" {}
variables {
  subscription_id  = "11111111-1111-1111-1111-111111111111"
  ttl              = "2026-10-24T08:00:00Z"
  owner_cidr       = "8.8.8.8/32"
  admin_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU1tU6KxrPjsnIB9dI9Zut3zjS9gKVT9ngP3gYOdr1p pathwatch"
}
run "custom_zone_omits_private_link_only_resolution_policy" {
  command = plan
  module { source = "../../../modules/dns-azure" }
  assert {
    condition     = alltrue([for link in azurerm_private_dns_zone_virtual_network_link.this : link.resolution_policy == ""])
    error_message = "Custom DNS zone must leave resolution_policy unset; Default is only for privatelink zones."
  }
}
