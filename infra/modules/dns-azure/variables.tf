variable "subscription_id" { type = string }
variable "ttl" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.ttl))
    error_message = "Strict UTC TTL required."
  }
}
variable "owner_cidr" {
  type = string
  validation {
    condition     = can(regex("^[0-9.]+/32$", var.owner_cidr)) && var.owner_cidr != "0.0.0.0/32"
    error_message = "One actual owned global IPv4 /32 required; budget guard also validates global address."
  }
}
variable "admin_public_key" { type = string }
variable "hourly" {
  type    = object({ vm = string, ip = string, disk = string, dns = string, tm = string })
  default = { vm = "0.1", ip = "0.005", disk = "0.05", dns = "0.1", tm = "1" }
}
