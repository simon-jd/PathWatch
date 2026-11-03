variable "subscription_id" {
  description = "Explicit authorized subscription, checked against existing RG ARM ID."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "An explicit subscription UUID is required."
  }
}
variable "resource_group_name" {
  description = "Existing Task 1 RG; this module never creates or deletes the shared group."
  type        = string
  default     = "rg-pathwatch-lab"
  validation {
    condition     = var.resource_group_name == "rg-pathwatch-lab"
    error_message = "Only rg-pathwatch-lab is authorized."
  }
}
variable "name" {
  description = "Unique regional network resource suffix."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.name))
    error_message = "Use 3-40 lowercase letters, digits or hyphens, starting with a letter."
  }
}
variable "location" {
  description = "One of the five locations allowed by current subscription policy; no SKU availability claim."
  type        = string
  validation {
    condition     = contains(["westus", "francecentral", "norwayeast", "denmarkeast", "swedencentral"], var.location)
    error_message = "Choose westus/francecentral/norwayeast/denmarkeast/swedencentral; other locations are disallowed."
  }
}
variable "address_space" {
  description = "Exact Azure regional /16; home 10.30/16 and WireGuard 10.255.0/24 remain reserved."
  type        = string
  validation {
    condition     = contains(["10.10.0.0/16", "10.20.0.0/16"], var.address_space)
    error_message = "Only the two assigned Azure /16 CIDRs (10.10/16 and 10.20/16) are permitted."
  }
}
variable "tags" {
  description = "All taggable parents require project ownership, exact UTC expiry and zero hourly estimate."
  type        = map(string)
  validation {
    condition     = try(var.tags.project == "pathwatch" && var.tags.owner == "codex-pathwatch" && can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.tags.ttl)) && can(timeadd(var.tags.ttl, "0s")) && tonumber(var.tags.est_usd_per_hour) == 0, false)
    error_message = "Supply project=pathwatch, owner=codex-pathwatch, valid UTC ttl and est_usd_per_hour=0 for free networking."
  }
}
variable "private_ingress_rules" {
  description = "Optional self-owned private probe traffic; no SSH/RDP or wildcard ports. Destinations are subnet purposes."
  type = map(object({
    source_prefix     = string
    destination       = string
    protocol          = string
    destination_ports = optional(list(string), [])
  }))
  default = {}
  validation {
    condition = length(var.private_ingress_rules) <= 100 && alltrue([for key, rule in var.private_ingress_rules :
      can(regex("^[a-z][a-z0-9-]{0,39}$", key)) && contains(["workloads", "nva", "aks_nodes"], rule.destination) && contains(["Tcp", "Udp", "Icmp"], rule.protocol) &&
      try(tonumber(split("/", rule.source_prefix)[1]) >= 16 && tonumber(split("/", rule.source_prefix)[1]) <= 32 && can(cidrnetmask(rule.source_prefix)) && cidrhost(rule.source_prefix, 0) == split("/", rule.source_prefix)[0] && ((can(regex("^10[.](10|20|30)[.]", cidrhost(rule.source_prefix, 0))) && can(regex("^10[.](10|20|30)[.]", cidrhost(rule.source_prefix, -1)))) || (tonumber(split("/", rule.source_prefix)[1]) >= 24 && can(regex("^10[.]255[.]0[.]", cidrhost(rule.source_prefix, 0))) && can(regex("^10[.]255[.]0[.]", cidrhost(rule.source_prefix, -1))))), false) &&
      (rule.protocol == "Icmp" ? length(rule.destination_ports) == 0 : length(rule.destination_ports) > 0 && alltrue([for port in rule.destination_ports : try(tonumber(port) >= 1 && tonumber(port) <= 65535 && floor(tonumber(port)) == tonumber(port) && !contains([22, 3389], tonumber(port)), false)]))
    ])
    error_message = "Use self-owned 10.10/16, 10.20/16, 10.30/16 or 10.255.0/24 sources, subnet destinations, ICMP or explicit TCP/UDP ports excluding SSH/RDP."
  }
}
variable "wireguard_peer_cidrs" {
  description = "Public IPv4 /32 addresses of owned WireGuard peers; empty until Task 3. Only UDP51820 to NVA subnet is allowed."
  type        = set(string)
  default     = []
  validation {
    condition = length(var.wireguard_peer_cidrs) <= 100 && alltrue([for peer in var.wireguard_peer_cidrs :
      try(can(cidrnetmask(peer)) && split("/", peer)[1] == "32" && cidrhost(peer, 0) == split("/", peer)[0] && !can(regex("^(0[.]|10[.]|100[.](6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])[.]|127[.]|169[.]254[.]|192[.]0[.](0|2)[.]|192[.]88[.]99[.]|198[.](18|19)[.]|198[.]51[.]100[.]|203[.]0[.]113[.]|172[.](1[6-9]|2[0-9]|3[01])[.]|192[.]168[.]|22[4-9][.]|23[0-9][.]|24[0-9][.]|25[0-5][.])", cidrhost(peer, 0))), false)
    ])
    error_message = "WireGuard peers must be explicit owned public IPv4 /32 addresses, never world or private ranges."
  }
}
variable "home_routes_confirmed_no_overlap" {
  description = "Operator confirms actual home LAN/container/VPN routes do not overlap the reserved ranges before enabling UDRs."
  type        = bool
  default     = false
}
variable "nva_private_ip" {
  description = "Optional Task 3/10 NVA NIC IPv4 inside NVA /27 (.1.4-.1.30); null leaves all UDR routes absent."
  type        = string
  default     = null
  validation {
    condition     = var.nva_private_ip == null || try(var.home_routes_confirmed_no_overlap && cidrhost("${var.nva_private_ip}/32", 0) == var.nva_private_ip && join(".", slice(split(".", var.nva_private_ip), 0, 3)) == "${join(".", slice(split(".", var.address_space), 0, 2))}.1" && tonumber(split(".", var.nva_private_ip)[3]) >= 4 && tonumber(split(".", var.nva_private_ip)[3]) <= 30, false)
    error_message = "Confirm real home-route nonoverlap and supply an NVA address .1.4-.1.30 inside this VNet, or leave null."
  }
}
variable "wireguard_alternate_enabled" {
  description = "Task3 second logical WG tunnel on UDP51821, scoped to the same owned /32 peers."
  type        = bool
  default     = false
}
variable "management_peer_cidrs" {
  description = "Optional actual owner public IPv4 /32 SSH access to NVA/workload subnets."
  type        = set(string)
  default     = []
  validation {
    condition     = length(var.management_peer_cidrs) <= 2 && alltrue([for peer in var.management_peer_cidrs : try(can(cidrnetmask(peer)) && split("/", peer)[1] == "32" && cidrhost(peer, 0) == split("/", peer)[0] && !can(regex("^(0[.]|10[.]|100[.](6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])[.]|127[.]|169[.]254[.]|192[.]0[.](0|2)[.]|192[.]88[.]99[.]|198[.](18|19)[.]|198[.]51[.]100[.]|203[.]0[.]113[.]|172[.](1[6-9]|2[0-9]|3[01])[.]|192[.]168[.]|22[4-9][.]|23[0-9][.]|24[0-9][.]|25[0-5][.])", cidrhost(peer, 0))), false)])
    error_message = "SSH may be enabled only for up to two actual owner public IPv4 /32 addresses."
  }
}
variable "hybrid_forwarding_enabled" {
  description = "Fixed private ICMP/TCP5201 transit: workload-to-home NVA ingress and home-to-workload NVA egress, preserving original addresses."
  type        = bool
  default     = false
}
