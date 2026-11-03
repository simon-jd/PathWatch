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
module "network" {
  source                           = "../../../modules/network-azure"
  subscription_id                  = var.subscription_id
  resource_group_name              = "rg-pathwatch-lab"
  name                             = "pathwatch-azure-a"
  location                         = var.location
  address_space                    = "10.10.0.0/16"
  tags                             = { project = "pathwatch", owner = "codex-pathwatch", ttl = var.ttl, est_usd_per_hour = "0" }
  wireguard_peer_cidrs             = var.owner_peer_cidrs
  wireguard_alternate_enabled      = true
  hybrid_forwarding_enabled        = true
  management_peer_cidrs            = var.owner_peer_cidrs
  nva_private_ip                   = "10.10.1.4"
  home_routes_confirmed_no_overlap = var.home_routes_confirmed_no_overlap
  private_ingress_rules = {
    home-icmp     = { source_prefix = "10.30.0.0/16", destination = "workloads", protocol = "Icmp" }
    home-iperf    = { source_prefix = "10.30.0.0/16", destination = "workloads", protocol = "Tcp", destination_ports = ["5201"] }
    workload-icmp = { source_prefix = "10.10.0.0/24", destination = "nva", protocol = "Icmp" }
  }
}
module "appliance" {
  source           = "../../../modules/nva-azure"
  subscription_id  = var.subscription_id
  location         = var.location
  ttl              = var.ttl
  admin_public_key = var.admin_public_key
  hourly           = var.hourly
  include_client   = var.include_client
  depends_on       = [module.network]
}
