locals {
  subnet_prefixes = {
    workloads = cidrsubnet(var.address_space, 8, 0)
    nva       = cidrsubnet(var.address_space, 11, 8)
    aks_nodes = cidrsubnet(var.address_space, 4, 1)
  }
  reserved = {
    home         = "10.30.0.0/16"
    wireguard    = "10.255.0.0/24"
    aks_pods     = cidrsubnet(var.address_space, 2, 1)
    aks_services = cidrsubnet(var.address_space, 4, 2)
  }
  # Excluding NVA prevents routing the appliance's own home traffic back to itself.
  routed_subnets = { for key, prefix in local.subnet_prefixes : key => prefix if key != "nva" }
}
data "azurerm_resource_group" "project" {
  name = var.resource_group_name
  lifecycle {
    postcondition {
      condition     = lower(self.id) == lower("/subscriptions/${var.subscription_id}/resourceGroups/rg-pathwatch-lab") && try(self.tags.project == "pathwatch" && self.tags.owner == "codex-pathwatch", false)
      error_message = "Existing group must match the explicit subscription, exact project RG and ownership tags."
    }
  }
}
resource "azurerm_virtual_network" "this" {
  name                = "vnet-${var.name}"
  resource_group_name = data.azurerm_resource_group.project.name
  location            = var.location
  address_space       = [var.address_space]
  tags                = var.tags
}
resource "azurerm_subnet" "this" {
  for_each                        = local.subnet_prefixes
  name                            = "snet-${each.key}"
  resource_group_name             = data.azurerm_resource_group.project.name
  virtual_network_name            = azurerm_virtual_network.this.name
  address_prefixes                = [each.value]
  default_outbound_access_enabled = false
}
resource "azurerm_network_security_group" "this" {
  name                = "nsg-${var.name}"
  resource_group_name = data.azurerm_resource_group.project.name
  location            = var.location
  tags                = var.tags
}
# Azure built-in AllowVNetInBound is 65000. This explicit deny closes it unless
# a narrowly scoped higher-precedence allow below matches the packet.
resource "azurerm_network_security_rule" "deny_ingress" {
  name                        = "deny-all-inbound"
  resource_group_name         = data.azurerm_resource_group.project.name
  network_security_group_name = azurerm_network_security_group.this.name
  priority                    = 4096
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "*"
  destination_address_prefix  = "*"
}
resource "azurerm_network_security_rule" "private" {
  for_each                    = var.private_ingress_rules
  name                        = "allow-private-${each.key}"
  resource_group_name         = data.azurerm_resource_group.project.name
  network_security_group_name = azurerm_network_security_group.this.name
  priority                    = 100 + index(sort(keys(var.private_ingress_rules)), each.key)
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = each.value.protocol
  source_port_range           = "*"
  destination_port_range      = each.value.protocol == "Icmp" ? "*" : null
  destination_port_ranges     = each.value.protocol == "Icmp" ? null : each.value.destination_ports
  source_address_prefix       = each.value.source_prefix
  destination_address_prefix  = local.subnet_prefixes[each.value.destination]
}
resource "azurerm_network_security_rule" "wireguard" {
  for_each                    = var.wireguard_peer_cidrs
  name                        = "allow-wireguard-${index(sort(tolist(var.wireguard_peer_cidrs)), each.key)}"
  resource_group_name         = data.azurerm_resource_group.project.name
  network_security_group_name = azurerm_network_security_group.this.name
  priority                    = 2000 + index(sort(tolist(var.wireguard_peer_cidrs)), each.key)
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Udp"
  source_port_range           = "*"
  destination_port_range      = "51820"
  source_address_prefix       = each.value
  destination_address_prefix  = local.subnet_prefixes.nva
}
resource "azurerm_subnet_network_security_group_association" "this" {
  for_each                  = local.subnet_prefixes
  subnet_id                 = azurerm_subnet.this[each.key].id
  network_security_group_id = azurerm_network_security_group.this.id
}
resource "azurerm_route_table" "this" {
  name                          = "rt-${var.name}"
  resource_group_name           = data.azurerm_resource_group.project.name
  location                      = var.location
  bgp_route_propagation_enabled = false
  tags                          = var.tags
}
resource "azurerm_subnet_route_table_association" "this" {
  for_each       = local.routed_subnets
  subnet_id      = azurerm_subnet.this[each.key].id
  route_table_id = azurerm_route_table.this.id
}
resource "azurerm_route" "home" {
  for_each               = var.nva_private_ip == null ? {} : { home = local.reserved.home, wireguard = local.reserved.wireguard }
  name                   = "to-${each.key}"
  resource_group_name    = data.azurerm_resource_group.project.name
  route_table_name       = azurerm_route_table.this.name
  address_prefix         = each.value
  next_hop_type          = "VirtualAppliance"
  next_hop_in_ip_address = var.nva_private_ip
}
resource "azurerm_network_security_rule" "wireguard_alternate" {
  for_each                    = var.wireguard_alternate_enabled ? var.wireguard_peer_cidrs : toset([])
  name                        = "allow-wireguard-alternate-${index(sort(tolist(var.wireguard_peer_cidrs)), each.key)}"
  resource_group_name         = data.azurerm_resource_group.project.name
  network_security_group_name = azurerm_network_security_group.this.name
  priority                    = 2200 + index(sort(tolist(var.wireguard_peer_cidrs)), each.key)
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Udp"
  source_port_range           = "*"
  destination_port_range      = "51821"
  source_address_prefix       = each.value
  destination_address_prefix  = local.subnet_prefixes.nva
}
resource "azurerm_network_security_rule" "management" {
  for_each                     = var.management_peer_cidrs
  name                         = "allow-owner-ssh-${index(sort(tolist(var.management_peer_cidrs)), each.key)}"
  resource_group_name          = data.azurerm_resource_group.project.name
  network_security_group_name  = azurerm_network_security_group.this.name
  priority                     = 2400 + index(sort(tolist(var.management_peer_cidrs)), each.key)
  direction                    = "Inbound"
  access                       = "Allow"
  protocol                     = "Tcp"
  source_port_range            = "*"
  destination_port_range       = "22"
  source_address_prefix        = each.value
  destination_address_prefixes = [local.subnet_prefixes.nva, local.subnet_prefixes.workloads]
}
# NSG sees the original home destination when a workload UDR delivers traffic to
# the NVA NIC. An allow whose destination is the NVA subnet would not match.
resource "azurerm_network_security_rule" "hybrid_forwarding" {
  for_each                    = var.hybrid_forwarding_enabled ? { icmp = { protocol = "Icmp", port = "*" }, iperf = { protocol = "Tcp", port = "5201" } } : {}
  name                        = "allow-hybrid-forward-${each.key}"
  resource_group_name         = data.azurerm_resource_group.project.name
  network_security_group_name = azurerm_network_security_group.this.name
  priority                    = 2600 + index(["icmp", "iperf"], each.key)
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = each.value.protocol
  source_port_range           = "*"
  destination_port_range      = each.value.port
  source_address_prefix       = local.subnet_prefixes.workloads
  destination_address_prefix  = local.reserved.home
}
# Decapsulated traffic leaves the NVA with its original home source. Without a
# home UDR on the appliance subnet, VirtualNetwork need not include that source;
# Azure's default AllowVnetOutBound is therefore insufficient for new home flows.
resource "azurerm_network_security_rule" "hybrid_outbound" {
  for_each                    = var.hybrid_forwarding_enabled ? { icmp = { protocol = "Icmp", port = "*" }, iperf = { protocol = "Tcp", port = "5201" } } : {}
  name                        = "allow-hybrid-outbound-${each.key}"
  resource_group_name         = data.azurerm_resource_group.project.name
  network_security_group_name = azurerm_network_security_group.this.name
  priority                    = 2600 + index(["icmp", "iperf"], each.key)
  direction                   = "Outbound"
  access                      = "Allow"
  protocol                    = each.value.protocol
  source_port_range           = "*"
  destination_port_range      = each.value.port
  source_address_prefix       = local.reserved.home
  destination_address_prefix  = local.subnet_prefixes.workloads
}
