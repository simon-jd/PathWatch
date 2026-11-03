output "runner_id" { value = azurerm_linux_virtual_machine.runner.id }
output "disk_id" { value = azapi_update_resource.disk_tags.resource_id }
output "public_ip" { value = azurerm_public_ip.runner.ip_address }
