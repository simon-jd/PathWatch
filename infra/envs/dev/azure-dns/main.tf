provider "azurerm" {
  features {
    virtual_machine { delete_os_disk_on_deletion = true }
  }
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
}
provider "azapi" {
  subscription_id            = var.subscription_id
  enable_preflight           = false
  skip_provider_registration = true
}
module "dns" {
  source           = "../../../modules/dns-azure"
  subscription_id  = var.subscription_id
  ttl              = var.ttl
  owner_cidr       = var.owner_cidr
  admin_public_key = var.admin_public_key
  hourly           = var.hourly
}
output "inventory" { value = merge(module.dns.inventory, { campaign_id = var.campaign_id, task3_cleanup_confirmed = var.task3_cleanup_confirmed }) }
