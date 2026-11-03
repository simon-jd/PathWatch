output "regions" { value = local.regions }
output "private_records" { value = local.records }
output "inventory" {
  value = {
    scope           = "pathwatch-dns-owned"
    subscription_id = var.subscription_id
    ttl             = var.ttl
    owner_cidr      = var.owner_cidr
    zone            = local.zone
    tm_name         = local.label
    tm_fqdn         = "${local.label}.trafficmanager.net"
    sites = { for site, region in local.regions : site => {
      region     = region
      vm_id      = "${local.prefix}/Microsoft.Compute/virtualMachines/vm-pathwatch-dns-${site}"
      public_ip  = azurerm_public_ip.this[site].ip_address
      private_ip = local.ips[site]
    } }
  }
}
