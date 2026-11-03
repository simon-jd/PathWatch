# Native provider-schema state/plan representations; mock apply never contacts Azure.
mock_provider "azurerm" {
  mock_resource "azurerm_resource_group" {
    defaults = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab" }
  }
  mock_resource "azurerm_storage_account" {
    defaults = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Storage/storageAccounts/pwteststate" }
  }
  mock_resource "azurerm_storage_container" {
    defaults = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Storage/storageAccounts/pwteststate/blobServices/default/containers/tfstate" }
  }
  mock_resource "azurerm_role_assignment" {
    defaults = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Storage/storageAccounts/pwteststate/providers/Microsoft.Authorization/roleAssignments/22222222-2222-2222-2222-222222222222" }
  }
}
variables {
  subscription_id       = "11111111-1111-1111-1111-111111111111"
  location              = "westus"
  ttl                   = "2026-10-25T03:03:27Z"
  est_usd_per_hour      = 0.001
  storage_account_name  = "pwteststate"
  operator_principal_id = "33333333-3333-3333-3333-333333333333"
  operator_ipv4_cidrs   = ["8.8.8.8/32"]
}
run "seed_mock_state" {
  command = apply
}
run "parent_ttl_renewal" {
  command = plan
  variables { ttl = "2026-11-27T19:59:00Z" }
  assert {
    condition     = azurerm_storage_account.state.tags.ttl == "2026-11-27T19:59:00Z" && azurerm_resource_group.project.tags.ttl == "2026-11-27T19:59:00Z"
    error_message = "Both bootstrap parents renew together."
  }
}
