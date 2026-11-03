output "vnet_id" {
  description = "Regional vnet_id."
  value       = module.network.vnet_id
}
output "vnet_name" {
  description = "Regional vnet_name."
  value       = module.network.vnet_name
}
output "subnet_ids" {
  description = "Regional subnet_ids."
  value       = module.network.subnet_ids
}
output "subnet_names" {
  description = "Regional subnet_names."
  value       = module.network.subnet_names
}
output "subnet_prefixes" {
  description = "Regional subnet_prefixes."
  value       = module.network.subnet_prefixes
}
output "nsg_id" {
  description = "Regional nsg_id."
  value       = module.network.nsg_id
}
output "nsg_name" {
  description = "Regional nsg_name."
  value       = module.network.nsg_name
}
output "route_table_id" {
  description = "Regional route_table_id."
  value       = module.network.route_table_id
}
output "route_table_name" {
  description = "Regional route_table_name."
  value       = module.network.route_table_name
}
output "address_spaces" {
  description = "Regional address_spaces."
  value       = module.network.address_spaces
}
output "reserved_address_spaces" {
  description = "Regional reserved_address_spaces."
  value       = module.network.reserved_address_spaces
}
output "location" {
  description = "Regional location."
  value       = module.network.location
}
output "resource_group_name" {
  description = "Regional resource_group_name."
  value       = module.network.resource_group_name
}
