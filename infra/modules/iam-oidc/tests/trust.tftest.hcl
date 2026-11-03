mock_provider "azurerm" {}
variables {
  resource_group_name      = "rg-pathwatch-lab"
  location                 = "westus2"
  tags                     = { project = "pathwatch", owner = "codex-pathwatch", ttl = "2026-10-24T00:00:00Z", est_usd_per_hour = "0" }
  github_repository        = "example/pathwatch"
  state_storage_account_id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Storage/storageAccounts/pwteststate"
}
run "main_only_federation" {
  command = plan
  assert {
    condition     = azurerm_federated_identity_credential.github.issuer == "https://token.actions.githubusercontent.com" && azurerm_federated_identity_credential.github.subject == "repo:example/pathwatch:ref:refs/heads/main" && length(azurerm_federated_identity_credential.github.audience) == 1 && contains(azurerm_federated_identity_credential.github.audience, "api://AzureADTokenExchange")
    error_message = "Issuer, exact main-only subject and audience must be constrained."
  }
  assert {
    condition     = azurerm_user_assigned_identity.ci.resource_group_name == "rg-pathwatch-lab" && azurerm_role_assignment.state.role_definition_name == "Storage Blob Data Contributor" && azurerm_role_assignment.state.scope == var.state_storage_account_id
    error_message = "Identity/state role escaped narrow project scope."
  }
}
run "reject_other_group" {
  command = plan
  variables { resource_group_name = "rg-mlinfra-westus" }
  expect_failures = [var.resource_group_name]
}
run "reject_wildcard_repository" {
  command = plan
  variables { github_repository = "example/*" }
  expect_failures = [var.github_repository]
}
run "reject_state_outside_group" {
  command = plan
  variables { state_storage_account_id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-mlinfra-westus/providers/Microsoft.Storage/storageAccounts/pwteststate" }
  expect_failures = [var.state_storage_account_id]
}
run "reject_missing_tags" {
  command = plan
  variables { tags = { project = "pathwatch" } }
  expect_failures = [var.tags]
}
