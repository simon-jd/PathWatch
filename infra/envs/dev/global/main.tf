provider "azurerm" {
  features {}
  subscription_id                 = var.subscription_id
  storage_use_azuread             = true
  resource_provider_registrations = "none"
}
module "iam_oidc" {
  source                   = "../../../modules/iam-oidc"
  resource_group_name      = "rg-pathwatch-lab"
  location                 = var.location
  tags                     = { project = "pathwatch", owner = "codex-pathwatch", ttl = var.ttl, est_usd_per_hour = tostring(var.est_usd_per_hour) }
  github_repository        = var.github_repository
  state_storage_account_id = var.state_storage_account_id
}
