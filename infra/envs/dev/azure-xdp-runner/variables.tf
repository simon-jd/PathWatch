variable "subscription_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$", var.subscription_id))
    error_message = "Explicit canonical subscription UUID required."
  }
}
variable "xdp_scope" {
  type    = string
  default = "xdp-runner"
  validation {
    condition     = var.xdp_scope == "xdp-runner"
    error_message = "Only explicit xdp-runner scope."
  }
}
variable "location" {
  type    = string
  default = "westus"
  validation {
    condition     = var.location == "westus"
    error_message = "Only reviewed westus candidate."
  }
}
variable "run_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9]{8,16}$", var.run_id))
    error_message = "Unique immutable run ID:8–16 lowercase letters/digits."
  }
}
variable "ttl" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.ttl)) && can(timeadd(var.ttl, "0s"))
    error_message = "Frozen strict UTC deadline required; external guard enforces <=2h."
  }
}
variable "operator_ipv4_cidr" {
  type = string
  validation {
    condition     = can(cidrhost(var.operator_ipv4_cidr, 0)) && endswith(var.operator_ipv4_cidr, "/32")
    error_message = "One operator IPv4 /32; guard also rejects non-global addresses."
  }
}
variable "admin_public_key" {
  type      = string
  sensitive = true
}
variable "hourly" {
  type = object({ vm = string, ip = string, disk = string })
  validation {
    condition     = alltrue([for v in values(var.hourly) : can(tonumber(v)) && tonumber(v) >= 0])
    error_message = "Nonnegative hourly estimates; exact metered lower bounds checked separately."
  }
}
