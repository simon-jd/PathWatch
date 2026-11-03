variable "resource_group_name" {
  type = string
  validation {
    condition     = var.resource_group_name == "rg-pathwatch-lab"
    error_message = "Only rg-pathwatch-lab is authorized."
  }
}
variable "location" { type = string }
variable "tags" {
  type = map(string)
  validation {
    condition     = try(var.tags.project == "pathwatch" && var.tags.owner == "codex-pathwatch" && can(timeadd(var.tags.ttl, "0s")) && can(regex("Z$", var.tags.ttl)) && tonumber(var.tags.est_usd_per_hour) >= 0, false)
    error_message = "Required project ownership, UTC expiry and nonnegative estimate tags missing."
  }
}
variable "github_repository" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Exact owner/repository required; wildcard subjects forbidden."
  }
}
variable "state_storage_account_id" {
  type = string
  validation {
    condition     = can(regex("(?i)^/subscriptions/[0-9a-f-]{36}/resourceGroups/rg-pathwatch-lab/providers/Microsoft.Storage/storageAccounts/[a-z0-9]+$", var.state_storage_account_id))
    error_message = "State must be a storage account in exact project RG."
  }
}
