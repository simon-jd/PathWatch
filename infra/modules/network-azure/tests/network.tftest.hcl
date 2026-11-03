mock_provider "azurerm" {
  override_during = plan
  mock_data "azurerm_resource_group" {
    defaults = {
      id   = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab"
      tags = { project = "pathwatch", owner = "codex-pathwatch" }
    }
  }
  mock_resource "azurerm_virtual_network" {
    defaults = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/virtualNetworks/vnet-pathwatch-test" }
  }
  mock_resource "azurerm_network_security_group" {
    defaults = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/networkSecurityGroups/nsg-pathwatch-test" }
  }
  mock_resource "azurerm_route_table" {
    defaults = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/routeTables/rt-pathwatch-test" }
  }
}
override_resource {
  target          = azurerm_subnet.this["workloads"]
  override_during = plan
  values          = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/virtualNetworks/vnet-pathwatch-test/subnets/snet-workloads" }
}
override_resource {
  target          = azurerm_subnet.this["nva"]
  override_during = plan
  values          = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/virtualNetworks/vnet-pathwatch-test/subnets/snet-nva" }
}
override_resource {
  target          = azurerm_subnet.this["aks_nodes"]
  override_during = plan
  values          = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Network/virtualNetworks/vnet-pathwatch-test/subnets/snet-aks_nodes" }
}
variables {
  subscription_id = "11111111-1111-1111-1111-111111111111"
  name            = "pathwatch-test"
  location        = "westus"
  address_space   = "10.10.0.0/16"
  tags            = { project = "pathwatch", owner = "codex-pathwatch", ttl = "2026-10-24T00:00:00Z", est_usd_per_hour = "0" }
  private_ingress_rules = {
    metrics = { source_prefix = "10.10.0.0/24", destination = "workloads", protocol = "Tcp", destination_ports = ["9090"] }
  }
  wireguard_peer_cidrs = ["1.1.1.1/32"]
}
run "regional_network_contract" {
  command = plan
  assert {
    condition     = azurerm_virtual_network.this.address_space == toset(["10.10.0.0/16"]) && { for key, subnet in azurerm_subnet.this : key => subnet.address_prefixes } == { workloads = tolist(["10.10.0.0/24"]), nva = tolist(["10.10.1.0/27"]), aks_nodes = tolist(["10.10.16.0/20"]) }
    error_message = "Require disjoint in-/16 workload, NVA and future AKS node subnets."
  }
  assert {
    condition     = output.reserved_address_spaces == { home = "10.30.0.0/16", wireguard = "10.255.0.0/24", aks_pods = "10.10.64.0/18", aks_services = "10.10.32.0/20" } && alltrue([for subnet in azurerm_subnet.this : !subnet.default_outbound_access_enabled])
    error_message = "Reserve home/transit/pod/service space; disable implicit outbound IPs."
  }
  assert {
    condition     = azurerm_network_security_rule.deny_ingress.access == "Deny" && azurerm_network_security_rule.deny_ingress.direction == "Inbound" && azurerm_network_security_rule.deny_ingress.priority == 4096 && azurerm_network_security_rule.deny_ingress.source_address_prefix == "*" && azurerm_network_security_rule.deny_ingress.destination_address_prefix == "*" && azurerm_network_security_rule.deny_ingress.protocol == "*" && azurerm_network_security_rule.deny_ingress.destination_port_range == "*"
    error_message = "Explicit deny must override Azure's built-in AllowVNetInBound."
  }
  assert {
    condition     = azurerm_network_security_rule.private["metrics"].source_address_prefix == "10.10.0.0/24" && azurerm_network_security_rule.private["metrics"].destination_address_prefix == "10.10.0.0/24" && azurerm_network_security_rule.private["metrics"].destination_port_ranges == toset(["9090"]) && azurerm_network_security_rule.private["metrics"].priority < 4096 && azurerm_network_security_rule.wireguard["1.1.1.1/32"].source_address_prefix == "1.1.1.1/32" && azurerm_network_security_rule.wireguard["1.1.1.1/32"].destination_address_prefix == "10.10.1.0/27" && azurerm_network_security_rule.wireguard["1.1.1.1/32"].destination_port_range == "51820" && azurerm_network_security_rule.wireguard["1.1.1.1/32"].protocol == "Udp"
    error_message = "Private and WireGuard allows must be source/destination/port scoped."
  }
  assert {
    condition     = length(azurerm_subnet_network_security_group_association.this) == 3 && alltrue([for key, association in azurerm_subnet_network_security_group_association.this : association.network_security_group_id == azurerm_network_security_group.this.id && association.subnet_id == azurerm_subnet.this[key].id]) && toset(keys(azurerm_subnet_route_table_association.this)) == toset(["workloads", "aks_nodes"]) && alltrue([for key, association in azurerm_subnet_route_table_association.this : association.route_table_id == azurerm_route_table.this.id && association.subnet_id == azurerm_subnet.this[key].id]) && length(azurerm_route.home) == 0
    error_message = "All subnets need NSG; workload/node UDRs must exclude the NVA and have no routes until explicitly configured."
  }
  assert {
    condition     = alltrue([for resource in [azurerm_virtual_network.this, azurerm_network_security_group.this, azurerm_route_table.this] : resource.resource_group_name == "rg-pathwatch-lab" && resource.location == "westus" && resource.tags == var.tags])
    error_message = "Every taggable parent must carry exact ownership, expiry and zero-cost estimate in project RG."
  }
}
run "conditional_home_routes" {
  command = plan
  variables {
    nva_private_ip                   = "10.10.1.4"
    home_routes_confirmed_no_overlap = true
  }
  assert {
    condition     = length(azurerm_route.home) == 2 && toset([for route in azurerm_route.home : route.address_prefix]) == toset(["10.30.0.0/16", "10.255.0.0/24"]) && alltrue([for route in azurerm_route.home : route.next_hop_type == "VirtualAppliance" && route.next_hop_in_ip_address == "10.10.1.4"])
    error_message = "Only home/transit routes may point to the explicit in-subnet NVA; never install a default route."
  }
}
run "reject_foreign_group" {
  command = plan
  variables { resource_group_name = "rg-mlinfra-westus" }
  expect_failures = [var.resource_group_name]
}
run "reject_foreign_subscription" {
  command = plan
  variables { subscription_id = "22222222-2222-2222-2222-222222222222" }
  expect_failures = [data.azurerm_resource_group.project]
}
run "reject_foreign_ownership" {
  command = plan
  override_data {
    target = data.azurerm_resource_group.project
    values = { id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-pathwatch-lab", tags = { project = "warden", owner = "codex-warden" } }
  }
  expect_failures = [data.azurerm_resource_group.project]
}
run "reject_invalid_cidr" {
  command = plan
  variables { address_space = "10.30.0.0/16" }
  expect_failures = [var.address_space]
}
run "reject_disallowed_region" {
  command = plan
  variables { location = "westus2" }
  expect_failures = [var.location]
}
run "reject_open_admin" {
  command = plan
  variables { private_ingress_rules = { ssh = { source_prefix = "0.0.0.0/0", destination = "workloads", protocol = "Tcp", destination_ports = ["22"] } } }
  expect_failures = [var.private_ingress_rules]
}
run "reject_private_admin" {
  command = plan
  variables { private_ingress_rules = { ssh = { source_prefix = "10.10.0.0/24", destination = "workloads", protocol = "Tcp", destination_ports = ["22"] } } }
  expect_failures = [var.private_ingress_rules]
}
run "reject_open_wireguard" {
  command = plan
  variables { wireguard_peer_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.wireguard_peer_cidrs]
}
run "reject_unconfirmed_home_routes" {
  command = plan
  variables { nva_private_ip = "10.10.1.4" }
  expect_failures = [var.nva_private_ip]
}
run "reject_foreign_nva" {
  command = plan
  variables {
    nva_private_ip                   = "10.20.1.4"
    home_routes_confirmed_no_overlap = true
  }
  expect_failures = [var.nva_private_ip]
}
run "reject_missing_tags" {
  command = plan
  variables { tags = { project = "pathwatch" } }
  expect_failures = [var.tags]
}
run "reject_unowned_private_source" {
  command = plan
  variables { private_ingress_rules = { metrics = { source_prefix = "10.0.0.0/8", destination = "workloads", protocol = "Tcp", destination_ports = ["9090"] } } }
  expect_failures = [var.private_ingress_rules]
}
run "reject_nonpublic_wireguard" {
  command = plan
  variables { wireguard_peer_cidrs = ["100.64.1.1/32"] }
  expect_failures = [var.wireguard_peer_cidrs]
}
run "dual_wireguard_and_owner_management" {
  command = plan
  variables {
    wireguard_alternate_enabled = true
    hybrid_forwarding_enabled   = true
    management_peer_cidrs       = ["8.8.8.8/32"]
  }
  assert {
    condition     = azurerm_network_security_rule.wireguard_alternate["1.1.1.1/32"].destination_port_range == "51821" && azurerm_network_security_rule.wireguard_alternate["1.1.1.1/32"].source_address_prefix == "1.1.1.1/32"
    error_message = "Alternate WG must remain UDP51821 scoped peer."
  }
  assert {
    condition     = azurerm_network_security_rule.management["8.8.8.8/32"].source_address_prefix == "8.8.8.8/32" && azurerm_network_security_rule.management["8.8.8.8/32"].destination_port_range == "22" && toset(azurerm_network_security_rule.management["8.8.8.8/32"].destination_address_prefixes) == toset(["10.10.1.0/27", "10.10.0.0/24"])
    error_message = "Management is exact owner/32 SSH to NVA+workloads only."
  }
}
run "world_management_refused" {
  command = plan
  variables { management_peer_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.management_peer_cidrs]
}

run "home_destination_transit_nsg" {
  command = plan
  variables { hybrid_forwarding_enabled = true }
  assert {
    condition     = azurerm_network_security_rule.hybrid_forwarding["iperf"].destination_address_prefix == "10.30.0.0/16" && azurerm_network_security_rule.hybrid_forwarding["iperf"].source_address_prefix == "10.10.0.0/24" && azurerm_network_security_rule.hybrid_forwarding["iperf"].destination_port_range == "5201"
    error_message = "Forwarded packet keeps its remote home destination through NVA ingress NSG."
  }
}

run "hybrid_outbound_default_off" {
  command = plan
  assert {
    condition     = length(azurerm_network_security_rule.hybrid_outbound) == 0
    error_message = "Default networks must not allow forwarded home-source egress."
  }
}
run "home_source_outbound_nsg" {
  command = plan
  variables { hybrid_forwarding_enabled = true }
  assert {
    condition = length(azurerm_network_security_rule.hybrid_outbound) == 2 && alltrue([
      for key, rule in azurerm_network_security_rule.hybrid_outbound :
      rule.name == "allow-hybrid-outbound-${key}" && rule.direction == "Outbound" && rule.access == "Allow" &&
      rule.priority == 2600 + index(["icmp", "iperf"], key) &&
      rule.source_address_prefix == "10.30.0.0/16" && rule.destination_address_prefix == "10.10.0.0/24" &&
      rule.source_port_range == "*" && rule.protocol == (key == "icmp" ? "Icmp" : "Tcp") &&
      rule.destination_port_range == (key == "icmp" ? "*" : "5201") &&
      rule.resource_group_name == "rg-pathwatch-lab" && rule.network_security_group_name == azurerm_network_security_group.this.name
    ])
    error_message = "NVA egress must preserve home source and allow only workload ICMP/TCP5201."
  }
  assert {
    condition     = !contains(keys(azurerm_subnet_route_table_association.this), "nva")
    error_message = "Outbound repair must not install a home UDR on the NVA subnet."
  }
}
