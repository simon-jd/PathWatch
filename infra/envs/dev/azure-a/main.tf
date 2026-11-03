provider "azurerm" {
  features {}
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
}
module "network" {
  source              = "../../../modules/network-azure"
  subscription_id     = var.subscription_id
  resource_group_name = "rg-pathwatch-lab"
  name                = "pathwatch-azure-a"
  location            = var.location
  address_space       = "10.10.0.0/16"
  tags                = { project = "pathwatch", owner = "codex-pathwatch", ttl = var.ttl, est_usd_per_hour = "0" }
  private_ingress_rules = {
    metrics    = { source_prefix = "10.10.0.0/16", destination = "workloads", protocol = "Tcp", destination_ports = ["9090"] }
    probe-icmp = { source_prefix = "10.10.0.0/16", destination = "workloads", protocol = "Icmp" }
  }
  wireguard_peer_cidrs             = var.wireguard_peer_cidrs
  nva_private_ip                   = var.nva_private_ip
  home_routes_confirmed_no_overlap = var.home_routes_confirmed_no_overlap
}
