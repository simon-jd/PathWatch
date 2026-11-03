resource "azurerm_user_assigned_identity" "ci" {
  name                = "id-pathwatch-github"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}
resource "azurerm_federated_identity_credential" "github" {
  name      = "github-main"
  parent_id = azurerm_user_assigned_identity.ci.id
  issuer    = "https://token.actions.githubusercontent.com"
  audience  = ["api://AzureADTokenExchange"]
  subject   = "repo:${var.github_repository}:ref:refs/heads/main"
}
# Identity verification and state operations only; no Contributor/Owner role.
resource "azurerm_role_assignment" "state" {
  scope                = var.state_storage_account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ci.principal_id
}
