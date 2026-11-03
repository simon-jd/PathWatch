variable "subscription_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-f-]{36}$", var.subscription_id))
    error_message = "Explicit Azure subscription UUID required."
  }
}
variable "location" {
  type    = string
  default = "westus2"
}
variable "ttl" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.ttl)) && can(timeadd(var.ttl, "0s"))
    error_message = "ttl must be a valid UTC timestamp YYYY-MM-DDTHH:MM:SSZ."
  }
}
variable "est_usd_per_hour" {
  type = number
  validation {
    condition     = var.est_usd_per_hour >= 0 && var.est_usd_per_hour <= 0.10
    error_message = "Storage estimate must be in [0, 0.10]; budget guard separately checks metered usage."
  }
}
variable "storage_account_name" {
  type = string
  validation {
    condition     = can(regex("^pw[a-z0-9]{3,22}$", var.storage_account_name))
    error_message = "Use globally unique 5–24 lowercase alphanumeric name beginning pw."
  }
}
variable "operator_principal_id" { type = string }
variable "operator_ipv4_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.operator_ipv4_cidrs) > 0 && alltrue([for ip in var.operator_ipv4_cidrs : can(cidrhost(ip, 0)) && !endswith(ip, "/0")])
    error_message = "Provide operator public IPv4 CIDRs; no world access."
  }
}
