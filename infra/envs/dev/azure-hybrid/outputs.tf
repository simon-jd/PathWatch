output "public_ips" { value = module.appliance.public_ips }
output "private_ips" { value = module.appliance.private_ips }
output "disk_ids" { value = module.appliance.disk_ids }
output "vm_ids" { value = module.appliance.vm_ids }
output "network" { value = { vnet_id = module.network.vnet_id, subnet_ids = module.network.subnet_ids, route_table_id = module.network.route_table_id } }

output "public_ip_ids" { value = module.appliance.public_ip_ids }
output "public_fqdns" { value = module.appliance.public_fqdns }
