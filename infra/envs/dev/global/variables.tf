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
    condition     = var.est_usd_per_hour >= 0 && var.est_usd_per_hour <= 0.02
    error_message = "Task 1 storage estimate must be in [0, 0.02]; budget guard separately checks metered usage."
  }
}
variable "github_repository" { type = string }
variable "state_storage_account_id" { type = string }
