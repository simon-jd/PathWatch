mock_provider "azurerm" {}
variables {
  subscription_id       = "11111111-1111-1111-1111-111111111111"
  location              = "westus2"
  ttl                   = "2026-10-24T00:00:00Z"
  est_usd_per_hour      = 0.001
  storage_account_name  = "pwteststateunique"
  operator_principal_id = "22222222-2222-2222-2222-222222222222"
  operator_ipv4_cidrs   = ["203.0.113.10/32"]
}
run "azure_state_contract" {
  command = plan
  assert {
    condition     = azurerm_resource_group.project.name == "rg-pathwatch-lab" && azurerm_storage_account.state.resource_group_name == "rg-pathwatch-lab"
    error_message = "State must live only in project RG."
  }
  assert {
    condition     = azurerm_storage_account.state.account_tier == "Standard" && azurerm_storage_account.state.account_replication_type == "LRS" && !azurerm_storage_account.state.shared_access_key_enabled && azurerm_storage_account.state.default_to_oauth_authentication && !azurerm_storage_account.state.allow_nested_items_to_be_public
    error_message = "Budget and Entra-only auth contract violated."
  }
  assert {
    condition     = azurerm_storage_account.state.blob_properties[0].versioning_enabled && azurerm_storage_account.state.blob_properties[0].delete_retention_policy[0].days == 7 && azurerm_storage_account.state.blob_properties[0].container_delete_retention_policy[0].days == 7
    error_message = "State recovery configuration missing."
  }
  assert {
    condition     = azurerm_storage_account.state.network_rules[0].default_action == "Deny" && azurerm_storage_account.state.tags.project == "pathwatch" && azurerm_storage_account.state.tags.owner == "codex-pathwatch" && azurerm_resource_group.project.tags.ttl == var.ttl
    error_message = "Ownership/expiry/network contract violated."
  }
}
run "reject_invalid_ttl" {
  command = plan
  variables { ttl = "2026-02-30T00:00:00Z" }
  expect_failures = [var.ttl]
}
run "reject_world_access" {
  command = plan
  variables { operator_ipv4_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.operator_ipv4_cidrs]
}
