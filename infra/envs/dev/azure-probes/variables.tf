variable "subscription_id" { type = string }
variable "probe_site" {
  type = string
  validation {
    condition     = contains(["azure-a", "azure-b"], var.probe_site)
    error_message = "One explicitly named physical Azure observer per state."
  }
}
variable "location" { type = string }
variable "ttl" { type = string }
variable "parent_ttl" { type = string }
variable "campaign_id" { type = string }
variable "vm_size" { type = string }
variable "image_version" { type = string }
variable "admin_public_key" {
  type      = string
  sensitive = true
}
variable "hourly" { type = object({ vm = string, ip = string, disk = string }) }
variable "owner_peer_cidrs" {
  type = set(string)
  validation {
    condition     = length(var.owner_peer_cidrs) == 1
    error_message = "Exactly one verified owned public IPv4 /32."
  }
}
variable "home_routes_confirmed_no_overlap" {
  type = bool
  validation {
    condition     = var.home_routes_confirmed_no_overlap
    error_message = "Current host routes must be checked before enabling project routes."
  }
}
