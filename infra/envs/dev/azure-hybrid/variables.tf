variable "subscription_id" { type = string }
variable "location" {
  type    = string
  default = "westus"
}
variable "ttl" { type = string }
variable "admin_public_key" {
  type      = string
  sensitive = true
}
variable "hourly" { type = object({ vm = string, ip = string, disk = string }) }
variable "include_client" {
  type    = bool
  default = true
}
variable "owner_peer_cidrs" {
  type = set(string)
  validation {
    condition     = length(var.owner_peer_cidrs) == 1
    error_message = "One actual owned public /32 is required for this bounded experiment."
  }
}
variable "home_routes_confirmed_no_overlap" {
  type = bool
  validation {
    condition     = var.home_routes_confirmed_no_overlap
    error_message = "Recheck actual host LAN, Docker and VPN routes before enabling UDRs."
  }
}
