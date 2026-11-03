output "vnet_id" {
  description = "VNet ARM ID for Task 3/10/14 consumers."
  value       = azurerm_virtual_network.this.id
}
output "vnet_name" {
  description = "Regional VNet name."
  value       = azurerm_virtual_network.this.name
}
output "subnet_ids" {
  description = "Subnet ARM IDs keyed by workloads, nva and aks_nodes."
  value       = { for key, subnet in azurerm_subnet.this : key => subnet.id }
}
output "subnet_names" {
  description = "Subnet names keyed by purpose."
  value       = { for key, subnet in azurerm_subnet.this : key => subnet.name }
}
output "subnet_prefixes" {
  description = "Disjoint allocated subnet CIDRs keyed by purpose."
  value       = local.subnet_prefixes
}
output "nsg_id" {
  description = "Common explicit default-deny inbound NSG ARM ID."
  value       = azurerm_network_security_group.this.id
}
output "nsg_name" {
  description = "Regional NSG name."
  value       = azurerm_network_security_group.this.name
}
output "route_table_id" {
  description = "Workload/AKS UDR table ARM ID; not associated with NVA subnet."
  value       = azurerm_route_table.this.id
}
output "route_table_name" {
  description = "Regional route table name."
  value       = azurerm_route_table.this.name
}
output "address_spaces" {
  description = "Reserved regional VNet address spaces."
  value       = azurerm_virtual_network.this.address_space
}
output "reserved_address_spaces" {
  description = "Unallocated AKS pods/services, home and WireGuard prefixes; no connectivity implied."
  value       = local.reserved
}
output "location" {
  description = "Actual VNet location, with no VM SKU availability claim."
  value       = azurerm_virtual_network.this.location
}
output "resource_group_name" {
  description = "Existing shared group owned by bootstrap, not by this module."
  value       = data.azurerm_resource_group.project.name
}
