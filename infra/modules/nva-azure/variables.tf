variable "subscription_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "Explicit subscription UUID required."
  }
}
variable "location" {
  type    = string
  default = "westus"
  validation {
    condition     = var.location == "westus"
    error_message = "Only westus has reviewed ARM64 SKU/image evidence for Task3."
  }
}
variable "ttl" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.ttl)) && can(timeadd(var.ttl, "0s"))
    error_message = "Strict UTC TTL required; guard also enforces four-hour timebox and parent expiry."
  }
}
variable "admin_public_key" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^ssh-(ed25519|rsa) [A-Za-z0-9+/=]+( [^\\n]*)?$", var.admin_public_key))
    error_message = "SSH public key only, never a private key."
  }
}
variable "hourly" {
  description = "Conservative per-resource rates from fresh exact Retail selectors; disk includes transactions."
  type        = object({ vm = string, ip = string, disk = string })
  validation {
    condition     = alltrue([for rate in values(var.hourly) : try(tonumber(rate) > 0 && tonumber(rate) <= 0.2, false)])
    error_message = "All three priced resources require positive rate tags <=0.2 USD/hour."
  }
}
variable "vm_size" {
  type    = string
  default = "Standard_B2pts_v2"
  validation {
    condition     = var.vm_size == "Standard_B2pts_v2"
    error_message = "Only reviewed 2vCPU/1GiB ARM B2pts_v2 is allowed; availability/quota still need live checks."
  }
}
variable "disk_size_gb" {
  type    = number
  default = 30
  validation {
    condition     = contains([30, 31, 32], var.disk_size_gb)
    error_message = "OS disk must be 30–32GB Standard_LRS."
  }
}
variable "include_client" {
  description = "Temporary workload VM for actual UDR data-plane acceptance, torn down with this state."
  type        = bool
  default     = false
}
