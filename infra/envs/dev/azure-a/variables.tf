variable "subscription_id" {
  description = "Explicit authorized Azure subscription UUID."
  type        = string
}
variable "location" {
  description = "Network location in the five subscription-policy allowed regions; no compute SKU claim."
  type        = string
  default     = "westus"
}
variable "ttl" {
  description = "Required UTC expiry timestamp, YYYY-MM-DDTHH:MM:SSZ."
  type        = string
}
variable "wireguard_peer_cidrs" {
  description = "Owned public peer /32 addresses; no WireGuard ingress until Task 3 supplies these."
  type        = set(string)
  default     = []
}
variable "nva_private_ip" {
  description = "Optional NVA .1.4-.1.30 in this regional NVA subnet; no home UDR until configured."
  type        = string
  default     = null
}
variable "home_routes_confirmed_no_overlap" {
  description = "Operator must verify actual home LAN/container/VPN prefixes before routing."
  type        = bool
  default     = false
}
