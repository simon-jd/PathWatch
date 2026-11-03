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
  subscription_id         = "11111111-1111-1111-1111-111111111111"
  ttl                     = "2026-10-24T08:00:00Z"
  owner_cidr              = "8.8.8.8/32"
  campaign_id             = "11111111-2222-3333-4444-555555555555"
  admin_public_key        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGU1tU6KxrPjsnIB9dI9Zut3zjS9gKVT9ngP3gYOdr1p pathwatch"
  task3_cleanup_confirmed = true
}
run "bounded_dns" {
  command = plan
  assert {
    condition     = module.dns.private_records == { a = "10.10.0.4", b = "10.20.0.4", home = "10.30.0.10" }
    error_message = "Private records must refer only to actual owned addresses."
  }
  assert {
    condition     = module.dns.regions == { a = "westus", b = "swedencentral" }
    error_message = "Only reviewed A/B regions."
  }
}
run "cleanup_required" {
  command = plan
  variables { task3_cleanup_confirmed = false }
  expect_failures = [var.task3_cleanup_confirmed]
}
run "public_owner_only" {
  command = plan
  variables { owner_cidr = "0.0.0.0/0" }
  expect_failures = [var.owner_cidr]
}
