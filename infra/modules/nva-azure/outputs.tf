output "public_ips" { value = { for key, ip in azurerm_public_ip.this : key => ip.ip_address } }
output "private_ips" { value = { for key, nic in azurerm_network_interface.this : key => nic.ip_configuration[0].private_ip_address } }
output "vm_ids" { value = { for key, vm in azurerm_linux_virtual_machine.this : key => vm.id } }
output "disk_ids" { value = { for key, disk in azapi_update_resource.disk_tags : key => disk.resource_id } }

output "public_ip_ids" { value = { for key, ip in azurerm_public_ip.this : key => ip.id } }
output "public_fqdns" { value = { for key, ip in azurerm_public_ip.this : key => ip.fqdn } }
