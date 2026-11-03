mock_provider "azurerm" {
  override_during = plan
  mock_data "azurerm_resource_group" {
    defaults = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab", tags = { project = "pathwatch", owner = "codex-pathwatch" } }
  }
}
variables {
  subscription_id = "11111111-1111-1111-1111-111111111111"
  ttl             = "2026-10-24T00:00:00Z"
}
run "fixed_regional_contract" {
  command = plan
  assert {
    condition     = module.network.location == "francecentral" && module.network.address_spaces == toset(["10.20.0.0/16"]) && length(module.network.subnet_ids) == 3 && module.network.resource_group_name == "rg-pathwatch-lab"
    error_message = "Regional location, assigned CIDR, subnets and existing group contract changed."
  }
}
