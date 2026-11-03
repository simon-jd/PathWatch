provider "azurerm" {
  features {}
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
}
module "network" {
  source          = "../../../modules/network-azure"
  subscription_id = var.subscription_id
  name            = var.name
  location        = var.location
  address_space   = var.address_space
  tags            = { project = "pathwatch", owner = "codex-pathwatch", ttl = var.ttl, est_usd_per_hour = "0" }
  private_ingress_rules = {
    metrics = { source_prefix = var.address_space, destination = "workloads", protocol = "Tcp", destination_ports = ["9090"] }
  }
  wireguard_peer_cidrs             = var.wireguard_peer_cidrs
  nva_private_ip                   = var.nva_private_ip
  home_routes_confirmed_no_overlap = var.home_routes_confirmed_no_overlap
}
