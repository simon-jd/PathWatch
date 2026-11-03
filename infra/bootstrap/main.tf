# Initial state is LOCAL. Migrate explicitly using backend.azurerm.tf.example after reviewed apply.
provider "azurerm" {
  features {}
  subscription_id                 = var.subscription_id
  storage_use_azuread             = true
  resource_provider_registrations = "none"
}
locals {
  tags = { project = "pathwatch", owner = "codex-pathwatch", ttl = var.ttl, est_usd_per_hour = tostring(var.est_usd_per_hour) }
}
resource "azurerm_resource_group" "project" {
  name     = "rg-pathwatch-lab"
  location = var.location
  tags     = local.tags
}
resource "azurerm_storage_account" "state" {
  #checkov:skip=CKV_AZURE_33:No queues used; queue diagnostic logging would add unrelated storage costs.
  #checkov:skip=CKV_AZURE_206:Standard_LRS is required by the approved budget; ZRS is not funded.
  #checkov:skip=CKV_AZURE_59:Public endpoint is limited by default-deny IPv4 ACL; no funded private endpoint/VPN.
  #checkov:skip=CKV2_AZURE_33:Private endpoint requires unapproved billable networking; Entra auth plus default-deny ACL.
  #checkov:skip=CKV2_AZURE_1:Microsoft managed encryption avoids a billable Key Vault outside Task 1.
  #checkov:skip=CKV_AZURE_36:No trusted-service bypass; only explicitly authorized operator IPv4 addresses may use the state endpoint.
  name                             = var.storage_account_name
  resource_group_name              = azurerm_resource_group.project.name
  location                         = azurerm_resource_group.project.location
  account_tier                     = "Standard"
  account_replication_type         = "LRS"
  account_kind                     = "StorageV2"
  min_tls_version                  = "TLS1_2"
  https_traffic_only_enabled       = true
  shared_access_key_enabled        = false
  default_to_oauth_authentication  = true
  allow_nested_items_to_be_public  = false
  public_network_access_enabled    = true
  cross_tenant_replication_enabled = false
  tags                             = local.tags
  network_rules {
    default_action = "Deny"
    bypass         = ["None"]
    ip_rules       = [for ip in var.operator_ipv4_cidrs : trimsuffix(ip, "/32")]
  }
  blob_properties {
    versioning_enabled            = true
    change_feed_enabled           = true
    change_feed_retention_in_days = 7
    delete_retention_policy { days = 7 }
    container_delete_retention_policy { days = 7 }
  }
}
resource "azurerm_role_assignment" "operator_state" {
  scope                = azurerm_storage_account.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.operator_principal_id
}
resource "azurerm_storage_container" "state" {
  #checkov:skip=CKV2_AZURE_21:Read-request diagnostics require separately priced logs; bounded state retains versions and soft-delete.
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.state.id
  container_access_type = "private"
  depends_on            = [azurerm_role_assignment.operator_state]
}
