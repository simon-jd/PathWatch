variable "subscription_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "Explicit subscription UUID required."
  }
}
variable "admin_public_key" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^ssh-(ed25519|rsa) [A-Za-z0-9+/=]+( [^\\n]*)?$", var.admin_public_key))
    error_message = "SSH public key required."
  }
}
variable "parent_ttl" {
  type = string
  validation {
    condition     = can(timeadd(var.parent_ttl, "0s"))
    error_message = "Existing parent resource-group expiry required."
  }
}
variable "observers" {
  description = "Explicit future A/B configuration. B2ats unavailable in prior discovery; neither SKU is preauthorized. Task10B must verify fresh quota/SKU, prices, campaign history and all network resources."
  type = map(object({
    location      = string
    vm_size       = string
    image_version = string
    nic_id        = string
    ttl           = string
    vm_hourly     = string
    disk_hourly   = string
  }))
  default = {}
  validation {
    condition = alltrue([for id, v in var.observers :
      contains(["azure-a", "azure-b"], id) && contains(["Standard_B2ats_v2", "Standard_B2pts_v2"], v.vm_size) &&
      can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", v.image_version)) && can(timeadd(v.ttl, "0s")) &&
      try(tonumber(v.vm_hourly) > 0 && tonumber(v.disk_hourly) > 0, false)
    ])
    error_message = "Only explicit Azure A/B, supported small CPU SKU, pinned Ubuntu version, UTC TTL and positive priced tags."
  }
}
